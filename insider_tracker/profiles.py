"""Company descriptions for the chart page, cached in SQLite.

Two sources, labelled in profiles.source:

  yahoo  Yahoo Finance's quoteSummary: the business summary plus sector, industry, HQ,
         employees, website. Unofficial, and it wants a cookie + "crumb" handshake before it
         answers -- the same kind of source (and the same fragility) as the prices.
  sec    The SEC's submissions record for the issuer: official and keyless, but only a
         category ("Homebuilding" level), never a summary. Used when Yahoo has nothing, and
         re-tried against Yahoo daily so a temporary outage doesn't pin a thin description.

tests/fixtures/sec_submissions_len.json is a real response; yahoo_profile_example.json has the
exact structure of one, filled with an invented company (Yahoo's data can't be redistributed).
Together they are the contract the parsers have to keep honouring. A profile is context, not data anyone depends on:
every failure here degrades to "no description", never to an error.
"""
import datetime as dt
import logging

import requests

from . import config
from .net import PoliteSession

log = logging.getLogger(__name__)
YAHOO_COOKIE = "https://fc.yahoo.com"
YAHOO_CRUMB = "https://query1.finance.yahoo.com/v1/test/getcrumb"
YAHOO_SUMMARY = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
UA = "Mozilla/5.0 (compatible; insider-tracker)"

FRESH = {("ok", "yahoo"): dt.timedelta(days=30),   # what a company does barely changes
         ("ok", "sec"): dt.timedelta(days=1)}      # thin: look for the real thing again soon
RETRY_NONE = dt.timedelta(days=7)
RETRY_ERROR = dt.timedelta(minutes=10)


class NotFound(Exception):
    """The source has no such company."""


def parse_yahoo(payload: dict):
    """quoteSummary JSON -> profile dict, or None if it holds nothing worth showing."""
    result = ((payload.get("quoteSummary") or {}).get("result") or [None])[0]
    ap = (result or {}).get("assetProfile") or {}
    summary = (ap.get("longBusinessSummary") or "").strip()
    sector, industry = ap.get("sector") or "", ap.get("industry") or ""
    if not (summary or sector or industry):
        return None
    where = ", ".join(p for p in (ap.get("city"), ap.get("state"), ap.get("country")) if p)
    return {"description": summary, "sector": sector, "industry": industry, "hq": where,
            "employees": ap.get("fullTimeEmployees"), "website": ap.get("website") or "",
            "source": "yahoo"}


def parse_sec(payload: dict):
    """submissions JSON -> profile dict (category only), or None."""
    industry = (payload.get("sicDescription") or "").strip()
    if not industry:
        return None
    return {"description": "", "sector": "", "industry": industry,
            "hq": "", "employees": None, "website": payload.get("website") or "", "source": "sec"}


def _yahoo(http, symbol):
    http.get(YAHOO_COOKIE)  # answers 404, but sets the cookie the crumb is tied to
    crumb = http.get(YAHOO_CRUMB)
    if crumb.status_code != 200 or not crumb.text or "<" in crumb.text:
        raise requests.RequestException(f"Yahoo gave no crumb (HTTP {crumb.status_code})")
    resp = http.get(YAHOO_SUMMARY.format(symbol=symbol),
                    params={"modules": "assetProfile", "crumb": crumb.text})
    if resp.status_code == 404:
        raise NotFound(symbol)
    resp.raise_for_status()
    return parse_yahoo(resp.json())


def _sec(http, cik):
    resp = http.get(SEC_SUBMISSIONS.format(cik=int(cik)))
    if resp.status_code == 404:
        raise NotFound(cik)
    resp.raise_for_status()
    return parse_sec(resp.json())


def _now():
    return dt.datetime.now(dt.timezone.utc)


def get(conn, ticker):
    r = conn.execute("SELECT * FROM profiles WHERE ticker=?", (ticker,)).fetchone()
    return dict(r) if r else None


def _store(conn, ticker, status, now, prof=None, note=None):
    p = prof or {}
    conn.execute(
        "INSERT OR REPLACE INTO profiles (ticker, status, source, description, sector, industry, hq, "
        "employees, website, note, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ticker, status, p.get("source"), p.get("description"), p.get("sector"), p.get("industry"),
         p.get("hq"), p.get("employees"), p.get("website"), note, now.isoformat(timespec="seconds")))
    conn.commit()


def ensure(conn, ticker, symbol=None, cik=None, yahoo_http=None, sec_http=None, now=None):
    """Make sure `ticker` has a cached profile, fetching if it is missing or stale. Returns the
    stored row (or None). Never raises."""
    now = now or _now()
    have = get(conn, ticker)
    if have:
        limit = (FRESH.get((have["status"], have["source"])) if have["status"] == "ok"
                 else {"none": RETRY_NONE, "error": RETRY_ERROR}.get(have["status"]))
        if limit is not None and now - dt.datetime.fromisoformat(have["fetched_at"]) < limit:
            return have

    prof, problem = None, None
    if symbol:
        try:
            prof = _yahoo(yahoo_http or PoliteSession(UA, 0.4, max_retries=1, timeout=15), symbol)
        except NotFound:
            pass
        except (requests.RequestException, ValueError, KeyError) as exc:
            problem = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("profile fetch for %s failed: %s", symbol, exc)
    if prof is None and cik:
        try:
            if sec_http is None:
                sec_http = PoliteSession(config.sec_user_agent(), config.SEC_MIN_INTERVAL, max_retries=1, timeout=15)
            prof = _sec(sec_http, cik)
        except (NotFound, SystemExit):
            pass
        except (requests.RequestException, ValueError, KeyError) as exc:
            problem = problem or f"{type(exc).__name__}: {exc}"[:200]
    if prof:
        _store(conn, ticker, "ok", now, prof)
    elif problem:
        _store(conn, ticker, "error", now, note=problem)
    else:
        _store(conn, ticker, "none", now, note="no company profile found")
    return get(conn, ticker)


def safe_ensure(db_path, ticker, symbol=None):
    """ensure() on its own connection (the page's is read-only), looking up the issuer's CIK from
    what we already hold. Anything that goes wrong returns None."""
    from . import db
    try:
        conn = db.connect(db_path)
        try:
            row = conn.execute("SELECT issuer_cik FROM insider_tx WHERE ticker=? AND issuer_cik<>'' LIMIT 1",
                               (ticker,)).fetchone()
            return ensure(conn, ticker, symbol, row[0] if row else None)
        finally:
            conn.close()
    except Exception as exc:
        log.warning("profile for %s unavailable: %s", ticker, exc)
        return None
