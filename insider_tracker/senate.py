"""Senate Periodic Transaction Reports via efdsearch.senate.gov.

The site requires accepting a usage agreement (a CSRF-protected form)
before search works. Electronic PTRs render as HTML tables; paper PTRs are
scanned images and are flagged 'needs_manual'.

This site changes occasionally and is the most likely thing here to break.
If it does, the fix is usually in the form field names below.
"""
import datetime as dt
import logging
import re

from bs4 import BeautifulSoup

from . import db
from .util import mdy_to_iso, parse_amount_range

log = logging.getLogger(__name__)
BASE = "https://efdsearch.senate.gov"
PTR_REPORT_TYPE = "[11]"
UUID_RE = re.compile(r"/search/view/(ptr|paper)/([0-9a-fA-F\-]{36})/")

# eFD leaves the Ticker column as "--" for a lot of securities -- ADRs above all --
# while still naming the ticker in the asset text: "Zurich Insurance Group Ltd
# Sponsored ADR (ZURVY)", or "EA - Electronic Arts Inc". Recovering those is the
# difference between a third of Senate rows reaching the ticker-level signals and
# being dropped on the floor. Both patterns demand upper-case throughout, which is
# what keeps "(The)" and "(Exchanged)" out; NOT_TICKERS covers the rest.
TICKER_IN_NAME = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,6})\)")
TICKER_PREFIX = re.compile(r"^([A-Z][A-Z0-9.\-]{0,6})\s+-\s+")
NOT_TICKERS = {"ADR", "ETF", "ETN", "REIT", "LLC", "LP", "INC", "PLC", "LTD",
               "IRA", "USD", "NA", "THE", "CO", "SA", "AG", "NV", "CD", "NR"}


def ticker_from_name(asset: str) -> str:
    """Pull a ticker out of the asset text when the Ticker column is empty."""
    for m in TICKER_IN_NAME.finditer(asset or ""):
        if m.group(1) not in NOT_TICKERS:
            return m.group(1)
    m = TICKER_PREFIX.match(asset or "")
    if m and m.group(1) not in NOT_TICKERS:
        return m.group(1)
    return ""


class SenateClient:
    def __init__(self, http):
        self.http = http

    def _csrf(self):
        return self.http.session.cookies.get("csrftoken", "")

    def accept_terms(self):
        r = self.http.get(f"{BASE}/search/home/")
        r.raise_for_status()
        m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r.text)
        if not m:
            raise RuntimeError("Senate eFD: could not find CSRF token on the agreement page")
        r = self.http.post(f"{BASE}/search/home/",
                           data={"prohibition_agreement": "1", "csrfmiddlewaretoken": m.group(1)},
                           headers={"Referer": f"{BASE}/search/home/"})
        r.raise_for_status()

    def search_ptrs(self, since: dt.date, page_size: int = 100) -> list:
        self.accept_terms()
        out, start = [], 0
        while True:
            r = self.http.post(f"{BASE}/search/report/data/", data={
                "start": str(start), "length": str(page_size),
                "report_types": PTR_REPORT_TYPE, "filer_types": "[]",
                "submitted_start_date": since.strftime("%m/%d/%Y 00:00:00"),
                "submitted_end_date": "", "candidate_state": "", "senator_state": "",
                "office_id": "", "first_name": "", "last_name": "",
                "csrfmiddlewaretoken": self._csrf(),
            }, headers={"Referer": f"{BASE}/search/", "X-CSRFToken": self._csrf()})
            r.raise_for_status()
            payload = r.json()
            data = payload.get("data", [])
            for row in data:
                first, last, _office, link_html, filed = (row + [""] * 5)[:5]
                m = UUID_RE.search(link_html or "")
                if not m:
                    continue
                out.append({
                    "doc_id": m.group(2), "kind": m.group(1),
                    "member": f"{first} {last}".strip(),
                    "filed_date": mdy_to_iso(filed),
                    "url": f"{BASE}{m.group(0)}",
                })
            start += page_size
            if not data or start >= int(payload.get("recordsTotal", 0)):
                break
        return out

    def fetch_ptr(self, url: str) -> str:
        r = self.http.get(url, headers={"Referer": f"{BASE}/search/"})
        if r.url.rstrip("/").endswith("/search/home"):  # session lapsed
            self.accept_terms()
            r = self.http.get(url, headers={"Referer": f"{BASE}/search/"})
        r.raise_for_status()
        return r.text


def _norm_type(t: str) -> str:
    t = t.lower()
    if t.startswith("purchase"):
        return "P"
    if t.startswith("sale"):
        return "S"
    if t.startswith("exchange"):
        return "E"
    return t[:1].upper()


def parse_ptr_html(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []
    headers = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]

    def col(rec, *keys):
        for k in keys:  # exact header match first ("type" vs "asset type")
            if k in rec:
                return rec[k]
        for h, v in rec.items():
            if any(k in h for k in keys):
                return v
        return ""

    rows = []
    for tr in table.select("tbody tr"):
        rec = dict(zip(headers, (td.get_text(" ", strip=True) for td in tr.find_all("td"))))
        if not rec:
            continue
        ticker = col(rec, "ticker")
        ticker = "" if ticker in ("--", "N/A") else ticker.upper()
        asset = col(rec, "asset name")
        quality = "ok" if ticker else "no_ticker"
        if not ticker:
            ticker = ticker_from_name(asset)
            if ticker:
                # Flagged separately from a ticker the site stated outright, so
                # anyone can tell an inferred symbol from a reported one.
                quality = "name_ticker"
        amount = col(rec, "amount")
        low, high = parse_amount_range(amount)
        rows.append({
            "trans_date": mdy_to_iso(col(rec, "transaction date")),
            "owner": col(rec, "owner") or "Self",
            "ticker": ticker, "asset": asset,
            "asset_type": col(rec, "asset type"),
            "tx_type": _norm_type(col(rec, "type")),
            "amount_text": amount, "amount_low": low, "amount_high": high,
            "parse_quality": quality,
        })
    return rows


def ingest(conn, http, since: dt.date) -> int:
    client = SenateClient(http)
    done = db.processed_ids(conn, "senate")
    filings = [f for f in client.search_ptrs(since) if f["doc_id"] not in done]
    log.info("Senate: %d new PTRs since %s", len(filings), since)
    stored = 0
    for f in filings:
        if f["kind"] == "paper":
            db.mark(conn, "senate", f["doc_id"], "needs_manual", f"paper filing: {f['url']}")
            continue
        try:
            rows = parse_ptr_html(client.fetch_ptr(f["url"]))
            out = [{**r, "chamber": "Senate", "doc_id": f["doc_id"], "seq": k, "member": f["member"],
                    "state_district": "", "filed_date": f["filed_date"], "url": f["url"]}
                   for k, r in enumerate(rows)]
            stored += db.insert_rows(conn, "congress_tx", out)
            db.mark(conn, "senate", f["doc_id"], "ok" if rows else "no_transactions")
        except Exception as exc:
            log.warning("Senate PTR %s failed: %s", f["doc_id"], exc)
            db.mark(conn, "senate", f["doc_id"], "error", str(exc)[:500])
        conn.commit()
    return stored
