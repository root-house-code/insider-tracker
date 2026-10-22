"""Daily price history for the chart page, cached in SQLite.

Source: Yahoo Finance's chart endpoint. It is unofficial and keyless: no sign-up, but also
no promise it keeps working, so every failure here degrades to "no chart for this ticker"
rather than an error page. tests/fixtures/yahoo_chart_example.json has the exact structure of a
real response (with invented numbers, since Yahoo's data can't be redistributed) and is the
contract parse_chart() has to keep honouring.

Prices are fetched the first time a ticker's chart is opened and topped up afterwards. Bars
are keyed by Yahoo *symbol*, not by the ticker string in our tables: 'LEN', 'LEN, LEN.B' and
'LEN/B' are three spellings of two symbols, and share what they can.
"""
import datetime as dt
import logging
import re

import requests

from .net import PoliteSession

log = logging.getLogger(__name__)
URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
UA = "Mozilla/5.0 (compatible; insider-tracker)"
FRESH_OK = dt.timedelta(hours=6)       # don't re-ask Yahoo about a ticker more often than this
RETRY_NONE = dt.timedelta(days=7)      # Yahoo said it has nothing; don't hammer
RETRY_ERROR = dt.timedelta(minutes=10)  # we couldn't reach it; try again soon


class NotFound(Exception):
    """Yahoo has no such symbol."""


def candidates(ticker: str) -> list:
    """Yahoo symbols a ticker string could mean, best first. Our tables hold tickers as filings
    wrote them -- 'BRK.B', 'LEN/B', 'HEI, HEI.A', 'N O G' -- and Yahoo wants 'BRK-B'."""
    out = []
    for part in re.split(r"[,;]", ticker or ""):
        s = part.strip().upper()
        if re.fullmatch(r"(?:[A-Z] )+[A-Z]", s):          # "N O G" -> NOG
            s = s.replace(" ", "")
        s = re.sub(r"[./](?=[A-Z]{1,2}$)", "-", s)         # BRK.B, LEN/B -> BRK-B, LEN-B
        if re.fullmatch(r"[A-Z0-9]{1,6}(-[A-Z]{1,2})?", s) and s not in out:
            out.append(s)
    return out


def parse_chart(payload: dict) -> list:
    """Yahoo's chart JSON -> [{date, open, high, low, close, adj_close, volume}], skipping
    days with no close (holidays Yahoo pads, or a bar still forming)."""
    chart = payload.get("chart") or {}
    if chart.get("error") or not chart.get("result"):
        return []
    res = chart["result"][0]
    stamps = res.get("timestamp") or []
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    adj = ((res.get("indicators", {}).get("adjclose") or [{}])[0]).get("adjclose") or []
    # Timestamps are the session open in UTC; the exchange's offset gives its calendar date.
    offset = int((res.get("meta") or {}).get("gmtoffset") or 0)
    rows = []
    for i, ts in enumerate(stamps):
        close = (q.get("close") or [None] * len(stamps))[i]
        if close is None:
            continue
        day = (dt.datetime.fromtimestamp(ts, dt.timezone.utc) + dt.timedelta(seconds=offset)).date()
        pick = lambda k: (q.get(k) or [None] * len(stamps))[i]
        rows.append({"date": day.isoformat(), "open": pick("open"), "high": pick("high"),
                     "low": pick("low"), "close": close,
                     "adj_close": adj[i] if i < len(adj) and adj[i] is not None else close,
                     "volume": pick("volume")})
    return rows


def _fetch(http, symbol: str, last_date):
    params = {"interval": "1d"}
    if last_date:  # top up: overlap by a week so a restated recent bar gets corrected
        start = dt.date.fromisoformat(last_date) - dt.timedelta(days=7)
        params["period1"] = int(dt.datetime(start.year, start.month, start.day,
                                            tzinfo=dt.timezone.utc).timestamp())
        params["period2"] = int((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).timestamp())
    else:
        params["range"] = "5y"
    resp = http.get(URL.format(symbol=symbol), params=params)
    if resp.status_code == 404:
        raise NotFound(symbol)
    resp.raise_for_status()
    return parse_chart(resp.json())


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _set_meta(conn, ticker, symbol, status, note, now):
    conn.execute("INSERT OR REPLACE INTO price_meta (ticker, symbol, status, note, fetched_at) "
                 "VALUES (?,?,?,?,?)", (ticker, symbol, status, note, now.isoformat(timespec="seconds")))
    conn.commit()


def get_meta(conn, ticker):
    r = conn.execute("SELECT symbol, status, note, fetched_at FROM price_meta WHERE ticker=?",
                     (ticker,)).fetchone()
    return dict(zip(("symbol", "status", "note", "fetched_at"), r)) if r else None


def ensure(conn, ticker: str, http=None, now=None) -> dict:
    """Make sure prices for `ticker` are cached and reasonably fresh; returns its price_meta.
    Never raises: a price problem must not stop the chart page (or anything else) rendering."""
    now = now or _now()
    meta = get_meta(conn, ticker)
    if meta:
        age = now - dt.datetime.fromisoformat(meta["fetched_at"])
        limit = {"ok": FRESH_OK, "none": RETRY_NONE, "error": RETRY_ERROR}.get(meta["status"])
        if limit is not None and age < limit:
            return meta
    syms = candidates(ticker)
    if not syms:
        _set_meta(conn, ticker, None, "none", f"{ticker!r} isn't a plain ticker symbol", now)
        return get_meta(conn, ticker)
    http = http or PoliteSession(UA, 0.4, max_retries=1, timeout=15)
    known = meta["symbol"] if meta and meta["status"] == "ok" and meta["symbol"] else None
    for sym in ([known] if known else syms):
        last = conn.execute("SELECT MAX(date) FROM prices WHERE symbol=?", (sym,)).fetchone()[0]
        try:
            rows = _fetch(http, sym, last)
        except NotFound:
            continue
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning("price fetch for %s failed: %s", sym, exc)
            _set_meta(conn, ticker, known or sym, "error", f"{type(exc).__name__}: {exc}"[:200], now)
            return get_meta(conn, ticker)
        if not rows and not last:
            continue
        conn.executemany(
            "INSERT OR REPLACE INTO prices (symbol, date, open, high, low, close, adj_close, volume) "
            "VALUES (:symbol, :date, :open, :high, :low, :close, :adj_close, :volume)",
            [{**r, "symbol": sym} for r in rows])
        _set_meta(conn, ticker, sym, "ok", None, now)
        return get_meta(conn, ticker)
    _set_meta(conn, ticker, None, "none", f"Yahoo has no price history for {', '.join(syms)}", now)
    return get_meta(conn, ticker)


def stale(conn, tickers, now=None) -> list:
    """Tickers whose cached prices are missing or past their freshness window (ensure() would fetch)."""
    now = now or _now()
    out = []
    for t in dict.fromkeys(tickers):
        meta = get_meta(conn, t)
        limit = {"ok": FRESH_OK, "none": RETRY_NONE, "error": RETRY_ERROR}.get(meta["status"]) if meta else None
        if not meta or limit is None or now - dt.datetime.fromisoformat(meta["fetched_at"]) >= limit:
            out.append(t)
    return out


def refresh_in_background(db_path, tickers) -> bool:
    """ensure() each ticker on a background thread, sharing one session. True if a fill is running now."""
    from . import background
    if not tickers:
        return background.running("prices")

    def fill(conn):
        http = PoliteSession(UA, 0.4, max_retries=1, timeout=15)
        for t in tickers:
            ensure(conn, t, http)
    return background.start_db("prices", db_path, fill)


def safe_ensure(db_path, ticker, http=None) -> dict:
    """ensure() on its own connection, for callers whose own connection is read-only.
    Anything that goes wrong comes back as an 'error' status, never as an exception."""
    from . import db
    try:
        conn = db.connect(db_path)
        try:
            return ensure(conn, ticker, http)
        finally:
            conn.close()
    except Exception as exc:  # e.g. database locked by an ingest
        return {"symbol": None, "status": "error", "note": f"{type(exc).__name__}: {exc}"[:200],
                "fetched_at": None}


def bars(conn, symbol, since_iso):
    return [dict(r) for r in conn.execute(
        "SELECT date, open, high, low, close, adj_close, volume FROM prices "
        "WHERE symbol=? AND date>=? ORDER BY date", (symbol, since_iso))]
