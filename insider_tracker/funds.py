"""What every institution did with each stock last quarter, from the SEC's Form 13F data sets.

edgar_13f.py follows a handful of chosen managers filing by filing. This module answers the other
question -- across ALL ~10,000 13F filers, how many opened, added to, cut or closed a position in
this stock -- from the bulk files the SEC publishes:

    https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets

Each zip covers three months of FILING dates, so it holds essentially one quarter's 13Fs (the
Jun-Aug file holds the 30 June positions). Its INFOTABLE is ~400 MB and millions of rows, far too
much to keep: the rows are staged in a scratch SQLite file, compared quarter on quarter, and only a
summary per stock (fund_flows, a few MB) and the positions of your watchlist managers
(fund_tracked) are stored.

Rules, each from something in the data:
  * Only managers who filed for BOTH quarters are compared. A first-time filer would otherwise
    count as "new" in every stock it holds, and one that stopped filing as "exited" from all.
  * A zip also carries late filings for old quarters (one in 2026 reported 2001); only the quarter
    being summarised is used.
  * Amendments: the latest original-or-restatement filing for a quarter is the position; "new
    holdings" amendments filed after it add to it.
  * Only shares (SH) count -- not bonds (PRN) or options (put/call rows).
  * A change of 5% or less is "held", not added or cut: index funds rebalance constantly.

Lag: a 13F shows positions on the last day of a quarter, filed up to 45 days later, and the SEC
publishes each data set after its three-month window closes. So the newest quarter here is always
two to five months old. It says what institutions were doing before the insiders bought, not
alongside them.
"""
import csv
import datetime as dt
import io
import logging
import re
import sqlite3
import zipfile
from pathlib import Path

from . import config, cusip_map
from .net import PoliteSession

log = logging.getLogger(__name__)

PAGE = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
LINK_RE = re.compile(r'href="(/files/[^"]+/(\d{2}[a-z]{3}\d{4})-(\d{2}[a-z]{3}\d{4})_form13f\.zip)"', re.I)
CHANGE = 0.05          # more than this up or down counts as added / cut
DIR = Path(config.DB_PATH).parent / "13f_sets"
csv.field_size_limit(10_000_000)


def _date(s: str) -> str:
    """'30-JUN-2026' or '30jun2026' -> '2026-06-30'."""
    s = s.strip().replace("-", "")
    return dt.datetime.strptime(s.title(), "%d%b%Y").date().isoformat()


def list_datasets(http) -> list:
    """[(url, window_start, window_end)] newest first, from the SEC's data set page."""
    resp = http.get(PAGE)
    resp.raise_for_status()
    seen, out = set(), []
    for path, start, end in LINK_RE.findall(resp.text):
        if path not in seen:
            seen.add(path)
            out.append(("https://www.sec.gov" + path, _date(start), _date(end)))
    return sorted(out, key=lambda d: d[1], reverse=True)


def quarter_of(zip_path) -> str:
    """The quarter a data set holds: the period most of its 13F-HR filings report. (Read from the
    data rather than the file name: the Jun-Aug window holds 30 June, filed by mid-August, but
    the windows have changed shape before -- 2023q4, then 01jan2024-29feb2024.)"""
    counts = {}
    with zipfile.ZipFile(zip_path) as z:
        for r in _tsv(z, "SUBMISSION.tsv"):
            if r["SUBMISSIONTYPE"] == "13F-HR":
                counts[r["PERIODOFREPORT"]] = counts.get(r["PERIODOFREPORT"], 0) + 1
    if not counts:
        raise ValueError(f"{zip_path}: no 13F-HR filings")
    return _date(max(counts, key=counts.get))


def download(http, url, dest_dir=None) -> Path:
    dest_dir = Path(dest_dir or DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / url.rsplit("/", 1)[-1]
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    resp = http.get(url, timeout=600)
    resp.raise_for_status()
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(resp.content)
    tmp.replace(dest)
    return dest


def _tsv(z, name):
    with z.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline=""),
                                  delimiter="\t", quoting=csv.QUOTE_NONE)


def _filings(z, period):
    """{accession: (cik, manager name)} -- the filings that state `period`'s positions."""
    cover = {r["ACCESSION_NUMBER"]: r for r in _tsv(z, "COVERPAGE.tsv")}
    subs = []
    for r in _tsv(z, "SUBMISSION.tsv"):
        if r["SUBMISSIONTYPE"] not in ("13F-HR", "13F-HR/A"):
            continue
        try:
            if _date(r["PERIODOFREPORT"]) != period:
                continue
            filed = _date(r["FILING_DATE"])
        except ValueError:
            continue
        c = cover.get(r["ACCESSION_NUMBER"], {})
        kind = (c.get("AMENDMENTTYPE") or "").upper() if r["SUBMISSIONTYPE"] == "13F-HR/A" else ""
        subs.append((str(int(r["CIK"])), filed, r["ACCESSION_NUMBER"], kind, c.get("FILINGMANAGER_NAME") or ""))
    by_cik = {}
    for s in subs:
        by_cik.setdefault(s[0], []).append(s)
    keep = {}
    for cik, ss in by_cik.items():
        base = max((s for s in ss if "NEW HOLDINGS" not in s[3]), key=lambda s: (s[1], s[2]), default=None)
        if base is None:
            continue
        keep[base[2]] = (cik, base[4])
        for s in ss:  # holdings added by a later amendment
            if "NEW HOLDINGS" in s[3] and (s[1], s[2]) > (base[1], base[2]):
                keep[s[2]] = (cik, base[4])
    return keep


def _stage(stage, zip_path, period):
    """Load one quarter's share positions into the scratch database. Returns managers counted."""
    with zipfile.ZipFile(zip_path) as z:
        keep = _filings(z, period)
        stage.executemany("INSERT OR REPLACE INTO managers VALUES (?,?,?)",
                          [(period, cik, name) for cik, name in set(keep.values())])
        batch = []
        for r in _tsv(z, "INFOTABLE.tsv"):
            who = keep.get(r["ACCESSION_NUMBER"])
            if not who or r["SSHPRNAMTTYPE"] != "SH" or (r["PUTCALL"] or "").strip():
                continue
            try:
                shares = float(r["SSHPRNAMT"])
                value = float(r["VALUE"] or 0)
            except ValueError:
                continue
            batch.append((period, who[0], r["CUSIP"].strip().upper(), r["NAMEOFISSUER"].strip(), shares, value))
            if len(batch) >= 50_000:
                stage.executemany("INSERT INTO pos VALUES (?,?,?,?,?,?)", batch)
                batch = []
        stage.executemany("INSERT INTO pos VALUES (?,?,?,?,?,?)", batch)
        stage.commit()
    return len({c for c, _ in keep.values()})


def summarize(conn, zip_now, zip_before, tracked=None, work_dir=None, sec_http=None) -> dict:
    """Compare the quarter in `zip_now` with the one in `zip_before` and store the result:
    fund_flows (one row per stock) and fund_tracked (your watchlist managers' positions)."""
    period, prior = quarter_of(zip_now), quarter_of(zip_before)
    if prior >= period:
        raise ValueError(f"expected an older quarter to compare against, got {prior} vs {period}")
    work = Path(work_dir or Path(zip_now).parent) / "staging.db"
    if work.exists():
        work.unlink()
    stage = sqlite3.connect(work)
    try:
        stage.executescript("""
            PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
            CREATE TABLE pos (period TEXT, cik TEXT, cusip TEXT, issuer TEXT, shares REAL, value REAL);
            CREATE TABLE managers (period TEXT, cik TEXT, name TEXT, PRIMARY KEY (period, cik));""")
        n_now = _stage(stage, zip_now, period)
        n_before = _stage(stage, zip_before, prior)
        stage.executescript("CREATE INDEX ix ON pos(period, cik, cusip);")
        rows = stage.execute(f"""
            WITH both_q AS (SELECT cik FROM managers WHERE period=:p
                            INTERSECT SELECT cik FROM managers WHERE period=:q),
                 a AS (SELECT cik, cusip, MAX(issuer) AS issuer, SUM(shares) AS s, SUM(value) AS v FROM pos
                       WHERE period=:p AND cik IN both_q GROUP BY cik, cusip),
                 b AS (SELECT cik, cusip, MAX(issuer) AS issuer, SUM(shares) AS s FROM pos
                       WHERE period=:q AND cik IN both_q GROUP BY cik, cusip),
                 j AS (SELECT a.cik, a.cusip, a.issuer, a.s AS now, a.v, COALESCE(b.s, 0) AS before
                       FROM a LEFT JOIN b ON b.cik=a.cik AND b.cusip=a.cusip
                       UNION ALL
                       SELECT b.cik, b.cusip, b.issuer, 0, 0, b.s FROM b
                       LEFT JOIN a ON a.cik=b.cik AND a.cusip=b.cusip WHERE a.cik IS NULL)
            SELECT cusip, MAX(issuer),
                   SUM(now > 0), SUM(now > 0 AND before <= 0),
                   SUM(now > 0 AND before > 0 AND now > before * {1 + CHANGE}),
                   SUM(now > 0 AND before > 0 AND now < before * {1 - CHANGE}),
                   SUM(now <= 0 AND before > 0), SUM(now), SUM(before), SUM(v)
            FROM j GROUP BY cusip""", {"p": period, "q": prior}).fetchall()
        # Filers type issuer names themselves ("QNITY ELECTRONICS INC" against H.B. Fuller's CUSIP),
        # so the name shown -- and matched to a ticker -- is the one most filers used.
        names, best = {}, {}
        for cusip, issuer, n in stage.execute(
                "SELECT cusip, issuer, COUNT(*) FROM pos WHERE period=? GROUP BY cusip, issuer", (period,)):
            if n > best.get(cusip, 0):
                names[cusip], best[cusip] = issuer, n
        rows = [(r[0], names.get(r[0], r[1]), *r[2:]) for r in rows]
        compared = stage.execute("SELECT COUNT(*) FROM (SELECT cik FROM managers WHERE period=? "
                                 "INTERSECT SELECT cik FROM managers WHERE period=?)", (period, prior)).fetchone()[0]
        labels = {str(int(c)): (n or "").strip() for c, n in dict(tracked or {}).items()}
        tracked = set(labels)
        mine = []
        if tracked:
            marks = ",".join("?" * len(tracked))
            mine = stage.execute(f"""
                SELECT m.cik, m.name, x.cusip, SUM(CASE WHEN x.period=? THEN x.shares ELSE 0 END),
                       SUM(CASE WHEN x.period=? THEN x.shares ELSE 0 END)
                FROM pos x JOIN managers m ON m.cik=x.cik AND m.period=?
                WHERE x.cik IN ({marks}) GROUP BY m.cik, x.cusip""",
                (period, prior, period, *tracked)).fetchall()
            # your own label from the Watchlist ("MM") reads better than the legal filing name
            mine = [(cik, labels.get(cik) or name, *rest) for cik, name, *rest in mine]
    finally:
        stage.close()
        work.unlink(missing_ok=True)

    tickers = _tickers(conn, {r[0]: r[1] for r in rows}, sec_http)
    conn.execute("DELETE FROM fund_flows WHERE period=?", (period,))
    conn.executemany(
        "INSERT INTO fund_flows (period, prior_period, cusip, issuer, ticker, holders, new, added, cut, "
        "exited, shares, prior_shares, value) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(period, prior, r[0], r[1], tickers.get(r[0], ""), *r[2:]) for r in rows])
    conn.execute("DELETE FROM fund_tracked WHERE period=?", (period,))
    conn.executemany("INSERT INTO fund_tracked (period, cik, manager, cusip, shares, prior_shares) "
                     "VALUES (?,?,?,?,?,?)", [(period, *r) for r in mine])
    conn.execute("INSERT OR REPLACE INTO fund_meta (period, prior_period, managers, managers_prior, compared, "
                 "loaded_at) VALUES (?,?,?,?,?,?)",
                 (period, prior, n_now, n_before, compared,
                  dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
    conn.commit()
    return {"period": period, "prior": prior, "stocks": len(rows), "compared": compared,
            "with_ticker": sum(1 for r in rows if tickers.get(r[0]))}


def _tickers(conn, issuers: dict, sec_http=None) -> dict:
    """{cusip: ticker} for the stocks in a quarter: what cusip_ticker already knows (OpenFIGI),
    else the issuer name matched against the SEC's own ticker list. Never raises."""
    known = {r[0]: r[1] for r in conn.execute("SELECT cusip, ticker FROM cusip_ticker WHERE ticker<>''")}
    out = {c: known[c] for c in issuers if c in known}
    try:
        http = sec_http or PoliteSession(config.sec_user_agent(), config.SEC_MIN_INTERVAL)
        resp = http.get(cusip_map.SEC_TICKERS)
        resp.raise_for_status()
        idx = cusip_map.build_name_index(resp.json())
    except (Exception, SystemExit) as exc:
        log.warning("13F tickers by name skipped: %s", exc)
        return out
    for c, name in issuers.items():
        if c not in out:
            tk = cusip_map.match_name(idx, name or "")
            if tk:
                out[c] = tk
    return out


def fill_insider_tickers(conn, period, figi_http=None, since=None) -> int:
    """Second pass for the stocks insiders actually bought: where the SEC's ticker list missed a
    stock, find 13F CUSIPs whose issuer name matches the company's name on its Form 4, and keep
    one only if OpenFIGI confirms that CUSIP is that ticker (a name alone can't tell Class A from
    Class C, or shares from warrants). A few dozen lookups, not tens of thousands. Never raises."""
    from . import prices
    since = since or (dt.date.fromisoformat(period) - dt.timedelta(days=120)).isoformat()
    have = set(flows_by_ticker(conn, period))
    bought = {r[0]: r[1] for r in conn.execute(
        "SELECT ticker, MAX(issuer_name) FROM insider_tx WHERE code='P' AND is_derivative=0 AND ticker<>'' "
        "AND trans_date>=? GROUP BY ticker", (since,))}
    missing = {t: n for t, n in bought.items() if not set(prices.candidates(t)) & have and t not in have}
    by_name = {}
    for c, issuer in conn.execute("SELECT cusip, issuer FROM fund_flows WHERE period=? AND ticker=''", (period,)):
        by_name.setdefault(cusip_map.norm(issuer or ""), []).append(c)
    ask = {}
    for t, name in missing.items():
        key = cusip_map.norm(name or "")
        if len("".join(key)) >= 4:
            for c in by_name.get(key, []):
                ask[c] = t
    if not ask:
        return 0
    http = figi_http or PoliteSession(config.sec_user_agent(), cusip_map.MIN_INTERVAL)
    found, figi_says = 0, {}
    todo = sorted(ask)
    for i in range(0, len(todo), cusip_map.BATCH):
        batch = todo[i:i + cusip_map.BATCH]
        try:
            resp = http.post(cusip_map.URL, json=[{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"}
                                                   for c in batch])
            resp.raise_for_status()
            results = resp.json()
            if len(results) != len(batch):
                raise ValueError(f"asked for {len(batch)}, got {len(results)}")
        except Exception as exc:
            log.warning("13F ticker confirmation skipped for %d CUSIPs: %s", len(batch), exc)
            continue
        for c, res in zip(batch, results):
            tk, nm, st = cusip_map._pick(res)
            figi_says[c] = tk
            want = ask[c]
            same = tk and (tk.upper() == want.upper() or set(prices.candidates(tk)) & set(prices.candidates(want)))
            conn.execute("INSERT OR REPLACE INTO cusip_ticker (cusip, ticker, name, security_type, source) "
                         "VALUES (?,?,?,?,?)", (c, tk, nm, st, "figi" if tk else None))
            if same:
                conn.execute("UPDATE fund_flows SET ticker=? WHERE period=? AND cusip=?", (want, period, c))
                found += 1
        conn.commit()
    # OpenFIGI has holes (foreign-registered CUSIPs, some small US listings). Where it knows
    # nothing -- rather than naming another ticker -- and exactly one 13F CUSIP carries this
    # company's name, the match is unambiguous. Two CUSIPs (Class A and C, shares and warrants)
    # stay unmatched rather than risk the wrong one.
    per_ticker = {}
    for c, t in ask.items():
        per_ticker.setdefault(t, []).append(c)
    for t, cs in per_ticker.items():
        if len(cs) == 1 and figi_says.get(cs[0]) == "" and cs[0] in figi_says:
            conn.execute("UPDATE fund_flows SET ticker=? WHERE period=? AND cusip=? AND ticker=''", (t, period, cs[0]))
            found += 1
    conn.commit()
    return found


def latest(conn):
    """The newest summarised quarter's fund_meta row, or None."""
    r = conn.execute("SELECT * FROM fund_meta ORDER BY period DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def flows_by_ticker(conn, period) -> dict:
    """{ticker: flow row} for one quarter. A ticker with several CUSIPs (share classes) keeps the
    one most managers hold, so no manager is counted twice."""
    out = {}
    for r in conn.execute("SELECT * FROM fund_flows WHERE period=? AND ticker<>'' ORDER BY holders DESC",
                          (period,)):
        out.setdefault(r["ticker"], dict(r))
    return out


def tracked_by_cusip(conn, period) -> dict:
    """{cusip: [(manager, shares, prior_shares)]} for your watchlist managers."""
    out = {}
    for r in conn.execute("SELECT * FROM fund_tracked WHERE period=?", (period,)):
        out.setdefault(r["cusip"], []).append((r["manager"], r["shares"], r["prior_shares"]))
    return out


def ingest(conn, http=None, tracked=None, keep=2) -> dict:
    """Download the two newest data sets (if not already here) and summarise the newer quarter
    against the older. Older downloads are deleted; returns summarize()'s counts."""
    http = http or PoliteSession(config.sec_user_agent(), config.SEC_MIN_INTERVAL)
    sets = list_datasets(http)[:keep]
    if len(sets) < 2:
        raise RuntimeError("the SEC's 13F data set page listed fewer than two files")
    paths = [download(http, url) for url, _, _ in sets]
    for old in DIR.glob("*_form13f.zip"):
        if old not in paths:
            old.unlink()
    result = summarize(conn, paths[0], paths[1], tracked, sec_http=http)
    try:
        result["confirmed"] = fill_insider_tickers(conn, result["period"])
    except (Exception, SystemExit) as exc:  # the summary stands without it
        log.warning("13F ticker second pass skipped: %s", exc)
    return result
