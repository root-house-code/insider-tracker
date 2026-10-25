"""When each company reports its quarterly results, from the SEC.

Companies announce results with a Form 8-K that includes Item 2.02 (Results of Operations and
Financial Condition), so each company's SEC submissions record lists every earnings report it has
made. Stored per company in the earnings table, refreshed every couple of weeks (a new report comes
each quarter), and used to say where in the quarter insiders bought.

Why it matters: most companies bar insiders from trading in the weeks before results, so buying
reveals something about timing. In the 2015-2026 backtest, CFO clusters bought one to two months
after a report -- mid-quarter -- were the only group where more than half beat small caps in both
halves of the period. Clusters bought shortly before a report did worse. (Found by looking, on a
few hundred clusters: the backtest tracks it from 2026-10-24 on to see if it holds.)

Some companies (mostly small or foreign) don't file Item 2.02 8-Ks; they come out "unknown".
"""
import bisect
import datetime as dt
import json
import logging
from pathlib import Path

import requests

from . import background, config
from .net import PoliteSession

log = logging.getLogger(__name__)
URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
PAGE = "https://data.sec.gov/submissions/{name}"
FRESH = dt.timedelta(days=14)
SINCE = "2014-01-01"
TIMING = [(0, 7, "first week after results"), (8, 30, "1-4 weeks after results"),
          (31, 60, "1-2 months after results"), (61, 10_000, "2+ months after results")]
MID_QUARTER = "1-2 months after results"
MIN_REPORTS = 4  # fewer than this and the company likely doesn't file Item 2.02 8-Ks: don't judge


def report_dates(block) -> list:
    """8-K filing dates that carry Item 2.02, from one block of an SEC submissions record."""
    return [filed for form, filed, items in zip(block.get("form", []), block.get("filingDate", []),
                                                block.get("items", []))
            if form in ("8-K", "8-K/A") and "2.02" in (items or "")]


def dates(conn, cik) -> list:
    return [r[0] for r in conn.execute("SELECT report_date FROM earnings WHERE cik=? ORDER BY report_date",
                                       (str(int(cik)),))] if str(cik or "").strip().isdigit() else []


def dates_many(conn, ciks) -> dict:
    out = {}
    ciks = sorted({str(int(c)) for c in ciks if str(c or "").strip().isdigit()})
    for i in range(0, len(ciks), 500):
        part = ciks[i:i + 500]
        for cik, d in conn.execute(f"SELECT cik, report_date FROM earnings WHERE cik IN ({','.join('?' * len(part))}) "
                                   "ORDER BY report_date", part):
            out.setdefault(cik, []).append(d)
    return out


def days_since_results(reports, day):
    """Days from the last report before `day` to `day`; None if the history can't say."""
    if len(reports) < MIN_REPORTS:
        return None
    i = bisect.bisect_left(reports, day) - 1
    return (dt.date.fromisoformat(day) - dt.date.fromisoformat(reports[i])).days if i >= 0 else None


def timing(days) -> str:
    """Where in the quarter: a label for days since the last results ('unknown' if not known)."""
    if days is None:
        return "unknown"
    return next(label for lo, hi, label in TIMING if lo <= days <= hi)


def missing(conn, ciks, now=None) -> list:
    now = now or dt.datetime.now(dt.timezone.utc)
    want = {str(int(c)) for c in ciks if str(c or "").strip().isdigit()}
    seen = {r[0]: r[1] for r in conn.execute("SELECT cik, fetched_at FROM earnings_meta")}
    return sorted(c for c in want if c not in seen or now - dt.datetime.fromisoformat(seen[c]) >= FRESH)


def fetch_many(conn, ciks, http=None, now=None) -> int:
    """Fetch and store each company's earnings report dates. Never raises; returns how many done."""
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        http = http or PoliteSession(config.sec_user_agent(), config.SEC_MIN_INTERVAL, max_retries=1, timeout=20)
    except SystemExit as exc:
        log.warning("earnings dates skipped: %s", exc)
        return 0
    done = 0
    for cik in ciks:
        try:
            r = http.get(URL.format(cik=int(cik)))
            found = []
            if r.status_code != 404:
                r.raise_for_status()
                j = r.json()
                recent = (j.get("filings") or {}).get("recent") or {}
                found = report_dates(recent)
                oldest = min(recent.get("filingDate") or ["9999"])
                for f in (j.get("filings") or {}).get("files", []):  # older filings live on extra pages
                    if oldest < SINCE:
                        break
                    rr = http.get(PAGE.format(name=f["name"]))
                    if rr.status_code == 200:
                        page = rr.json()
                        found += report_dates(page)
                        oldest = min(page.get("filingDate") or [oldest])
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning("earnings dates for %s failed: %s", cik, exc)
            continue
        store(conn, cik, found, now)
        done += 1
    return done


def store(conn, cik, report_days, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    cik = str(int(cik))
    conn.executemany("INSERT OR IGNORE INTO earnings (cik, report_date) VALUES (?,?)",
                     [(cik, d) for d in set(report_days) if d >= SINCE])
    conn.execute("INSERT OR REPLACE INTO earnings_meta (cik, fetched_at) VALUES (?,?)",
                 (cik, now.isoformat(timespec="seconds")))
    conn.commit()


def import_json(conn, path) -> int:
    """Load {cik: [dates]} saved by an earlier one-off download (so it isn't fetched twice)."""
    data = json.loads(Path(path).read_text())
    for cik, ds in data.items():
        store(conn, cik, ds)
    return len(data)


def refresh_in_background(db_path, ciks) -> bool:
    if not ciks:
        return background.running("earnings")
    return background.start_db("earnings", db_path, lambda conn: fetch_many(conn, list(ciks)))
