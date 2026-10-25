"""SEC Form 4 ingestion (insider transactions).

Discovery uses EDGAR's daily master index (pipe-delimited, one line per
filer, so each Form 4 appears once for the issuer and once per reporting
owner; we dedupe by accession number). Each filing's full-submission .txt
contains the ownershipDocument XML, so it's one request per filing.
"""
import datetime as dt
import logging
import re
import xml.etree.ElementTree as ET

from . import db
from .edgar_common import ARCHIVES, strip_ns
from .util import daterange, to_num

log = logging.getLogger(__name__)
FORMS = {"4", "4/A"}
OWNERSHIP_RE = re.compile(r"<ownershipDocument>.*?</ownershipDocument>", re.S)
PLAN_RE = re.compile(r"10b5-?1", re.I)
HOLDINGS_LOOKBACK_DAYS = 90  # each ingest fills in holding rows for purchase filings this far back


def daily_index_url(day: dt.date) -> str:
    q = (day.month - 1) // 3 + 1
    return f"{ARCHIVES}/edgar/daily-index/{day.year}/QTR{q}/master.{day:%Y%m%d}.idx"


def list_form4_filings(http, day: dt.date) -> list:
    resp = http.get(daily_index_url(day))
    if resp.status_code in (403, 404):  # holiday, or index not published yet
        return []
    resp.raise_for_status()
    filings = {}
    for line in resp.text.splitlines():
        parts = line.split("|")
        if len(parts) != 5 or parts[2] not in FORMS:
            continue
        cik, _name, form, date_filed, path = parts
        acc = path.rsplit("/", 1)[-1].removesuffix(".txt")
        f = filings.setdefault(acc, {
            "accession": acc, "form_type": form, "path": path, "ciks": set(),
            "filed_date": f"{date_filed[:4]}-{date_filed[4:6]}-{date_filed[6:8]}",
        })
        f["ciks"].add(str(int(cik)))
    return list(filings.values())


def fetch_ownership_xml(http, path: str):
    resp = http.get(f"{ARCHIVES}/{path}")
    resp.raise_for_status()
    m = OWNERSHIP_RE.search(resp.text)
    return m.group(0) if m else None


# ---------- parsing ----------

def _val(el, path):
    """Form 4 wraps most values as <x><value>...</value></x>; some are bare."""
    if el is None:
        return None
    node = el.find(path)
    if node is None:
        return None
    v = node.find("value")
    text = (v.text if v is not None else node.text) or ""
    return text.strip() or None


def _flag(el, path) -> int:
    v = _val(el, path)
    return 1 if v and v.lower() in ("1", "true") else 0


def parse_form4(xml_text: str, accession: str, form_type: str, filed_date: str) -> list:
    root = strip_ns(ET.fromstring(xml_text))
    issuer = root.find("issuer")
    issuer_cik = str(int(_val(issuer, "issuerCik") or 0))
    ticker = (_val(issuer, "issuerTradingSymbol") or "").upper()
    if ticker in ("NONE", "N/A", "NA", "--"):
        ticker = ""

    names, ciks, rel = [], [], []
    flags = {"is_director": 0, "is_officer": 0, "is_ten_pct": 0}
    for owner in root.findall("reportingOwner"):
        names.append(_val(owner, "reportingOwnerId/rptOwnerName") or "?")
        ciks.append(_val(owner, "reportingOwnerId/rptOwnerCik") or "")
        r = owner.find("reportingOwnerRelationship")
        if _flag(r, "isDirector"):
            flags["is_director"] = 1
            rel.append("Director")
        if _flag(r, "isOfficer"):
            flags["is_officer"] = 1
            rel.append(_val(r, "officerTitle") or "Officer")
        if _flag(r, "isTenPercentOwner"):
            flags["is_ten_pct"] = 1
            rel.append("10% Owner")
        if _flag(r, "isOther"):
            rel.append(_val(r, "otherText") or "Other")

    # Since April 2023 the form has a filing-level 10b5-1 checkbox. Older
    # filings disclose plan trades only in footnotes, so check those too.
    doc_plan = _flag(root, "aff10b5One")
    footnotes = {fn.get("id"): "".join(fn.itertext()) for fn in root.iter("footnote") if fn.get("id")}

    url = f"{ARCHIVES}/edgar/data/{issuer_cik}/{accession.replace('-', '')}/{accession}-index.htm"
    base = {
        "accession": accession, "form_type": form_type, "filed_date": filed_date,
        "issuer_cik": issuer_cik, "issuer_name": _val(issuer, "issuerName"), "ticker": ticker,
        "owner_cik": "; ".join(ciks), "owner_name": "; ".join(names),
        "relationship": ", ".join(dict.fromkeys(rel)), **flags, "url": url,
    }

    rows, seq = [], 0
    for table, tag, is_deriv in (("nonDerivativeTable", "nonDerivativeTransaction", 0),
                                 ("derivativeTable", "derivativeTransaction", 1)):
        for tx in root.findall(f"{table}/{tag}"):
            fn_ids = {f.get("id") for f in tx.iter("footnoteId")}
            plan = doc_plan or any(PLAN_RE.search(footnotes.get(i, "")) for i in fn_ids)
            shares = to_num(_val(tx, "transactionAmounts/transactionShares"))
            price = to_num(_val(tx, "transactionAmounts/transactionPricePerShare"))
            rows.append({
                **base, "seq": seq,
                "trans_date": (_val(tx, "transactionDate") or "")[:10] or None,
                "security": _val(tx, "securityTitle"),
                "is_derivative": is_deriv,
                "code": _val(tx, "transactionCoding/transactionCode"),
                "acq_disp": _val(tx, "transactionAmounts/transactionAcquiredDisposedCode"),
                "shares": shares, "price": price,
                "value": round(shares * price, 2) if shares and price else None,
                "shares_after": to_num(_val(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
                "direct_indirect": _val(tx, "ownershipNature/directOrIndirectOwnership"),
                "plan_10b5_1": int(bool(plan)),
            })
            seq += 1
    return rows


def parse_holdings(xml_text: str, accession: str) -> list:
    """Table I "holding" rows: stock the owner reports holding, on a line that had no transaction
    in this filing (a trust, a spouse's account, shares held directly while buying via a fund).
    Without them, a purchase into one holding looks like the owner's whole position."""
    root = strip_ns(ET.fromstring(xml_text))
    return [{"accession": accession, "seq": seq, "security": _val(h, "securityTitle"),
             "shares": to_num(_val(h, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
             "direct_indirect": _val(h, "ownershipNature/directOrIndirectOwnership"),
             "nature": _val(h, "ownershipNature/natureOfOwnership")}
            for seq, h in enumerate(root.findall("nonDerivativeTable/nonDerivativeHolding"))]


def backfill_holdings(conn, http, since: str) -> int:
    """Fetch holding rows for purchase filings stored before parse_holdings existed (or whose
    fetch failed). Only filings with open-market purchases need them; returns how many were done."""
    done = db.processed_ids(conn, "form4_holdings")
    todo = [r[0] for r in conn.execute(
        "SELECT DISTINCT accession FROM insider_tx WHERE code='P' AND is_derivative=0 AND filed_date>=?",
        (since,)) if r[0] not in done]
    paths = {}
    for r in conn.execute("SELECT DISTINCT accession, url FROM insider_tx WHERE code='P' AND filed_date>=?", (since,)):
        # .../edgar/data/<cik>/<acc no dashes>/<acc>-index.htm -> edgar/data/<cik>/<acc>.txt
        parts = r[1].split("/edgar/data/", 1)[-1].split("/")
        paths[r[0]] = f"edgar/data/{parts[0]}/{r[0]}.txt"
    for i, acc in enumerate(todo, 1):
        try:
            xml_text = fetch_ownership_xml(http, paths[acc])
            if xml_text:
                db.insert_rows(conn, "insider_holdings", parse_holdings(xml_text, acc))
            db.mark(conn, "form4_holdings", acc, "ok")
        except Exception as exc:  # retried next time
            log.warning("form4 holdings %s failed: %s", acc, exc)
            db.mark(conn, "form4_holdings", acc, "error", str(exc)[:500])
        if i % 100 == 0:
            conn.commit()
            log.info("  holdings %d/%d", i, len(todo))
    conn.commit()
    return len(todo)


# ---------- runner ----------

def ingest(conn, http, start: dt.date, end: dt.date, issuer_ciks: set = None) -> int:
    """Fetch and store every Form 4 filed between start and end (inclusive).

    Pass issuer_ciks to restrict to a watchlist. An unfiltered business day
    is roughly a few hundred to a couple thousand filings, which takes a few
    minutes at the SEC rate limit.
    """
    done = db.processed_ids(conn, "form4")
    stored = 0
    for day in daterange(start, end):
        if day.weekday() >= 5:
            continue
        filings = [f for f in list_form4_filings(http, day) if f["accession"] not in done]
        if issuer_ciks:
            filings = [f for f in filings if f["ciks"] & issuer_ciks]
        log.info("%s: %d new Form 4 filings", day, len(filings))
        for i, f in enumerate(filings, 1):
            try:
                xml_text = fetch_ownership_xml(http, f["path"])
                if not xml_text:
                    db.mark(conn, "form4", f["accession"], "no_xml")
                    continue
                rows = parse_form4(xml_text, f["accession"], f["form_type"], f["filed_date"])
                stored += db.insert_rows(conn, "insider_tx", rows)
                db.insert_rows(conn, "insider_holdings", parse_holdings(xml_text, f["accession"]))
                db.mark(conn, "form4", f["accession"], "ok" if rows else "no_transactions")
                db.mark(conn, "form4_holdings", f["accession"], "ok")
            except Exception as exc:  # keep going; failures are retried next run
                log.warning("form4 %s failed: %s", f["accession"], exc)
                db.mark(conn, "form4", f["accession"], "error", str(exc)[:500])
            if i % 100 == 0:
                conn.commit()
                log.info("  %d/%d", i, len(filings))
        conn.commit()
    # purchase filings stored before holdings were parsed: the stake-change math needs them
    backfill_holdings(conn, http, (start - dt.timedelta(days=HOLDINGS_LOOKBACK_DAYS)).isoformat())
    from . import issuers  # fund or operating company, for the noise filters
    issuers.fill_buyers(conn, http)
    return stored
