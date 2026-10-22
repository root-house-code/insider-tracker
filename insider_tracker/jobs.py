"""In-process job runner, so the UI can trigger an ingest instead of only the CLI.

State lives in memory only -- it resets if the process restarts, same as any
mid-run state a CLI invocation would lose. Each source can have at most one job
running at a time; a second attempt is a no-op that reports the job as already
running rather than starting a second one. This is deliberately not a queue or
a persisted job table: a single local user clicking one button at a time does
not need either, and adding them would be solving a problem nobody has here.
"""
import datetime as dt
import logging
import threading

from . import config, db, edgar_13f, edgar_form4, funds, house, report, senate
from .edgar_common import tickers_to_ciks
from .net import PoliteSession

log = logging.getLogger(__name__)
SOURCES = ("form4", "house", "senate", "13f", "funds", "update")

_lock = threading.Lock()
_jobs = {}  # source -> {status, started_at, finished_at, message, error}


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def status(source: str = None):
    """A snapshot of one job, or all of them if source is omitted."""
    with _lock:
        if source:
            return dict(_jobs.get(source, {"status": "idle"}))
        return {s: dict(_jobs.get(s, {"status": "idle"})) for s in SOURCES}


def any_running() -> bool:
    with _lock:
        return any(j.get("status") in BUSY for j in _jobs.values())


# Update runs these in order, each reported on its own row as well as Update's.
UPDATE_SOURCES = ("form4", "house", "senate", "13f")
BUSY = ("running", "queued")


def _fresh(status: str) -> dict:
    return {"status": status, "started_at": _now() if status == "running" else None,
            "finished_at": None, "message": None, "error": None}


def start(source: str, db_path=None, **params) -> bool:
    """Start `source` on a background thread. Returns False if it, or a job it
    would overlap with, is already running.

    Update and the individual sources exclude each other: Update can't start
    while any of its sources is busy, and no source can start while Update is
    running -- otherwise two writers race on the same database."""
    if source not in SOURCES:
        raise ValueError(f"unknown source: {source}")
    involved = (source, *UPDATE_SOURCES) if source == "update" else (source, "update")
    with _lock:
        if any(_jobs.get(s, {}).get("status") in BUSY for s in involved):
            return False
        _jobs[source] = _fresh("running")
        if source == "update":
            for s in UPDATE_SOURCES:
                _jobs[s] = _fresh("queued")
    threading.Thread(target=_run, args=(source, db_path, params), daemon=True).start()
    return True


def _set(source: str, **kw):
    with _lock:
        _jobs[source].update(**kw)


def _run(source: str, db_path, params: dict):
    # connect() belongs inside the try: if it raises (e.g. "database is locked"
    # while another job holds a write), the job must still end as 'error' --
    # otherwise it reads 'running' forever and that source can never start again.
    conn = None
    try:
        conn = db.connect(db_path)
        message = _RUNNERS[source](conn, params)
        with _lock:
            _jobs[source].update(status="ok", message=message, finished_at=_now())
    except SystemExit as exc:  # a runner's own "can't proceed" (e.g. no watchlist)
        with _lock:
            _jobs[source].update(status="error", error=str(exc), finished_at=_now())
    except Exception as exc:
        log.exception("job %s failed", source)
        with _lock:
            _jobs[source].update(status="error", error=f"{type(exc).__name__}: {exc}",
                                 finished_at=_now())
    finally:
        if conn is not None:
            conn.close()
        if source == "update":
            # If Update died before reaching some sources (e.g. it couldn't even open
            # the database), don't leave those rows 'queued' -- that would block
            # every one of them until the server restarts.
            with _lock:
                for s in UPDATE_SOURCES:
                    if _jobs.get(s, {}).get("status") in BUSY:
                        _jobs[s].update(status="skipped", finished_at=_now(),
                                        message="not run: Update stopped first")


def _run_form4(conn, params: dict) -> str:
    http = PoliteSession(config.sec_user_agent(), config.SEC_MIN_INTERVAL)
    end = dt.date.today()
    start_date = end - dt.timedelta(days=int(params.get("days") or 3))
    ciks = None
    if not params.get("all"):
        tickers = [t.strip() for t in (params.get("tickers") or "").split(",") if t.strip()]
        if not tickers:
            tickers = config.load_watchlist().get("tickers", [])
        if not tickers:
            raise SystemExit("No tickers to filter on. Add some in Watchlist, or run unfiltered.")
        ciks = set(tickers_to_ciks(http, tickers).values())
    n = edgar_form4.ingest(conn, http, start_date, end, ciks)
    conn.commit()
    scope = "all issuers" if ciks is None else f"watchlist of {len(ciks)}"
    return f"stored {n} transactions ({start_date} to {end}, {scope})"


def _run_house(conn, params: dict) -> str:
    http = PoliteSession(config.sec_user_agent(), config.CONGRESS_MIN_INTERVAL)
    year = int(params.get("year") or dt.date.today().year)
    since = (params.get("since") or "").strip() or None
    n = house.ingest(conn, http, year, since=since)
    return f"stored {n} transactions for {year}" + (f" since {since}" if since else "")


def _run_senate(conn, params: dict) -> str:
    http = PoliteSession(config.sec_user_agent(), config.CONGRESS_MIN_INTERVAL)
    since = dt.date.today() - dt.timedelta(days=int(params.get("days") or 30))
    n = senate.ingest(conn, http, since)
    return f"stored {n} transactions since {since}"


def _run_13f(conn, params: dict) -> str:
    http = PoliteSession(config.sec_user_agent(), config.SEC_MIN_INTERVAL)
    managers = config.load_watchlist().get("managers_13f", {})
    if not managers:
        raise SystemExit("No 13F managers in the Watchlist tab.")
    n = edgar_13f.ingest(conn, http, managers, quarters=int(params.get("quarters") or 4))
    return f"stored {n} positions for {len(managers)} managers"


def _run_funds(conn, params: dict) -> str:
    r = funds.ingest(conn, tracked=config.load_watchlist().get("managers_13f", {}))
    return (f"13F {r['period']} vs {r['prior']}: {r['compared']:,} institutions, {r['stocks']:,} stocks "
            f"({r['with_ticker'] + r.get('confirmed', 0):,} with a ticker)")


def _run_update(conn, params: dict) -> str:
    # Form 4 runs unfiltered: Update is the "refresh everything" button, so it
    # shouldn't silently depend on a watchlist (which may well be empty).
    results = []
    for name, fn, sub_params in (("form4", _run_form4, {"all": True}),
                                 ("house", _run_house, {}),
                                 ("senate", _run_senate, {}),
                                 ("13f", _run_13f, {})):
        _set(name, **_fresh("running"))
        try:
            msg = fn(conn, sub_params)
            _set(name, status="ok", message=msg, finished_at=_now())
            results.append(f"{name}: {msg}")
        except SystemExit as exc:  # can't proceed, e.g. no 13F managers -- not a failure
            _set(name, status="skipped", message=str(exc), finished_at=_now())
            results.append(f"{name}: skipped ({exc})")
        except Exception as exc:
            log.exception("update: %s failed", name)
            err = f"{type(exc).__name__}: {exc}"
            _set(name, status="error", error=err, finished_at=_now())
            results.append(f"{name}: failed ({err})")
    conn.commit()
    path = report.build(conn, 30, config.REPORT_DIR)
    results.append(f"report written to {path}")
    return " | ".join(results)


_RUNNERS = {"form4": _run_form4, "house": _run_house, "senate": _run_senate,
            "13f": _run_13f, "funds": _run_funds, "update": _run_update}
