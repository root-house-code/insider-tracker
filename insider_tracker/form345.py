"""Years of insider purchases, from the SEC's quarterly Insider Transactions Data Sets.

The live Form 4 ingest (edgar_form4.py) fetches one filing at a time and is only practical for
recent days. For history -- the backtest, and judging each insider by their own past -- the SEC
publishes every Form 3/4/5 since 2006 flattened into one zip per calendar quarter of filings:

    https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets

Each zip is ~10 MB. Only open-market PURCHASES (code P, non-derivative) are loaded -- ~5,000 a
quarter -- plus the holding rows of those filings, which the stake-change math needs. Rows land
in the same insider_tx / insider_holdings tables, in the same shape the live parser writes, so
every view, the backtest and the Discord bot see history and live data alike.

  * A filing already in the database (from the live ingest) is skipped, so the two never
    double-count where they overlap.
  * 10b5-1 plans: the filing-level checkbox (2023 on) or a 10b5-1 mention in a footnote the
    transaction cites -- the same rule as the live parser.
  * Each quarter is recorded in `processed` (source 'form345'), so a re-run only fetches new ones.
"""
import csv
import datetime as dt
import io
import logging
import re
import zipfile
from pathlib import Path

from . import config, db
from .edgar_common import ARCHIVES
from .edgar_form4 import PLAN_RE
from .net import PoliteSession
from .util import to_num

log = logging.getLogger(__name__)
PAGE = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
LINK_RE = re.compile(r'href="(/files/[^"]+/(\d{4})q([1-4])_form345\.zip)"')
DIR = Path(config.DB_PATH).parent / "form345_sets"
ROLE = {"Director": "is_director", "Officer": "is_officer", "TenPercentOwner": "is_ten_pct"}
csv.field_size_limit(10_000_000)


def _date(s):
    s = (s or "").strip()
    try:
        return dt.datetime.strptime(s.title(), "%d-%b-%Y").date().isoformat() if s else None
    except ValueError:
        return None


MAX_LATE_DAYS = 730


def _plausible(trans_date, filed_date) -> bool:
    """A trade date a filing could really have: not after the filing, and not years before it.
    Filers mistype dates ('0024-05-15', a 2016 trade in a 2015 filing); and a purchase reported
    two years late, typo or not, is nothing anyone could have acted on."""
    if not trans_date or not filed_date or trans_date > filed_date or trans_date < "2000-01-01":
        return False
    return (dt.date.fromisoformat(filed_date) - dt.date.fromisoformat(trans_date)).days <= MAX_LATE_DAYS


def list_quarters(http) -> list:
    """[(quarter label like '2026q3', url)] oldest first."""
    resp = http.get(PAGE)
    resp.raise_for_status()
    seen = {}
    for path, year, q in LINK_RE.findall(resp.text):
        seen.setdefault(f"{year}q{q}", "https://www.sec.gov" + path)
    return sorted(seen.items())


def _tsv(z, name):
    with z.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline=""),
                                  delimiter="\t", quoting=csv.QUOTE_NONE)


def parse(zip_path, skip=frozenset()) -> tuple:
    """(insider_tx rows, insider_holdings rows) for every open-market purchase in one quarter's
    file, leaving out the accessions in `skip`."""
    with zipfile.ZipFile(zip_path) as z:
        buys = [r for r in _tsv(z, "NONDERIV_TRANS.tsv")
                if r["TRANS_CODE"] == "P" and r["ACCESSION_NUMBER"] not in skip]
        wanted = {r["ACCESSION_NUMBER"] for r in buys}
        if not wanted:
            return [], []
        subs = {r["ACCESSION_NUMBER"]: r for r in _tsv(z, "SUBMISSION.tsv") if r["ACCESSION_NUMBER"] in wanted}
        owners = {}
        for r in _tsv(z, "REPORTINGOWNER.tsv"):
            if r["ACCESSION_NUMBER"] in wanted:
                owners.setdefault(r["ACCESSION_NUMBER"], []).append(r)
        notes = {}
        for r in _tsv(z, "FOOTNOTES.tsv"):
            if r["ACCESSION_NUMBER"] in wanted:
                notes[(r["ACCESSION_NUMBER"], r["FOOTNOTE_ID"])] = r["FOOTNOTE_TXT"] or ""
        held = [r for r in _tsv(z, "NONDERIV_HOLDING.tsv") if r["ACCESSION_NUMBER"] in wanted]

    tx, seq = [], {}
    for r in buys:
        acc = r["ACCESSION_NUMBER"]
        sub = subs.get(acc)
        if not sub:
            continue
        own = owners.get(acc, [])
        flags = {"is_director": 0, "is_officer": 0, "is_ten_pct": 0}
        rel = []
        for o in own:
            for part in (o["RPTOWNER_RELATIONSHIP"] or "").split(","):
                part = part.strip()
                if part in ROLE:
                    flags[ROLE[part]] = 1
                if part == "Director":
                    rel.append("Director")
                elif part == "Officer":
                    rel.append((o.get("RPTOWNER_TITLE") or "").strip() or "Officer")
                elif part == "TenPercentOwner":
                    rel.append("10% Owner")
                elif part == "Other":
                    rel.append((o.get("RPTOWNER_TXT") or "").strip() or "Other")
        cited = {v for k, v in r.items() if k.endswith("_FN") and v}
        plan = (sub.get("AFF10B5ONE") or "").lower() in ("1", "true") or any(
            PLAN_RE.search(notes.get((acc, fid.strip()), "")) for c in cited for fid in c.split(","))
        ticker = (sub["ISSUERTRADINGSYMBOL"] or "").strip().upper()
        if ticker in ("NONE", "N/A", "NA", "--"):
            ticker = ""
        if not _plausible(_date(r["TRANS_DATE"]), _date(sub["FILING_DATE"])):
            continue
        shares, price = to_num(r["TRANS_SHARES"]), to_num(r["TRANS_PRICEPERSHARE"])
        issuer_cik = str(int(sub["ISSUERCIK"] or 0))
        n = seq.get(acc, 0)
        seq[acc] = n + 1
        tx.append({
            "accession": acc, "seq": n, "form_type": sub["DOCUMENT_TYPE"], "filed_date": _date(sub["FILING_DATE"]),
            "trans_date": _date(r["TRANS_DATE"]), "issuer_cik": issuer_cik, "issuer_name": sub["ISSUERNAME"],
            "ticker": ticker, "owner_cik": "; ".join(o["RPTOWNERCIK"] for o in own),
            "owner_name": "; ".join(o["RPTOWNERNAME"] for o in own) or "?",
            "relationship": ", ".join(dict.fromkeys(rel)), **flags,
            "security": r["SECURITY_TITLE"], "is_derivative": 0, "code": "P",
            "acq_disp": r["TRANS_ACQUIRED_DISP_CD"], "shares": shares, "price": price,
            "value": round(shares * price, 2) if shares and price else None,
            "shares_after": to_num(r["SHRS_OWND_FOLWNG_TRANS"]),
            "direct_indirect": r["DIRECT_INDIRECT_OWNERSHIP"], "plan_10b5_1": int(bool(plan)),
            "url": f"{ARCHIVES}/edgar/data/{issuer_cik}/{acc.replace('-', '')}/{acc}-index.htm",
        })
    hold_seq = {}
    holds = []
    for h in held:
        acc = h["ACCESSION_NUMBER"]
        n = hold_seq.get(acc, 0)
        hold_seq[acc] = n + 1
        holds.append({"accession": acc, "seq": n, "security": h["SECURITY_TITLE"],
                      "shares": to_num(h["SHRS_OWND_FOLWNG_TRANS"]),
                      "direct_indirect": h["DIRECT_INDIRECT_OWNERSHIP"], "nature": h.get("NATURE_OF_OWNERSHIP")})
    return tx, holds


def load_quarter(conn, zip_path, label) -> int:
    """Store one quarter's purchases (skipping filings already here). Returns rows stored."""
    have = {r[0] for r in conn.execute("SELECT DISTINCT accession FROM insider_tx")}
    tx, holds = parse(zip_path, have)
    n = db.insert_rows(conn, "insider_tx", tx)
    db.insert_rows(conn, "insider_holdings", holds)
    for acc in {t["accession"] for t in tx}:
        db.mark(conn, "form4_holdings", acc, "ok")
    db.mark(conn, "form345", label, "ok", f"{n} purchases")
    conn.commit()
    return n


def ingest(conn, since_year=2015, http=None, keep_files=False) -> dict:
    """Download and load every quarter from `since_year` not loaded yet. The newest quarter is
    always re-checked, since the SEC publishes it while the live ingest is also catching up."""
    http = http or PoliteSession(config.sec_user_agent(), config.SEC_MIN_INTERVAL)
    done = db.processed_ids(conn, "form345")
    quarters = [(q, u) for q, u in list_quarters(http) if int(q[:4]) >= since_year]
    todo = [(q, u) for q, u in quarters if q not in done] + ([quarters[-1]] if quarters and quarters[-1][0] in done else [])
    DIR.mkdir(parents=True, exist_ok=True)
    loaded = 0
    for i, (q, url) in enumerate(todo, 1):
        path = DIR / url.rsplit("/", 1)[-1]
        if not path.exists():
            resp = http.get(url, timeout=600)
            resp.raise_for_status()
            path.write_bytes(resp.content)
        n = load_quarter(conn, path, q)
        loaded += n
        log.info("insider history %s: %d purchases (%d/%d)", q, n, i, len(todo))
        if not keep_files:
            path.unlink(missing_ok=True)
    first = conn.execute("SELECT MIN(trans_date) FROM insider_tx WHERE code='P'").fetchone()[0]
    return {"quarters": len(todo), "purchases": loaded, "earliest": first}
