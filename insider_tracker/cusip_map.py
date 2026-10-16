"""CUSIP -> ticker for 13F holdings. Two sources, always labelled in cusip_ticker.source.

13F filings identify securities by CUSIP only, and the SEC publishes no CUSIP-to-ticker
table, so tickers come from outside:

  1. OpenFIGI (free), looked up by CUSIP. Precise, but its US-exchange coverage of CUSIPs
     is incomplete: a real, listed company like Exxon simply comes back empty.
  2. For what OpenFIGI misses, the issuer NAME from the filing matched against the SEC's
     own company_tickers.json. Measured against CUSIPs OpenFIGI had resolved, it agreed
     99.6% of the time when it gave an answer. It deliberately answers less rather than
     guess: ambiguous names, bonds (principal amounts) and fund-like names stay blank,
     because a wrong ticker is worse than none.

Every answer is cached, including "no ticker exists" (source 'none'), so each CUSIP is
looked up once. A failed request is NOT cached: an outage must not become a permanent
"no ticker". Only CUSIPs and public issuer names ever leave this machine.
"""
import collections
import logging
import re

from . import config
from .net import PoliteSession

log = logging.getLogger(__name__)
URL = "https://api.openfigi.com/v3/mapping"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
BATCH = 10          # jobs per request without an API key
MIN_INTERVAL = 2.5  # 25 requests per minute without a key

# Words that carry no identity ("INC", "HOLDINGS", share-class boilerplate). Dropping
# them from both sides lets "SEAGATE TECHNOLOGY HLDNGS" meet "Seagate Technology Holdings plc".
NOISE = {"INC", "CORP", "CORPORATION", "CO", "COMPANY", "COMPANIES", "LTD", "LIMITED", "PLC",
         "NV", "SA", "SE", "AG", "LP", "LLC", "THE", "HOLDINGS", "HOLDING", "HLDGS", "HLDG",
         "HLDNGS", "GROUP", "GRP", "CL", "CLASS", "COM", "NEW", "DEL", "ORD", "SHS", "ADR",
         "ADS", "SPONSORED", "CAP", "STK", "UNIT", "UNITS"}
ABBR = {"INDS": "INDUSTRIES", "INTL": "INTERNATIONAL", "FINL": "FINANCIAL", "MATLS": "MATERIALS",
        "TECH": "TECHNOLOGY", "TECHNOLOGIES": "TECHNOLOGY", "SYS": "SYSTEMS", "SVCS": "SERVICES",
        "ENTMT": "ENTERTAINMENT", "PHARMACEUTICALS": "PHARMA", "PHARMACEUTICAL": "PHARMA",
        "COMMUNICATIONS": "COMMUNICATION", "COMMS": "COMMUNICATION", "ELEC": "ELECTRIC",
        "ELECTRONICS": "ELECTRONIC", "MFG": "MANUFACTURING", "NATL": "NATIONAL", "AMER": "AMERICAN",
        "RESOURCES": "RESOURCE", "RES": "RESOURCE", "LABS": "LABORATORIES"}
# An issuer that is a trust or fund can't be told apart by name ("WISDOMTREE TR" is dozens of ETFs).
FUNDISH = {"TR", "TRUST", "ETF", "FUND", "FD", "PORTFOLIO", "INDEX"}
TRUNCATED_AT = 28   # 13F issuer names are cut at ~30 chars, often mid-word


def _words(name: str) -> list:
    return re.sub(r"[^A-Z0-9 ]", " ", name.upper().replace("&", " AND ")).split()


def norm(name: str) -> tuple:
    toks = [ABBR.get(t, t) for t in _words(name)]
    return tuple(t for t in toks if t not in NOISE and len(t) > 1)


def build_name_index(companies: dict) -> dict:
    """{normalised name: {tickers}} from the SEC's company_tickers.json."""
    idx = collections.defaultdict(set)
    for v in companies.values():
        idx[norm(v["title"])].add(v["ticker"].upper())
    return idx


def match_name(idx: dict, name: str) -> str:
    """The one ticker this issuer name identifies, or '' if it is unknown or ambiguous."""
    name = " ".join(name.split())
    words = _words(name)
    if not words or set(words) & FUNDISH:
        return ""
    candidates = [words]
    # Names are cut at ~30 chars, so the last word may be a stub ("HOLDI", "PL", "IN").
    # Only drop it if it is the start of a boilerplate word -- dropping a meaningful word
    # ("INFRASTRUCTUR") would let "COHEN & STEERS INFRASTRUCTUR" match Cohen & Steers Inc.
    if (len(name) >= TRUNCATED_AT and len(words) > 1 and words[-1] not in NOISE
            and any(n.startswith(words[-1]) for n in NOISE)):
        candidates.append(words[:-1])
    for ws in candidates:
        key = norm(" ".join(ws))
        if len("".join(key)) < 4:  # "F N B CORP" normalises to nothing and would match anything
            continue
        hit = idx.get(key)
        if hit:
            return next(iter(hit)) if len(hit) == 1 else ""
    return ""


def unmapped(conn) -> list:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT cusip FROM holdings_13f WHERE cusip<>'' "
        "AND cusip NOT IN (SELECT cusip FROM cusip_ticker) ORDER BY cusip")]


def _pick(result: dict):
    """(ticker, name, security_type) from one OpenFIGI result, or '' fields if none."""
    data = result.get("data") or []
    if not data:  # {'warning': 'No identifier found.'} or {'error': 'Invalid idValue format.'}
        return "", "", ""
    d = data[0]
    return d.get("ticker") or "", d.get("name") or "", d.get("securityType") or ""


def _figi(conn, http) -> int:
    todo = unmapped(conn)
    if not todo:
        return 0
    log.info("mapping %d new CUSIPs to tickers (~%d min)", len(todo),
             max(1, round(len(todo) / BATCH * MIN_INTERVAL / 60)))
    stored = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        try:
            resp = http.post(URL, json=[{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"}
                                        for c in batch])
            resp.raise_for_status()
            results = resp.json()
            if len(results) != len(batch):
                raise ValueError(f"asked for {len(batch)}, got {len(results)}")
        except Exception as exc:  # leave the batch unmapped; the next run retries it
            log.warning("CUSIP mapping batch failed (%s); will retry next run", exc)
            continue
        rows = []
        for c, r in zip(batch, results):
            tk, nm, st = _pick(r)
            rows.append((c, tk, nm, st, "figi" if tk else None))
        conn.executemany("INSERT OR REPLACE INTO cusip_ticker "
                         "(cusip, ticker, name, security_type, source) VALUES (?,?,?,?,?)", rows)
        conn.commit()
        stored += len(batch)
    return stored


def _by_name(conn, http) -> int:
    """Second chance, by issuer name, for CUSIPs OpenFIGI had nothing for."""
    cands = conn.execute("""
        SELECT c.cusip, MAX(h.issuer) AS issuer, MAX(h.share_type='PRN') AS is_debt
        FROM cusip_ticker c JOIN holdings_13f h ON h.cusip=c.cusip
        WHERE c.ticker='' AND c.source IS NOT 'none' GROUP BY c.cusip""").fetchall()
    if not cands:
        return 0
    idx = None
    found = 0
    updates = []
    for cusip, issuer, is_debt in cands:
        tk = ""
        if not is_debt:  # a bond's issuer name would hand it the stock's ticker
            if idx is None:
                try:
                    resp = http.get(SEC_TICKERS)
                    resp.raise_for_status()
                    idx = build_name_index(resp.json())
                except Exception as exc:  # try again next run; don't mark anything 'none'
                    log.warning("name matching skipped: could not fetch SEC ticker list (%s)", exc)
                    return 0
            tk = match_name(idx, issuer or "")
        updates.append((tk, "sec_name" if tk else "none", cusip))
        found += bool(tk)
    conn.executemany("UPDATE cusip_ticker SET ticker=?, source=? WHERE cusip=?", updates)
    conn.commit()
    log.info("matched %d of %d unresolved CUSIPs by issuer name", found, len(updates))
    return found


def resolve(conn, http=None, sec_http=None) -> int:
    """Fill in tickers for every 13F CUSIP not yet cached, then retry the empty ones by
    issuer name. Returns how many CUSIPs OpenFIGI was asked about. Never raises: a mapping
    failure must not fail the ingest that called it."""
    try:
        ua = None if (http and sec_http) else config.sec_user_agent()
    except SystemExit as exc:
        log.warning("CUSIP mapping skipped: %s", exc)
        return 0
    http = http or PoliteSession(ua, MIN_INTERVAL)                       # OpenFIGI: 25 req/min
    sec_http = sec_http or PoliteSession(ua, config.SEC_MIN_INTERVAL)    # sec.gov: its own rules
    asked = _figi(conn, http)
    try:
        _by_name(conn, sec_http)
    except Exception as exc:
        log.warning("name matching failed: %s", exc)
    return asked
