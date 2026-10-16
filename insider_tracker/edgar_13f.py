"""SEC 13F-HR ingestion for a watchlist of institutional managers.

13F is quarterly, filed up to 45 days after quarter end, long positions
only. Useful for "what do these funds own and how did it change", not
for timely signals.
"""
import logging
import xml.etree.ElementTree as ET

from . import cusip_map, db
from .edgar_common import ARCHIVES, strip_ns
from .util import to_num

log = logging.getLogger(__name__)
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
# Filings made on/after 2023-01-03 report <value> in dollars; before that, thousands.
DOLLAR_VALUE_CUTOVER = "2023-01-03"


def list_13f_filings(http, cik: str, limit: int = 4):
    resp = http.get(SUBMISSIONS.format(cik=int(cik)))
    resp.raise_for_status()
    j = resp.json()
    recent = j["filings"]["recent"]
    out = []
    for form, acc, filed, period in zip(recent["form"], recent["accessionNumber"],
                                        recent["filingDate"], recent["reportDate"]):
        if form == "13F-HR":  # amendments (13F-HR/A) are skipped; see README
            out.append({"accession": acc, "filed_date": filed, "period": period})
    return j.get("name", ""), out[:limit]


def find_infotable_url(http, cik: str, accession: str):
    folder = f"{ARCHIVES}/edgar/data/{int(cik)}/{accession.replace('-', '')}"
    resp = http.get(f"{folder}/index.json")
    resp.raise_for_status()
    items = resp.json()["directory"]["item"]
    xmls = [i for i in items if i["name"].lower().endswith(".xml") and i["name"] != "primary_doc.xml"]
    if not xmls:
        return None
    best = max(xmls, key=lambda i: int(i.get("size") or 0))
    return f"{folder}/{best['name']}"


def parse_infotable(xml_bytes: bytes, values_in_thousands: bool) -> list:
    root = strip_ns(ET.fromstring(xml_bytes))
    agg = {}
    for it in root.iter("infoTable"):
        cusip = (it.findtext("cusip") or "").strip().upper()
        put_call = (it.findtext("putCall") or "").strip()
        value = to_num(it.findtext("value")) or 0.0
        if values_in_thousands:
            value *= 1000
        shares = to_num(it.findtext("shrsOrPrnAmt/sshPrnamt")) or 0.0
        key = (cusip, put_call)
        # The same security often appears on multiple lines (split by
        # sub-manager or voting authority); sum them.
        row = agg.setdefault(key, {
            "cusip": cusip, "put_call": put_call,
            "issuer": (it.findtext("nameOfIssuer") or "").strip(),
            "title_class": (it.findtext("titleOfClass") or "").strip(),
            "share_type": (it.findtext("shrsOrPrnAmt/sshPrnamtType") or "").strip(),
            "value_usd": 0.0, "shares": 0.0,
        })
        row["value_usd"] += value
        row["shares"] += shares
    return list(agg.values())


def ingest(conn, http, managers: dict, quarters: int = 4) -> int:
    """managers: {cik: display_name}. Fetches the latest N quarterly filings each."""
    done = db.processed_ids(conn, "13f")
    stored = 0
    for cik, label in managers.items():
        try:
            name, filings = list_13f_filings(http, cik, limit=quarters)
        except Exception as exc:
            log.warning("13F: could not list filings for %s (%s): %s", label, cik, exc)
            continue
        for f in filings:
            if f["accession"] in done:
                continue
            try:
                url = find_infotable_url(http, cik, f["accession"])
                if not url:
                    db.mark(conn, "13f", f["accession"], "no_infotable")
                    continue
                resp = http.get(url)
                resp.raise_for_status()
                holdings = parse_infotable(resp.content, f["filed_date"] < DOLLAR_VALUE_CUTOVER)
                rows = [{**h, "accession": f["accession"], "manager_cik": str(int(cik)),
                         "manager_name": label or name, "period": f["period"],
                         "filed_date": f["filed_date"]} for h in holdings]
                stored += db.insert_rows(conn, "holdings_13f", rows)
                db.mark(conn, "13f", f["accession"], "ok")
                log.info("13F %s %s: %d positions", label or name, f["period"], len(rows))
            except Exception as exc:
                log.warning("13F %s failed: %s", f["accession"], exc)
                db.mark(conn, "13f", f["accession"], "error", str(exc)[:500])
        conn.commit()
    cusip_map.resolve(conn)  # also catches CUSIPs left unmapped by an earlier failed run
    return stored
