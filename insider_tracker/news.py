"""Recent news per ticker, to put insider buying in context, cached in SQLite.

Two sources, labelled in news_items.kind:

  headline  Yahoo Finance's search endpoint (keyless, unofficial): the latest ~10 stories naming
            the ticker, with publisher and link. Only goes back a week or two, so items are kept
            as they arrive rather than replaced -- that is what lets a cluster show the news from
            before its first buy.
  8k        The company's own 8-K filings from the SEC (official): earnings, an executive leaving,
            a deal signed. An 8-K just before a cluster often explains it.

Every failure degrades to "no news", never to an error page. Titles come from third parties, so
they are escaped wherever they are shown, and only http(s) links are ever linked.
"""
import datetime as dt
import logging

import requests

from . import background, config, db, prices
from .net import PoliteSession
from .profiles import UA

log = logging.getLogger(__name__)

YAHOO_SEARCH = "https://query1.finance.yahoo.com/v1/finance/search"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_DOC = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
FRESH = {"ok": dt.timedelta(hours=6), "none": dt.timedelta(days=1), "error": dt.timedelta(minutes=10)}
EIGHT_K_DAYS = 180  # how far back 8-K filings are kept

# 8-K item numbers, in words. 9.01 (exhibits) accompanies almost every 8-K and says nothing.
ITEMS = {
    "1.01": "material agreement", "1.02": "agreement terminated", "1.03": "bankruptcy",
    "1.05": "cybersecurity incident", "2.01": "acquisition or sale completed", "2.02": "results",
    "2.03": "new debt", "2.04": "debt accelerated", "2.05": "restructuring", "2.06": "impairment",
    "3.01": "delisting notice", "3.02": "unregistered share sale", "3.03": "shareholder rights changed",
    "4.01": "auditor change", "4.02": "past financials unreliable", "5.01": "change in control",
    "5.02": "director or officer change", "5.03": "bylaws or fiscal year change",
    "5.07": "shareholder vote", "7.01": "investor disclosure", "8.01": "other event",
}

def parse_yahoo(payload: dict, symbol: str) -> list:
    """Yahoo search JSON -> headline items about `symbol` (stories tagged with other tickers only
    are dropped: a search for a short symbol can match unrelated news)."""
    out = []
    for n in (payload or {}).get("news") or []:
        title, link, when = (n.get("title") or "").strip(), n.get("link") or "", n.get("providerPublishTime")
        if not (title and when and link.startswith(("http://", "https://"))):
            continue
        related = [t.upper() for t in n.get("relatedTickers") or []]
        if related and symbol.upper() not in related:
            continue
        out.append({"kind": "headline", "title": title, "source": n.get("publisher") or "",
                    "url": link, "published": dt.datetime.fromtimestamp(int(when), dt.timezone.utc)
                    .isoformat(timespec="minutes")})
    return out


def parse_8k(payload: dict, since: str) -> list:
    """SEC submissions JSON -> 8-K items filed on or after `since`, each titled by what it reports."""
    rec = ((payload or {}).get("filings") or {}).get("recent") or {}
    cik = str(int(payload.get("cik") or 0))
    out = []
    for i, form in enumerate(rec.get("form") or []):
        filed = rec["filingDate"][i]
        if form not in ("8-K", "8-K/A") or filed < since:
            continue
        codes = [c.strip() for c in (rec.get("items") or [""] * (i + 1))[i].split(",") if c.strip()]
        words = [ITEMS[c] for c in codes if c in ITEMS] or ["filing"]
        acc = rec["accessionNumber"][i]
        out.append({"kind": "8k", "title": f"{form}: {', '.join(words)}", "source": "SEC",
                    "url": ARCHIVE_DOC.format(cik=cik, acc=acc.replace("-", ""), doc=rec["primaryDocument"][i]),
                    "published": filed})
    return out


def get(conn, ticker, since=None, limit=15) -> list:
    """Cached items for `ticker`, newest first."""
    return [dict(r) for r in conn.execute(
        "SELECT kind, published, title, source, url FROM news_items WHERE ticker=? AND published>=? "
        "ORDER BY published DESC LIMIT ?", (ticker, since or "", limit))]


def get_many(conn, tickers, since=None, limit=15) -> dict:
    return {t: get(conn, t, since, limit) for t in dict.fromkeys(tickers)}


def stale(conn, tickers, now=None) -> list:
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for t in dict.fromkeys(tickers):
        m = conn.execute("SELECT status, fetched_at FROM news_meta WHERE ticker=?", (t,)).fetchone()
        if not m or now - dt.datetime.fromisoformat(m[1]) >= FRESH.get(m[0], dt.timedelta(0)):
            out.append(t)
    return out


def _cik(conn, ticker):
    r = conn.execute("SELECT issuer_cik FROM insider_tx WHERE ticker=? AND issuer_cik<>'' LIMIT 1",
                     (ticker,)).fetchone()
    return int(r[0]) if r and str(r[0]).isdigit() else None


def fetch_many(conn, tickers, yahoo_http=None, sec_http=None, now=None) -> int:
    """Fetch and cache news for `tickers`. Never raises; returns how many got any items."""
    now = now or dt.datetime.now(dt.timezone.utc)
    yahoo_http = yahoo_http or PoliteSession(UA, 0.4, max_retries=1, timeout=15)
    if sec_http is None:
        try:
            sec_http = PoliteSession(config.sec_user_agent(), config.SEC_MIN_INTERVAL, max_retries=1, timeout=15)
        except SystemExit:  # no SEC identity configured: headlines only
            sec_http = None
    since_8k = (now - dt.timedelta(days=EIGHT_K_DAYS)).date().isoformat()
    got = 0
    for ticker in tickers:
        items, problem = [], None
        for sym in prices.candidates(ticker)[:1]:
            try:
                r = yahoo_http.get(YAHOO_SEARCH, params={"q": sym, "newsCount": 10, "quotesCount": 0,
                                                         "listsCount": 0})
                r.raise_for_status()
                items += parse_yahoo(r.json(), sym)
            except (requests.RequestException, ValueError) as exc:
                problem = f"Yahoo: {type(exc).__name__}: {exc}"[:200]
        cik = _cik(conn, ticker)
        if sec_http is not None and cik:
            try:
                r = sec_http.get(SEC_SUBMISSIONS.format(cik=cik))
                if r.status_code != 404:
                    r.raise_for_status()
                    items += parse_8k(r.json(), since_8k)
            except (requests.RequestException, ValueError, KeyError) as exc:
                problem = problem or f"SEC: {type(exc).__name__}: {exc}"[:200]
        conn.executemany(
            "INSERT OR IGNORE INTO news_items (ticker, kind, published, title, source, url) VALUES (?,?,?,?,?,?)",
            [(ticker, i["kind"], i["published"], i["title"], i["source"], i["url"]) for i in items])
        status = "ok" if items else ("error" if problem else "none")
        conn.execute("INSERT OR REPLACE INTO news_meta (ticker, status, note, fetched_at) VALUES (?,?,?,?)",
                     (ticker, status, problem, now.isoformat(timespec="seconds")))
        conn.commit()
        got += bool(items)
    return got


def refresh_in_background(db_path, tickers) -> bool:
    """fetch_many on a background thread. True if a fetch is running now."""
    if not tickers:
        return background.running("news")
    return background.start_db("news", db_path, lambda conn: fetch_many(conn, list(tickers)))


def safe_ensure(db_path, ticker):
    """Bring one ticker's news up to date on its own connection (the chart page). Never raises."""
    try:
        conn = db.connect(db_path)
        try:
            if stale(conn, [ticker]):
                fetch_many(conn, [ticker])
        finally:
            conn.close()
    except Exception as exc:
        log.warning("news for %s unavailable: %s", ticker, exc)
