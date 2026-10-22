"""Market cap and average daily volume per ticker, for the cluster-buys table, cached in SQLite.

Source: Yahoo Finance's quoteSummary (summaryDetail + price modules) -- the same unofficial,
cookie-and-crumb endpoint as profiles.py, and just as likely to change without notice, so every
failure degrades to blank cells. Both numbers move daily, so a cached value is good for a day.

A cluster table can list dozens of tickers, far too many to fetch while a page waits. The page
shows what is cached and hands the rest to refresh_in_background(), one thread at a time, which
reuses a single handshake for the whole batch.
"""
import datetime as dt
import logging

import requests

from . import background, prices
from .net import PoliteSession
from .profiles import UA, YAHOO_COOKIE, YAHOO_CRUMB, YAHOO_SUMMARY

log = logging.getLogger(__name__)

FRESH = {"ok": dt.timedelta(days=1), "none": dt.timedelta(days=7), "error": dt.timedelta(minutes=10)}


def parse_quote(payload: dict):
    """quoteSummary JSON -> {market_cap, avg_volume}, or None if it has neither."""
    result = ((payload.get("quoteSummary") or {}).get("result") or [None])[0] or {}
    raw = lambda mod, key: ((result.get(mod) or {}).get(key) or {}).get("raw")
    cap = raw("summaryDetail", "marketCap") or raw("price", "marketCap")
    vol = (raw("summaryDetail", "averageVolume") or raw("price", "averageDailyVolume3Month")
           or raw("summaryDetail", "averageDailyVolume10Day"))
    if not cap and not vol:
        return None
    return {"market_cap": cap, "avg_volume": vol}


def get_many(conn, tickers) -> dict:
    tickers = list(tickers)
    if not tickers:
        return {}
    marks = ",".join("?" * len(tickers))
    return {r["ticker"]: dict(r) for r in conn.execute(
        f"SELECT * FROM quotes WHERE ticker IN ({marks})", tickers)}


def stale(conn, tickers, now=None) -> list:
    """Tickers with no cached quote, or one past its freshness window."""
    now = now or dt.datetime.now(dt.timezone.utc)
    have = get_many(conn, tickers)
    out = []
    for t in dict.fromkeys(tickers):
        q = have.get(t)
        if not q or now - dt.datetime.fromisoformat(q["fetched_at"]) >= FRESH.get(q["status"], dt.timedelta(0)):
            out.append(t)
    return out


def _store(conn, ticker, now, status, symbol=None, quote=None, note=None):
    q = quote or {}
    conn.execute("INSERT OR REPLACE INTO quotes (ticker, symbol, market_cap, avg_volume, status, note, "
                 "fetched_at) VALUES (?,?,?,?,?,?,?)",
                 (ticker, symbol, q.get("market_cap"), q.get("avg_volume"), status, note,
                  now.isoformat(timespec="seconds")))
    conn.commit()


def fetch_many(conn, tickers, http=None, now=None) -> int:
    """Fetch and cache quotes for `tickers` (each tried under every Yahoo spelling it could have).
    Never raises; returns how many came back with numbers."""
    now = now or dt.datetime.now(dt.timezone.utc)
    http = http or PoliteSession(UA, 0.4, max_retries=1, timeout=15)
    try:
        http.get(YAHOO_COOKIE)  # answers 404, but sets the cookie the crumb is tied to
        crumb = http.get(YAHOO_CRUMB)
        if crumb.status_code != 200 or not crumb.text or "<" in crumb.text:
            raise requests.RequestException(f"Yahoo gave no crumb (HTTP {crumb.status_code})")
    except requests.RequestException as exc:
        note = f"{type(exc).__name__}: {exc}"[:200]
        log.warning("quotes unavailable: %s", note)
        for t in tickers:
            _store(conn, t, now, "error", note=note)
        return 0
    got = 0
    for ticker in tickers:
        quote, symbol, problem = None, None, None
        for sym in prices.candidates(ticker):
            try:
                resp = http.get(YAHOO_SUMMARY.format(symbol=sym),
                                params={"modules": "summaryDetail,price", "crumb": crumb.text})
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                quote = parse_quote(resp.json())
            except (requests.RequestException, ValueError) as exc:
                problem = f"{type(exc).__name__}: {exc}"[:200]
                break
            if quote:
                symbol = sym
                break
        if quote:
            _store(conn, ticker, now, "ok", symbol, quote)
            got += 1
        elif problem:
            _store(conn, ticker, now, "error", note=problem)
        else:
            _store(conn, ticker, now, "none", note="Yahoo has no market data for this ticker")
    return got


def refresh_in_background(db_path, tickers) -> bool:
    """fetch_many on a background thread. True if a fetch is running now (this one, or an earlier
    one still going, which the caller's next page view will pick up after)."""
    if not tickers:
        return background.running("quotes")
    return background.start_db("quotes", db_path, lambda conn: fetch_many(conn, list(tickers)))
