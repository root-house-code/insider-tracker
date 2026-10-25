"""What kind of issuer a company is, from its SEC record: an operating company or a fund.

Funds (closed-end funds, interval funds, BDC-style trusts) file Form 4s like any company, and their
directors buy shares routinely -- "clusters" that say nothing about a business. The SEC's
submissions record tells them apart cleanly: an operating company has an industry (SIC) code and
files 10-Ks or 20-Fs; a fund has no SIC code and files fund reports (N-CSR, N-2, N-PORT). Names
can't do this: "Trust" is in REITs' names too (Simon Property Group is a REIT; so is ARMOUR).

One request per issuer, cached for months. Filled in by every Form 4 ingest for issuers with
open-market purchases, so the Discord bot has it too; the UI fills any gaps in the background.
"""
import datetime as dt
import logging
import re

import requests

from . import background, config
from .net import PoliteSession

log = logging.getLogger(__name__)
URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
# fund reports: annual/semi-annual, registration, portfolio holdings, proxy votes, custody
FUND_FORMS = re.compile(r"^(N-CSRS?|N-2|N-PORT|NPORT-[A-Z]+|N-CEN|N-30D|N-1A|N-Q|N-PX|40-17F2|N-23C|497)\b")
FRESH = dt.timedelta(days=180)
FUND_TICKER = re.compile(r"^[A-Z]{4}X$")  # mutual-fund style symbols (PMPEX), when the SEC can't say


def classify(payload: dict) -> dict:
    """{sic, sic_description, entity_type, is_fund} from an SEC submissions record."""
    forms = (((payload or {}).get("filings") or {}).get("recent") or {}).get("form") or []
    sic = str(payload.get("sic") or "").strip()
    if not sic.strip("0"):  # some funds' records say "0000" rather than leaving it blank
        sic = ""
    fund_forms = sum(1 for f in forms[:500] if FUND_FORMS.match(f))
    company_forms = sum(1 for f in forms[:500] if f in ("10-K", "10-Q", "20-F", "40-F", "10-K/A"))
    return {"sic": sic, "sic_description": payload.get("sicDescription") or "",
            "entity_type": payload.get("entityType") or "",
            "is_fund": int(not sic and fund_forms > 0 and fund_forms >= company_forms)}


def get_many(conn, ciks) -> dict:
    ciks = [str(int(c)) for c in ciks if str(c or "").strip().isdigit()]
    if not ciks:
        return {}
    marks = ",".join("?" * len(ciks))
    return {r["cik"]: dict(r) for r in conn.execute(f"SELECT * FROM issuer_info WHERE cik IN ({marks})", ciks)}


def missing(conn, ciks, now=None) -> list:
    now = now or dt.datetime.now(dt.timezone.utc)
    have = get_many(conn, ciks)
    out = []
    for c in dict.fromkeys(str(int(c)) for c in ciks if str(c or "").strip().isdigit()):
        row = have.get(c)
        if not row or now - dt.datetime.fromisoformat(row["fetched_at"]) >= FRESH:
            out.append(c)
    return out


def fetch_many(conn, ciks, http=None, now=None) -> int:
    """Look up and cache each issuer. Never raises; returns how many were stored."""
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        http = http or PoliteSession(config.sec_user_agent(), config.SEC_MIN_INTERVAL, max_retries=1, timeout=15)
    except SystemExit as exc:
        log.warning("issuer lookups skipped: %s", exc)
        return 0
    stored = 0
    for cik in ciks:
        try:
            r = http.get(URL.format(cik=int(cik)))
            if r.status_code == 404:
                info = {"sic": "", "sic_description": "", "entity_type": "", "is_fund": 0}
            else:
                r.raise_for_status()
                info = classify(r.json())
        except (requests.RequestException, ValueError) as exc:
            log.warning("issuer %s lookup failed: %s", cik, exc)
            continue
        conn.execute("INSERT OR REPLACE INTO issuer_info (cik, sic, sic_description, entity_type, is_fund, "
                     "fetched_at) VALUES (?,?,?,?,?,?)",
                     (str(int(cik)), info["sic"], info["sic_description"], info["entity_type"], info["is_fund"],
                      now.isoformat(timespec="seconds")))
        stored += 1
        if stored % 50 == 0:
            conn.commit()
    conn.commit()
    return stored


def fill_buyers(conn, http=None, since=None) -> int:
    """Cache the type of every issuer with open-market purchases since `since` (a Form 4 ingest
    calls this, so the Discord bot's server knows funds from companies too)."""
    since = since or (dt.date.today() - dt.timedelta(days=120)).isoformat()
    ciks = [r[0] for r in conn.execute(
        "SELECT DISTINCT issuer_cik FROM insider_tx WHERE code='P' AND is_derivative=0 AND trans_date>=?", (since,))]
    todo = missing(conn, ciks)
    return fetch_many(conn, todo, http) if todo else 0


def refresh_in_background(db_path, ciks) -> bool:
    if not ciks:
        return background.running("issuers")
    return background.start_db("issuers", db_path, lambda conn: fetch_many(conn, list(ciks)))


def is_fund(info, ticker="") -> bool:
    """From the cached SEC record; failing that, a mutual-fund style ticker (five letters ending X)."""
    if info is not None:
        return bool(info.get("is_fund"))
    return bool(FUND_TICKER.match((ticker or "").upper()))
