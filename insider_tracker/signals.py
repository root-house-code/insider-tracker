"""Queries that turn raw filings into things worth reading.

Ordering reflects signal quality, roughly:
  1. Cluster buys: several insiders at one company buying on the open market
  2. Large open-market buys by officers/directors
  3. Congressional trades (lagged up to 45 days, dollar ranges only)
  4. Executive sales outside 10b5-1 plans (weak signal; people sell for many reasons)
  5. 13F changes (up to ~4.5 months stale)

All insider queries use only code 'P'/'S' (open-market) and exclude
10b5-1 plan trades. Grants, option exercises and tax withholding are noise.
SELECT DISTINCT on the economic fields collapses Form 4/A duplicates.
"""
import datetime as dt
import math
import re

from . import config

# Short role labels for a cluster, most senior first. Officer titles on Form 4 are free text
# ("CEO", "Pres. & CEO", "Principal Executive Officer"), so the top titles are matched by
# pattern -- and only for filers who ticked the Officer box, since "CEO of Sub Advisor" is
# someone else's CEO. Officers matching none of them still show up, as "Officer".
TITLES = [
    ("CEO", re.compile(r"\b(co-?)?ceo\b|chief executive|principal executive", re.I)),
    ("President", re.compile(r"\bpres(ident)?\b(?!\.? of)", re.I)),
    ("CFO", re.compile(r"\bcfo\b|chief financial|principal financial", re.I)),
    ("COO", re.compile(r"\b(co-?)?coo\b|chief operating", re.I)),
    ("Chair", re.compile(r"\bchair", re.I)),
]
NOT_TITLES = re.compile(r"vice[\s-]*pres\w*\.?|vice[\s-]*chair\w*", re.I)
ROLE_ORDER = [t for t, _ in TITLES] + ["Officer", "Director", "10% owner", "Other"]


def roles(relationship, is_director, is_officer, is_ten_pct) -> set:
    """The role labels one filer's Form 4 relationship maps to."""
    text = NOT_TITLES.sub(" ", relationship or "")
    found = {t for t, rx in TITLES if (is_officer or t == "Chair") and rx.search(text)}
    if is_officer and not found - {"Chair"}:
        found.add("Officer")
    if is_director:
        found.add("Director")
    if is_ten_pct:
        found.add("10% owner")
    return found or {"Other"}


def stake_text(pct) -> str:
    """'+25%', '+0.4%' or 'new' (a position that started from nothing)."""
    if pct is None:
        return ""
    if pct == math.inf:
        return "new"
    return f"+{pct:,.0f}%" if pct >= 10 else f"+{pct:.1f}%"


def csv_safe(rows):
    """Rows for a CSV file: a brand-new position is written 'new', not 'inf'."""
    return [{k: ("new" if v == math.inf else v) for k, v in r.items()} for r in rows]


def insider_stake(conn, trades):
    """stake_change for one insider's purchases, counting the other holdings their first filing
    in the set reports (Table I holding rows). Later filings' holding rows are left out: by then a
    holding bought into earlier would be counted twice."""
    if not trades:
        return None
    first = min(trades, key=lambda t: (t.get("filed_date") or "", t["accession"]))["accession"]
    held = _rows(conn, "SELECT security, shares FROM insider_holdings WHERE accession=?", (first,))
    return stake_change(trades, held)


def stake_change(trades, holdings=()):
    """How much a set of one insider's purchases grew the holdings they went into, in percent;
    math.inf for a position that started from nothing; None when the filings can't say.

    Form 4's "shares owned following" is per holding line, and one insider can buy into several
    of them at once -- shares held directly, plus one or more trusts or funds (indirect) -- each
    with its own running balance. The filing names which trust, but we don't store that, so the
    lines are recovered by chaining: a purchase continues the holding whose last balance equals
    its own starting balance. Lines are separated by share class and direct/indirect first, and
    only the class most of the shares went into is counted (classes aren't comparable shares).
    A sale between two purchases breaks a chain into two; that errs low, never high.

    `holdings` are the filing's other lines (shares held with no transaction, in a different
    trust or directly); those in the same share class add to the position before and after."""
    good = [t for t in trades if (t.get("shares") or 0) > 0 and t.get("shares_after") is not None]
    if not good or any(t["shares_after"] < t["shares"] - 0.5 for t in good):
        return None  # a balance smaller than the purchase itself: bad data, don't guess
    by_class = {}
    for t in good:
        by_class.setdefault(t.get("security") or "", []).append(t)
    main = max(by_class.values(), key=lambda ts: sum(t["shares"] for t in ts))
    before = after = 0.0
    for kind in {t.get("direct_indirect") or "" for t in main}:
        # dedupe restatements (a 4/A repeats the same line), then chain in balance order
        lines = sorted({(t["trans_date"], t["shares"], t["shares_after"]) for t in main
                        if (t.get("direct_indirect") or "") == kind}, key=lambda x: (x[0], x[2]))
        chains = []  # [starting balance, latest balance]
        for _, shares, owned in lines:
            start = owned - shares
            link = next((c for c in chains if abs(c[1] - start) <= 1), None)
            if link:
                link[1] = owned
            else:
                chains.append([start, owned])
        before += sum(c[0] for c in chains)
        after += sum(c[1] for c in chains)
    main_class = main[0].get("security") or ""
    other = sum(h["shares"] or 0 for h in holdings if (h.get("security") or "") == main_class)
    before, after = before + other, after + other
    if after - before <= 0:
        return None
    return math.inf if before < 0.5 else 100 * (after - before) / before


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params)]


HISTORY_YEARS = 3
BUYER_ORDER = ["first buy", "occasional", "routine", "unknown"]


def purchase_months(conn, issuer_ciks) -> dict:
    """{(issuer_cik, insider): {'YYYY-MM', ...}} -- every month each insider bought each of these
    companies' stock. The insider is the first CIK on the filing (as the Discord bot counts them)."""
    ciks = sorted({str(c) for c in issuer_ciks if c})
    out = {}
    for i in range(0, len(ciks), 500):
        part = ciks[i:i + 500]
        for issuer, owner, month in conn.execute(
                f"SELECT issuer_cik, owner_cik, substr(trans_date, 1, 7) FROM insider_tx "
                f"WHERE issuer_cik IN ({','.join('?' * len(part))}) AND code='P' AND is_derivative=0",
                part):
            who = (owner or "").split(";")[0].strip().lstrip("0")
            out.setdefault((issuer, who), set()).add(month)
    return out


def buyer_type(months, trans_date, data_start) -> str:
    """What kind of buyer an insider was at the time of a purchase, judged only by the three years
    before it (adapted from Cohen, Malloy and Pomorski, 2012: 'routine' insiders trade in the same
    month every year and their trades carry little information; the rest are 'opportunistic').

      routine     bought in this same calendar month in each of the previous three years
      first buy   no purchases of this company's stock in the previous three years
      occasional  anything in between
      unknown     the database doesn't reach three years back from this purchase"""
    year, month = int(trans_date[:4]), trans_date[5:7]
    if f"{year - HISTORY_YEARS}{trans_date[4:]}" < (data_start or "9999"):
        return "unknown"
    if all(f"{year - k}-{month}" in months for k in range(1, HISTORY_YEARS + 1)):
        return "routine"
    earlier = f"{year - HISTORY_YEARS}-{month}"
    if not any(earlier <= m < trans_date[:7] for m in months):
        return "first buy"
    return "occasional"


def data_start(conn) -> str:
    """The earliest filing date of a purchase in the database: history checks can't reach before it."""
    return conn.execute("SELECT MIN(filed_date) FROM insider_tx WHERE code='P'").fetchone()[0] or ""


def lockstep(buys) -> bool:
    """Every insider paid the identical price on the same day: the mark of an IPO allocation, a
    private placement or a company purchase plan, not of people independently deciding to buy."""
    people = {b["owner_name"] for b in buys}
    terms = {(b["trans_date"], round(b["price"] or 0, 2)) for b in buys}  # to the cent: the SEC data sets round
    return len(people) >= 2 and len(terms) == 1


REIT_SIC = "6798"


def noise_flags(is_fund: bool, buys, buyers="", is_reit=False) -> str:
    """The 'flags' value for a cluster: bare-comma list, so the filter can pick one.
      routine  every insider who bought was a routine buyer (same month each of the last three years)
      reit     a real estate investment trust: REIT clusters trailed small caps in both 2015-2020 and
               2021-2026 (backtest), unlike most other kinds of company"""
    return ",".join(f for f, on in (("fund", is_fund), ("lockstep", lockstep(buys)),
                                    ("routine", buyers == "routine"), ("reit", is_reit)) if on)


def cluster_buys(conn, since: str, min_insiders: int = 2, hide_noise: int = 1):
    """Several insiders buying one company on the open market. `flags` marks clusters that are
    probably not discretionary buying -- a fund's directors, or everyone at one price on one day
    (an IPO or placement) -- and hide_noise (the default) leaves those out."""
    from . import issuers
    rows = _rows(conn, """
        WITH buys AS (
          SELECT DISTINCT ticker, issuer_name, issuer_cik, owner_name, trans_date, shares, price, value
          FROM insider_tx
          WHERE code='P' AND is_derivative=0 AND plan_10b5_1=0 AND ticker<>'' AND trans_date>=?)
        SELECT ticker, issuer_name, MAX(issuer_cik) AS issuer_cik, COUNT(DISTINCT owner_name) AS insiders,
               SUM(value) AS total_value, MIN(trans_date) AS first_buy, MAX(trans_date) AS last_buy,
               GROUP_CONCAT(DISTINCT owner_name) AS who
        FROM buys GROUP BY ticker HAVING insiders >= ?
        ORDER BY insiders DESC, total_value DESC""", (since, min_insiders))
    found, bought = {}, {}
    for r in _rows(conn, """
            SELECT DISTINCT ticker, owner_name, relationship, is_director, is_officer, is_ten_pct,
                   security, direct_indirect, trans_date, shares, shares_after, accession, filed_date, price,
                   issuer_cik, owner_cik
            FROM insider_tx
            WHERE code='P' AND is_derivative=0 AND plan_10b5_1=0 AND ticker<>'' AND trans_date>=?""",
            (since,)):
        found.setdefault(r["ticker"], set()).update(
            roles(r["relationship"], r["is_director"], r["is_officer"], r["is_ten_pct"]))
        bought.setdefault(r["ticker"], {}).setdefault(r["owner_name"], []).append(r)
    from . import earnings
    kinds = issuers.get_many(conn, [r["issuer_cik"] for r in rows])
    months = purchase_months(conn, [r["issuer_cik"] for r in rows])
    reports = earnings.dates_many(conn, [r["issuer_cik"] for r in rows])
    start = data_start(conn)
    out = []
    for row in rows:  # bare-comma list, like GROUP_CONCAT's `who`, so the filter can pick one role
        buys = [b for ts in bought.get(row["ticker"], {}).values() for b in ts]
        types = set()
        for ts in bought.get(row["ticker"], {}).values():
            first = min(ts, key=lambda b: b["trans_date"])  # their first purchase in the window decides
            who = (first["owner_cik"] or "").split(";")[0].strip().lstrip("0")
            types.add(buyer_type(months.get((first["issuer_cik"], who), set()), first["trans_date"], start))
        row["buyers"] = ",".join(k for k in BUYER_ORDER if k in types)
        # where in the quarter the buying began: days since the company's last results
        row["quarter_timing"] = earnings.timing(earnings.days_since_results(
            reports.get(str(int(row["issuer_cik"] or 0)), []), row["first_buy"]))
        info = kinds.get(str(int(row["issuer_cik"] or 0)))
        row["flags"] = noise_flags(issuers.is_fund(info, row["ticker"]), buys, row["buyers"],
                                   bool(info and info.get("sic") == REIT_SIC))
        if hide_noise and row["flags"]:
            continue
        row["roles"] = ",".join(t for t in ROLE_ORDER if t in found.get(row["ticker"], ()))
        stakes = [s for s in (insider_stake(conn, ts) for ts in bought.get(row["ticker"], {}).values())
                  if s is not None]
        row["max_stake_change"] = max(stakes) if stakes else None
        out.append(row)
    return out


BREADTH_MIN_HOLDERS = 20  # fund_breadth needs this many holders now...
BREADTH_MIN_BEFORE = 10   # ...and this many the quarter before (else it's a new listing, or too few)


def fund_overlap(conn, since: str, min_insiders: int = 1, min_net_funds: int = 1, hide_noise: int = 1):
    """Stocks insiders bought in the window that institutions were also net buying in the latest
    13F quarter (funds.py): more managers opened or added (>5%) than cut or exited. Same rows and
    columns as cluster_buys, plus the fund columns. Empty until the 13F data sets are loaded."""
    from . import funds, prices  # funds is only needed by this view
    meta = funds.latest(conn)
    if not meta:
        return []
    flows = funds.flows_by_ticker(conn, meta["period"])
    alias = {}
    for t, f in flows.items():
        for c in [t, *prices.candidates(t)]:
            alias.setdefault(c, f)
    tracked = funds.tracked_by_cusip(conn, meta["period"])
    out = []
    for r in cluster_buys(conn, since, min_insiders, hide_noise):
        f = next((alias[c] for c in [r["ticker"], *prices.candidates(r["ticker"])] if c in alias), None)
        if not f:
            continue
        net = (f["new"] + f["added"]) - (f["cut"] + f["exited"])
        if net < min_net_funds:
            continue
        mine = []
        for name, now, before in sorted(tracked.get(f["cusip"], []), key=lambda x: x[0] or ""):
            if before and now:
                change = f"{100 * (now / before - 1):+.0f}%" if abs(now / before - 1) > 0.005 else "held"
            else:
                change = "new" if now else "exited"
            mine.append(f"{name} {change}")
        # Net buyers as a share of holders: size-neutral, so a mega-cap's routine churn (hundreds of
        # funds either way) doesn't outrank a smaller stock being discovered. Only where there is a
        # real base to measure against: a stock nobody held last quarter is usually a fresh listing
        # (every holder "new", 100%), and 7 of 7 holders is too few to mean anything.
        before = f["holders"] - f["new"] + f["exited"]
        breadth = (100 * net / f["holders"]
                   if f["holders"] >= BREADTH_MIN_HOLDERS and before >= BREADTH_MIN_BEFORE else None)
        out.append({**r, "net_funds": net, "fund_breadth": breadth,
                    "funds_new": f["new"], "funds_added": f["added"],
                    "funds_cut": f["cut"], "funds_exited": f["exited"], "fund_holders": f["holders"],
                    "fund_shares_change": (100 * (f["shares"] / f["prior_shares"] - 1)) if f["prior_shares"] else None,
                    "tracked_funds": ",".join(mine)})
    return out


CLUSTER_GAP_DAYS = 14  # a purchase this close to the previous one continues the same bout of buying


def cluster_episodes(buys, gap_days=CLUSTER_GAP_DAYS, min_insiders=2) -> list:
    """Split one company's purchases into separate bouts of buying, and keep the bouts with at
    least `min_insiders` different buyers: [{first, last, insiders, total_value}], oldest first.

    cluster_buys() treats a whole window as one group, which is right for "who is buying now"
    but wrong over a year: two directors buying nine months apart are not a cluster."""
    day = lambda b: dt.date.fromisoformat(b["trans_date"])
    bouts, cur = [], []
    for b in sorted((b for b in buys if b.get("trans_date")), key=lambda b: b["trans_date"]):
        if cur and (day(b) - day(cur[-1])).days > gap_days:
            bouts.append(cur)
            cur = []
        cur.append(b)
    if cur:
        bouts.append(cur)
    out = []
    for bout in bouts:
        who = {b["owner_name"] for b in bout}
        if len(who) >= min_insiders:
            out.append({"first": bout[0]["trans_date"], "last": bout[-1]["trans_date"], "insiders": len(who),
                        "total_value": sum(b.get("value") or 0 for b in bout)})
    return out


def large_buys(conn, since: str, min_value: float = config.LARGE_BUY_USD, limit: int = 30):
    rows = _rows(conn, """
        SELECT DISTINCT trans_date, ticker, issuer_name, owner_name, relationship,
               shares, price, value, shares_after, url, accession
        FROM insider_tx
        WHERE code='P' AND is_derivative=0 AND plan_10b5_1=0 AND value >= ?
          AND (is_officer=1 OR is_director=1) AND trans_date>=?
        ORDER BY value DESC LIMIT ?""", (min_value, since, limit))
    stakes = {}
    for r in rows:  # the whole filing's purchases against everything the filer reports holding
        acc = r.pop("accession")
        if acc not in stakes:
            stakes[acc] = insider_stake(conn, _rows(conn, """
                SELECT DISTINCT accession, filed_date, security, direct_indirect, trans_date, shares,
                       shares_after FROM insider_tx
                WHERE accession=? AND code='P' AND is_derivative=0""", (acc,)))
        r["stake_change"] = stakes[acc]
    return rows


def exec_sales(conn, since: str, min_value: float = config.EXEC_SALE_USD, limit: int = 30):
    return _rows(conn, """
        SELECT DISTINCT trans_date, ticker, issuer_name, owner_name, relationship,
               shares, price, value, shares_after, url
        FROM insider_tx
        WHERE code='S' AND is_derivative=0 AND plan_10b5_1=0 AND value >= ? AND is_officer=1
          AND trans_date>=?
        ORDER BY value DESC LIMIT ?""", (min_value, since, limit))


def congress_recent(conn, since: str, limit: int = 100):
    return _rows(conn, """
        SELECT filed_date, trans_date, chamber, member, owner, ticker, asset, tx_type,
               amount_text, CAST(julianday(filed_date)-julianday(trans_date) AS INT) AS lag_days, url
        FROM congress_tx WHERE filed_date>=?
        ORDER BY filed_date DESC, amount_low DESC LIMIT ?""", (since, limit))


def congress_most_traded(conn, since: str, limit: int = 20):
    # bought_sold_ratio is by dollars (disclosure-range floors), NULL when nothing
    # was sold -- a buys-only ticker has no finite ratio. min_total_usd also counts
    # exchanges, so it can exceed min_bought_usd + min_sold_usd.
    return _rows(conn, """
        SELECT *, ROUND(min_bought_usd * 1.0 / NULLIF(min_sold_usd, 0), 2) AS bought_sold_ratio
        FROM (
          SELECT ticker, COUNT(DISTINCT member) AS members,
                 SUM(tx_type='P') AS buys, SUM(tx_type='S') AS sells,
                 SUM(CASE WHEN tx_type='P' THEN COALESCE(amount_low,0) ELSE 0 END) AS min_bought_usd,
                 SUM(CASE WHEN tx_type='S' THEN COALESCE(amount_low,0) ELSE 0 END) AS min_sold_usd,
                 SUM(COALESCE(amount_low,0)) AS min_total_usd
          FROM congress_tx WHERE ticker<>'' AND filed_date>=?
          GROUP BY ticker)
        ORDER BY members DESC, min_total_usd DESC LIMIT ?""", (since, limit))


def congress_late(conn, since: str, days: int = config.LATE_FILING_DAYS):
    return _rows(conn, """
        SELECT member, chamber, ticker, asset, trans_date, filed_date,
               CAST(julianday(filed_date)-julianday(trans_date) AS INT) AS lag_days, amount_text, url
        FROM congress_tx
        WHERE filed_date>=? AND julianday(filed_date)-julianday(trans_date) > ?
        ORDER BY lag_days DESC""", (since, days))


def overlap(conn, since: str):
    """Tickers bought by both members of Congress and company insiders in the window."""
    return _rows(conn, """
        SELECT c.ticker, COUNT(DISTINCT c.member) AS members,
               GROUP_CONCAT(DISTINCT c.member) AS member_names,
               (SELECT COUNT(DISTINCT owner_name) FROM insider_tx i
                 WHERE i.ticker=c.ticker AND i.code='P' AND i.plan_10b5_1=0 AND i.trans_date>=?) AS insiders
        FROM congress_tx c
        WHERE c.tx_type='P' AND c.ticker<>'' AND c.trans_date>=?
        GROUP BY c.ticker HAVING insiders > 0
        ORDER BY members + insiders DESC""", (since, since))


def thirteenf_changes(conn, min_change_pct: float = 25.0):
    out = []
    managers = conn.execute("SELECT DISTINCT manager_cik, manager_name FROM holdings_13f").fetchall()
    for mgr in managers:
        periods = [r[0] for r in conn.execute(
            "SELECT DISTINCT period FROM holdings_13f WHERE manager_cik=? ORDER BY period DESC LIMIT 2",
            (mgr["manager_cik"],))]
        if len(periods) < 2:
            continue

        def snapshot(period):
            acc = conn.execute(
                "SELECT accession FROM holdings_13f WHERE manager_cik=? AND period=? "
                "ORDER BY filed_date DESC LIMIT 1", (mgr["manager_cik"], period)).fetchone()[0]
            return {(r["cusip"], r["put_call"]): dict(r) for r in conn.execute(
                "SELECT h.*, COALESCE(t.ticker, '') AS ticker FROM holdings_13f h "
                "LEFT JOIN cusip_ticker t ON t.cusip=h.cusip WHERE h.accession=?", (acc,))}

        cur, prev = snapshot(periods[0]), snapshot(periods[1])
        for key in cur.keys() | prev.keys():
            c, p = cur.get(key), prev.get(key)
            if c and not p:
                change, pct = "NEW", None
            elif p and not c:
                change, pct = "EXIT", -100.0
            else:
                if not p["shares"]:
                    continue
                pct = (c["shares"] - p["shares"]) / p["shares"] * 100
                if abs(pct) < min_change_pct:
                    continue
                change = "ADD" if pct > 0 else "TRIM"
            ref = c or p
            out.append({
                "manager": mgr["manager_name"], "period": periods[0], "prior": periods[1],
                "change": change, "issuer": ref["issuer"], "ticker": ref["ticker"], "cusip": ref["cusip"],
                "put_call": ref["put_call"], "shares_prior": p["shares"] if p else 0,
                "shares_now": c["shares"] if c else 0,
                "pct_change": round(pct, 1) if pct is not None else None,
                "value_now": c["value_usd"] if c else 0,
            })
    order = {"NEW": 0, "EXIT": 1, "ADD": 2, "TRIM": 3}
    return sorted(out, key=lambda r: (r["manager"], order[r["change"]], -(r["value_now"] or 0)))
