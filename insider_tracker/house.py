"""House Periodic Transaction Reports (STOCK Act), via the Clerk's site.

The Clerk publishes a yearly ZIP with an XML index of every financial
disclosure filing. FilingType 'P' = Periodic Transaction Report. Each PTR
is a PDF. Electronically filed ones have extractable text; paper filings
are scanned images and get flagged 'needs_manual' (OCR is a TODO).

PDF table extraction is the weakest link in this whole project. The parser
is anchored on the one stable pattern in each row (type, two dates, amount)
and reconstructs the rest around it. Rows it can't find a ticker for are
kept but marked parse_quality='no_ticker'.
"""
import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile

from . import db
from .util import mdy_to_iso, parse_amount_range

log = logging.getLogger(__name__)
FD_ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PTR_PDF = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"

OWNER_CODES = {"SP": "Spouse", "JT": "Joint", "DC": "Dependent"}
# Clerk PDFs embed NUL bytes inside the row-terminating labels, so a marker
# reads "F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00: New" rather than "F S: New". Regex \s does
# not match NUL, so those markers slip past STOP and their text gets absorbed
# into the asset name. Strip control characters before anything else looks at it.
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
ANCHOR = re.compile(
    r"(?<![A-Za-z0-9])(?P<tx>S \(partial\)|P|S|E)\s+"
    r"(?P<tdate>\d{1,2}/\d{1,2}/\d{4})\s+(?P<ndate>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<amt>Over \$[\d,]+|\$[\d,]+(?:\s*-\s*(?:\$[\d,]+)?)?)"
)
# Lines that end a row's wrapped asset text (filing status, description, etc.)
STOP = re.compile(r"^\s*(?:F\s*S\s*:|S\s*O\s*:|D\s*:|C\s*:|L\s*:|\*|Filing ID|ID\s+Owner|"
                  r"I CERTIFY|Digitally Signed|Asset Class|Initial Public|Type\s+Date|Yes\b|No\b)", re.I)
WRAPPED_UPPER = re.compile(r"\$[\d,]+")
TICKER = re.compile(r"\(([A-Z][A-Z0-9.\-/]{0,9})\)")
ASSET_TYPE = re.compile(r"\[([A-Z]{2})\]")


def list_ptrs(http, year: int) -> list:
    resp = http.get(FD_ZIP.format(year=year))
    resp.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    xml_name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
    root = ET.fromstring(zf.read(xml_name))
    out = []
    for m in root.iter("Member"):
        if (m.findtext("FilingType") or "").strip() != "P":
            continue
        name = " ".join(p for p in ((m.findtext(t) or "").strip()
                                    for t in ("Prefix", "First", "Last", "Suffix")) if p)
        doc_id = (m.findtext("DocID") or "").strip()
        out.append({
            "doc_id": doc_id, "member": name,
            "state_district": (m.findtext("StateDst") or "").strip(),
            "filed_date": mdy_to_iso(m.findtext("FilingDate")),
            "url": PTR_PDF.format(year=year, doc_id=doc_id),
        })
    return out


def pdf_text(content: bytes) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        return CONTROL.sub("", "\n".join((p.extract_text() or "") for p in pdf.pages))


def parse_ptr_text(text: str) -> list:
    lines = [ln.strip() for ln in CONTROL.sub('', text).splitlines()]
    anchor_idx = [i for i, ln in enumerate(lines) if ANCHOR.search(ln)]
    rows = []
    for n, i in enumerate(anchor_idx):
        line = lines[i]
        m = ANCHOR.search(line)
        prefix = line[:m.start()].strip()
        amount = m.group("amt").strip()

        # Gather wrapped continuation lines until the next row or a stop marker.
        nxt = anchor_idx[n + 1] if n + 1 < len(anchor_idx) else len(lines)
        cont = []
        for ln in lines[i + 1:min(nxt, i + 5)]:
            if not ln or STOP.match(ln):
                break
            # A wrapped range ("$15,001 -") continues on a later line, often
            # after the wrapped ticker: "(ACME) [ST] $50,000".
            if amount.endswith("-"):
                mm = WRAPPED_UPPER.search(ln)
                if mm:
                    amount = f"{amount} {mm.group(0)}"
                    ln = (ln[:mm.start()] + ln[mm.end():]).strip()
                    if not ln:
                        continue
            cont.append(ln)

        owner = "Self"
        first, _, rest = prefix.partition(" ")
        if first in OWNER_CODES:
            owner, prefix = OWNER_CODES[first], rest
        asset_full = " ".join([prefix, *cont]).strip()
        tm = TICKER.search(asset_full)
        am = ASSET_TYPE.search(asset_full)
        asset_name = asset_full[:tm.start()].strip(" -") if tm else ASSET_TYPE.sub("", asset_full).strip()
        low, high = parse_amount_range(amount)
        tx = m.group("tx")
        rows.append({
            "owner": owner, "asset": asset_name or asset_full,
            "ticker": tm.group(1) if tm else "",
            "asset_type": am.group(1) if am else "",
            "tx_type": "S" if tx.startswith("S") else tx,
            "trans_date": mdy_to_iso(m.group("tdate")),
            "amount_text": amount, "amount_low": low, "amount_high": high,
            "parse_quality": "ok" if tm else "no_ticker",
        })
    return rows


def ingest(conn, http, year: int, since: str = None) -> int:
    done = db.processed_ids(conn, "house")
    filings = [f for f in list_ptrs(http, year) if f["doc_id"] not in done]
    if since:
        filings = [f for f in filings if (f["filed_date"] or "") >= since]
    log.info("House %d: %d new PTRs", year, len(filings))
    stored = 0
    for f in filings:
        try:
            resp = http.get(f["url"])
            resp.raise_for_status()
            text = pdf_text(resp.content)
            rows = parse_ptr_text(text)
            if not rows:
                status = "needs_manual" if len(text.strip()) < 200 else "no_transactions"
                db.mark(conn, "house", f["doc_id"], status, "scanned/paper filing" if status == "needs_manual" else None)
                continue
            out = [{**r, "chamber": "House", "doc_id": f["doc_id"], "seq": k, "member": f["member"],
                    "state_district": f["state_district"], "filed_date": f["filed_date"], "url": f["url"]}
                   for k, r in enumerate(rows)]
            stored += db.insert_rows(conn, "congress_tx", out)
            db.mark(conn, "house", f["doc_id"], "ok")
        except Exception as exc:
            log.warning("House PTR %s failed: %s", f["doc_id"], exc)
            db.mark(conn, "house", f["doc_id"], "error", str(exc)[:500])
        conn.commit()
    return stored
