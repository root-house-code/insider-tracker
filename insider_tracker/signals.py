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
from . import config


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params)]


def cluster_buys(conn, since: str, min_insiders: int = 2):
    return _rows(conn, """
        WITH buys AS (
          SELECT DISTINCT ticker, issuer_name, owner_name, trans_date, shares, price, value
          FROM insider_tx
          WHERE code='P' AND is_derivative=0 AND plan_10b5_1=0 AND ticker<>'' AND trans_date>=?)
        SELECT ticker, issuer_name, COUNT(DISTINCT owner_name) AS insiders,
               SUM(value) AS total_value, MIN(trans_date) AS first_buy, MAX(trans_date) AS last_buy,
               GROUP_CONCAT(DISTINCT owner_name) AS who
        FROM buys GROUP BY ticker HAVING insiders >= ?
        ORDER BY insiders DESC, total_value DESC""", (since, min_insiders))


def large_buys(conn, since: str, min_value: float = config.LARGE_BUY_USD, limit: int = 30):
    return _rows(conn, """
        SELECT DISTINCT trans_date, ticker, issuer_name, owner_name, relationship,
               shares, price, value, shares_after, url
        FROM insider_tx
        WHERE code='P' AND is_derivative=0 AND plan_10b5_1=0 AND value >= ?
          AND (is_officer=1 OR is_director=1) AND trans_date>=?
        ORDER BY value DESC LIMIT ?""", (min_value, since, limit))


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
