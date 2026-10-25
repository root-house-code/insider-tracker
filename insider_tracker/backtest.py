"""How insider cluster buys have done since, replayed from the database -- years of them.

A cluster counts only from the day it could have been seen: the FILING date of the purchase that
brought the Nth distinct insider (by SEC ID) into a bout of buying (purchases no more than 14 days
apart). N is 2, 3 or 4 ("level"), each dated separately: calling a cluster "3 insiders" because a
third joined after you'd have bought would be using the future. Entry is the next trading day's
open; returns are measured at fixed trading-day horizons and against SPY and IWM over the same
days. Implausible filings are skipped the way the Discord bot skips them, Form 4/A restatements
count once, and funds and lockstep buys are flagged (and hidden by default).

Everything a row says about the cluster is as it stood at entry -- who had bought (and what kind
of buyer each was: routine, occasional, first buy), whether the CEO or CFO was among them, the
stock's move over the month before, and how much trades a day (a stand-in for size; today's
market cap would be hindsight).

Old clusters can't change, so `refresh()` (the Backtest job) computes them once, fetching each
ticker's price history from Yahoo, and stores the results in bt_events -- not the prices. The
view reads those and recomputes only recent clusters live, so it stays current on its own.

What this can't fix: a stock Yahoo has no prices for (often delisted -- the losers) drops out, so
results lean optimistic; a ticker reused by a later company is caught only when its price history
starts after the cluster; small, thinly traded stocks cost more to trade than their prices show.
"""
import bisect
import datetime as dt
import json
import logging
import sqlite3
import statistics as st

import requests

from . import alerts, background, earnings, issuers, prices, signals
from .net import PoliteSession

log = logging.getLogger(__name__)
HORIZONS = (5, 10, 20, 60, 120)   # trading days after entry
BENCHMARKS = ("SPY", "IWM")       # large caps; small caps, where most clusters are
LEVELS = (2, 3, 4)                # insiders needed for a cluster; each dated separately
GAP_DAYS = signals.CLUSTER_GAP_DAYS
MAX_FILING_VALUE = 1_000_000_000
LIVE_DAYS = 200                   # clusters this recent are recomputed on every view
TOO_FEW, READABLE = 30, 100
BENCH_FROM = "2014-10-01"


# ---------------------------------------------------------------- signals

def _bouts(trades):
    bouts, cur = [], []
    for t in sorted(trades, key=lambda t: t["trans_date"]):
        if cur and (dt.date.fromisoformat(t["trans_date"])
                    - dt.date.fromisoformat(cur[-1]["trans_date"])).days > GAP_DAYS:
            bouts.append(cur)
            cur = []
        cur.append(t)
    return bouts + ([cur] if cur else [])


def signals_for(trades, min_insiders=2) -> list:
    """For each bout that reached `min_insiders` different insiders: the signal as it stood when
    the filing that got it there came out (only trades filed by then)."""
    out = []
    for bout in _bouts(trades):
        seen, known = set(), []
        for t in sorted(bout, key=lambda t: (t["filed_date"], t["accession"])):
            seen.add(t["who"])
            known.append(t)
            if len(seen) >= min_insiders:
                out.append({"known_on": t["filed_date"], "first_buy": bout[0]["trans_date"],
                            "insiders": len(seen), "value": sum(x["value"] or 0 for x in known),
                            "trades": list(known),
                            "lockstep": signals.lockstep([{"owner_name": x["who"], "trans_date": x["trans_date"],
                                                           "price": x["price"]} for x in known])})
                break
    return out


def _signals_by_ticker(conn, since, today, levels, hide_noise):
    """{ticker: [(level, signal, flags, buyer types, top_exec)]} for clusters known from `since`."""
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=60)).isoformat()
    trades, _ = alerts.load_trades(conn, start, today, MAX_FILING_VALUE)
    first_filed = signals.data_start(conn) or today
    by_ticker = {}
    for t in trades:
        by_ticker.setdefault(t["ticker"], []).append(t)
    kinds = issuers.get_many(conn, {t["issuer_cik"] for t in trades})
    months = signals.purchase_months(conn, {t["issuer_cik"] for t in trades})
    reports = earnings.dates_many(conn, {t["issuer_cik"] for t in trades})
    out = {}
    for ticker, ts in by_ticker.items():
        info = kinds.get(str(int(ts[0]["issuer_cik"] or 0)))
        fund = issuers.is_fund(info, ticker)
        reit = bool(info and info.get("sic") == signals.REIT_SIC)
        for level in levels:
            for sig in signals_for(ts, level):
                if sig["known_on"] < max(since, first_filed):
                    continue  # before our data starts: nobody using this database could have seen it
                flags = ",".join(f for f, on in (("fund", fund), ("lockstep", sig["lockstep"]), ("reit", reit)) if on)
                if hide_noise and flags:
                    continue
                types = {}
                for t in sig["trades"]:
                    who = (t["owner_cik"] or "").split(";")[0].strip().lstrip("0")
                    kind = signals.buyer_type(months.get((t["issuer_cik"], who), set()), t["trans_date"],
                                              first_filed)
                    # an insider's earliest purchase in the bout decides what kind of buyer they were
                    types.setdefault(t["who"], kind)
                roles = set()
                for t in sig["trades"]:
                    roles |= signals.roles(t["relationship"], t["is_director"], t["is_officer"], t["is_ten_pct"])
                buyers = ",".join(k for k in signals.BUYER_ORDER if k in types.values())
                sig["since_results"] = earnings.days_since_results(
                    reports.get(str(int(ts[0]["issuer_cik"] or 0)), []), sig["first_buy"])
                out.setdefault(ticker, []).append((level, sig, flags, buyers,
                                                   ",".join(r for r in signals.ROLE_ORDER if r in roles),
                                                   ts[0]["issuer_name"]))
    return out


# ---------------------------------------------------------------- returns

def _row(ticker, issuer, level, sig, flags, buyers, roles, bars, bench):
    """One cluster's result row against these daily bars (and the benchmarks)."""
    days = [b["date"] for b in bars]
    row = {"ticker": ticker, "issuer_name": issuer, "level": level, "known_on": sig["known_on"],
           "first_buy": sig["first_buy"], "insiders": sig["insiders"], "total_value": sig["value"],
           "buyers": buyers, "roles": roles, "top_exec": int(bool({"CEO", "CFO"} & set(roles.split(",")))),
           "flags": flags, "since_results": sig.get("since_results"),
           "quarter_timing": earnings.timing(sig.get("since_results")), "entry_date": None, "days_held": None}
    i = bisect.bisect_right(days, sig["known_on"])
    while i < len(bars) and not bars[i]["open"]:
        i += 1
    if i >= len(bars) or (dt.date.fromisoformat(days[i]) - dt.date.fromisoformat(sig["known_on"])).days > 10:
        # nothing after it yet -- or prices only start long after (often a reused symbol)
        row["status"] = "no prices" if not bars or days[0] > sig["known_on"] else "awaiting entry"
        return row
    entry, entry_day = bars[i]["open"], days[i]
    row.update(entry_date=entry_day, status="open", days_held=len(bars) - i)
    for h in HORIZONS:
        if i + h - 1 < len(bars):
            row[f"ret_{h}d"] = 100 * (bars[i + h - 1]["close"] / entry - 1)
    row["ret_to_date"] = 100 * (bars[-1]["close"] / entry - 1)
    if i >= 21 and bars[i - 21]["close"]:  # the month before entry: buying into a fall, or a rise?
        row["prior_30d"] = 100 * (bars[i - 1]["close"] / bars[i - 21]["close"] - 1)
    vols = [b["close"] * b["volume"] for b in bars[max(0, i - 20):i] if b["close"] and b["volume"]]
    row["dollar_volume"] = st.mean(vols) if vols else None
    for b, (bd, bb) in bench.items():
        if not bb or bd[0] > entry_day:
            continue  # the benchmark's history doesn't reach back to this entry
        bi = bisect.bisect_left(bd, entry_day)
        bj = bisect.bisect_right(bd, days[-1]) - 1
        if bi >= len(bb) or bj < bi:
            continue
        row[f"vs_{b.lower()}"] = row["ret_to_date"] - 100 * (bb[bj]["close"] / bb[bi]["open"] - 1)
        for h in HORIZONS:  # excess at each horizon, for the summaries
            if f"ret_{h}d" in row:
                bk = bisect.bisect_right(bd, days[i + h - 1]) - 1
                row[f"_x{h}_{b}"] = row[f"ret_{h}d"] - 100 * (bb[bk]["close"] / bb[bi]["open"] - 1)
    return row


def _bars(conn, ticker, since):
    meta = prices.get_meta(conn, ticker)
    return prices.bars(conn, meta["symbol"], since) if meta and meta.get("symbol") else []


def _bench(conn):
    out = {}
    for b in BENCHMARKS:
        bb = prices.bars(conn, b, BENCH_FROM)
        out[b] = ([x["date"] for x in bb], bb)
    return out


def _key(r):
    return f"{r['ticker']}|{r['known_on']}|{r['level']}"


def live_events(conn, level=2, hide_noise=1, today=None) -> list:
    """Recent clusters (last LIVE_DAYS days), computed now from the cached prices."""
    today = today or dt.date.today().isoformat()
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=LIVE_DAYS)).isoformat()
    bench = _bench(conn)
    out = []
    for ticker, sigs in _signals_by_ticker(conn, since, today, (level,), hide_noise).items():
        bars = _bars(conn, ticker, (dt.date.fromisoformat(min(s[1]["known_on"] for s in sigs))
                                    - dt.timedelta(days=45)).isoformat())
        out += [_row(ticker, name, lv, sig, flags, buyers, top, bars, bench)
                for lv, sig, flags, buyers, top, name in sigs]
    return out


def stored_events(conn, level=2, hide_noise=1) -> list:
    rows = [json.loads(r[0]) for r in conn.execute("SELECT data FROM bt_events WHERE level=?", (level,))]
    return [r for r in rows if not (hide_noise and r.get("flags"))]


def events(conn, level=2, hide_noise=1, today=None) -> list:
    """Every cluster: stored history, with the recent ones recomputed live on top."""
    today = today or dt.date.today().isoformat()
    live = {_key(r): r for r in live_events(conn, level, hide_noise, today)}
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=LIVE_DAYS)).isoformat()
    rows = [r for r in stored_events(conn, level, hide_noise) if r["known_on"] < cutoff and _key(r) not in live]
    return sorted(rows + list(live.values()), key=lambda r: r["known_on"], reverse=True)


# ---------------------------------------------------------------- the job

def _yahoo_history(http, ticker, start):
    """Daily bars from `start` for the first Yahoo symbol this ticker could be; [] if none."""
    p1 = int(dt.datetime.fromisoformat(start).replace(tzinfo=dt.timezone.utc).timestamp())
    p2 = int((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).timestamp())
    for sym in prices.candidates(ticker):
        resp = http.get(prices.URL.format(symbol=sym), params={"interval": "1d", "period1": p1, "period2": p2})
        if resp.status_code == 404:
            continue
        resp.raise_for_status()
        bars = prices.parse_chart(resp.json())
        if bars:
            return bars
    return []


def _store_bench(conn, http):
    for b in BENCHMARKS:
        have = conn.execute("SELECT MIN(date) FROM prices WHERE symbol=?", (b,)).fetchone()[0]
        if have and have <= BENCH_FROM[:7] + "-31":
            continue
        rows = _yahoo_history(http, b, BENCH_FROM)
        conn.executemany("INSERT OR REPLACE INTO prices (symbol, date, open, high, low, close, adj_close, volume) "
                         "VALUES (:symbol, :date, :open, :high, :low, :close, :adj_close, :volume)",
                         [{**r, "symbol": b} for r in rows])
        conn.commit()


def refresh(conn, http=None, today=None, progress=None, only=None, sec_http=None) -> dict:
    """Compute and store every cluster that isn't stored yet as final (its 120-day return in, or
    known to have no prices). Resumable: each ticker is committed as it's done."""
    today = today or dt.date.today().isoformat()
    http = http or PoliteSession(prices.UA, 0.4, max_retries=2, timeout=20)
    _store_bench(conn, http)
    bench = _bench(conn)
    # fund or company, for every issuer in the history (the noise flags), before judging clusters
    ciks = [r[0] for r in conn.execute("SELECT DISTINCT issuer_cik FROM insider_tx WHERE code='P' AND issuer_cik<>''")]
    issuers.fetch_many(conn, issuers.missing(conn, ciks), sec_http)
    earnings.fetch_many(conn, earnings.missing(conn, ciks), sec_http)
    annotate(conn)  # stored rows from before roles were recorded
    final = {r[0] for r in conn.execute("SELECT key FROM bt_events WHERE final=1")}
    by_ticker = _signals_by_ticker(conn, signals.data_start(conn) or "2000-01-01", today, LEVELS, hide_noise=0)
    todo = {t: [s for s in sigs if f"{t}|{s[1]['known_on']}|{s[0]}" not in final] for t, sigs in by_ticker.items()}
    todo = {t: s for t, s in todo.items() if s and (only is None or t in only)}
    stored = fetched = 0
    for n, (ticker, sigs) in enumerate(sorted(todo.items()), 1):
        start = (dt.date.fromisoformat(min(s[1]["known_on"] for s in sigs)) - dt.timedelta(days=45)).isoformat()
        try:
            bars = _yahoo_history(http, ticker, start)
            fetched += 1
        except (requests.RequestException, ValueError) as exc:
            log.warning("backtest prices for %s failed (%s); will retry next run", ticker, exc)
            continue
        rows = []
        for lv, sig, flags, buyers, top, name in sigs:
            r = _row(ticker, name, lv, sig, flags, buyers, top, bars, bench)
            old = (dt.date.fromisoformat(today) - dt.date.fromisoformat(r["known_on"])).days > 365
            settled = int("ret_120d" in r or (r["status"] == "no prices" and old))
            rows.append((_key(r), lv, r["known_on"], settled, json.dumps(r)))
        try:
            conn.executemany("INSERT OR REPLACE INTO bt_events (key, level, known_on, final, data) VALUES (?,?,?,?,?)",
                             rows)
            conn.commit()
        except sqlite3.OperationalError as exc:  # another writer held the database too long: next run retries
            conn.rollback()
            log.warning("backtest results for %s not saved (%s); will retry next run", ticker, exc)
            continue
        stored += len(rows)
        if progress and n % 50 == 0:
            progress(n, len(todo))
    conn.execute("INSERT OR REPLACE INTO bt_meta (name, value) VALUES ('refreshed_at', ?)",
                 (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),))
    conn.commit()
    return {"tickers": len(todo), "fetched": fetched, "events": stored}


def refresh_in_background(db_path) -> bool:
    return background.start_db("backtest", db_path, lambda conn: refresh(conn))


def last_refresh(conn):
    r = conn.execute("SELECT value FROM bt_meta WHERE name='refreshed_at'").fetchone()
    return r[0] if r else None


# ---------------------------------------------------------------- summaries

def _reliability(n):
    return ("none yet" if not n else f"too few to read (under {TOO_FEW})" if n < TOO_FEW else
            f"small sample (under {READABLE})" if n < READABLE else "readable")


def summary(rows) -> list:
    """Per horizon: how many clusters have reached it, their median and mean return, how often
    they beat SPY, and median excess over SPY and IWM."""
    out = []
    for h in [*HORIZONS, None]:
        key = f"ret_{h}d" if h else "ret_to_date"
        got = [r for r in rows if r.get(key) is not None]
        spy = [r[f"_x{h}_SPY"] if h else r["vs_spy"] for r in got if (f"_x{h}_SPY" if h else "vs_spy") in r]
        iwm = [r[f"_x{h}_IWM"] if h else r["vs_iwm"] for r in got if (f"_x{h}_IWM" if h else "vs_iwm") in r]
        out.append({
            "horizon": f"{h} trading days" if h else "to latest close",
            "events": len(got),
            "median_return": st.median(r[key] for r in got) if got else None,
            "mean_return": st.mean(r[key] for r in got) if got else None,
            "beat_spy": 100 * sum(x > 0 for x in spy) / len(spy) if spy else None,
            "median_vs_spy": st.median(spy) if spy else None,
            "median_vs_iwm": st.median(iwm) if iwm else None,
            "reliability": _reliability(len(got)),
        })
    return out


FACTOR_HORIZON = 60  # the factor tables compare clusters on their 60-trading-day excess return


def trimmed_mean(xs, cut=0.01):
    """The average with the most extreme 1% at each end left out, so one freak result (a +1,000%
    biotech, a data error) can't carry it. Closest to what holding every cluster equally earned."""
    xs = sorted(xs)
    k = int(len(xs) * cut)
    xs = xs[k:len(xs) - k] if k else xs
    return st.mean(xs) if xs else None


def _slice(label, rows, sel, h=FACTOR_HORIZON):
    got = [r for r in rows if f"_x{h}_IWM" in r and sel(r)]
    x = [r[f"_x{h}_IWM"] for r in got]
    return {"group": label, "events": len(got),
            "avg_vs_iwm": trimmed_mean(x),
            "median_vs_iwm": st.median(x) if x else None,
            "beat_iwm": 100 * sum(v > 0 for v in x) / len(x) if x else None,
            "reliability": _reliability(len(got))}


def annotate(conn) -> int:
    """Add buyer roles and the REIT flag to stored results that predate them, from the filings
    alone (no prices needed). Returns how many rows were updated."""
    rows = [(k, json.loads(d)) for k, d in conn.execute("SELECT key, data FROM bt_events")]
    _sync_fund_flags(conn, rows)
    updates = _annotate_timing(conn, [(k, r) for k, r in rows if "quarter_timing" not in r and "roles" in r])
    todo = [(k, r) for k, r in rows if "roles" not in r]
    if not todo:
        return updates
    sic = {r[0]: r[1] for r in conn.execute("SELECT cik, sic FROM issuer_info")}
    cik_of = {r[0]: str(int(r[1] or 0)) for r in conn.execute(
        "SELECT ticker, MAX(issuer_cik) FROM insider_tx WHERE code='P' AND ticker<>'' GROUP BY ticker")}
    reports = earnings.dates_many(conn, set(cik_of.values()))
    updates = []
    for k, r in todo:
        roles, ciks = set(), set()
        for t in conn.execute(
                "SELECT relationship, is_director, is_officer, is_ten_pct, issuer_cik FROM insider_tx "
                "WHERE ticker=? AND code='P' AND is_derivative=0 AND plan_10b5_1=0 "
                "AND trans_date BETWEEN ? AND ? AND filed_date<=?",
                (r["ticker"], r["first_buy"], r["known_on"], r["known_on"])):
            roles |= signals.roles(t[0], t[1], t[2], t[3])
            ciks.add(str(int(t[4] or 0)))
        r["roles"] = ",".join(x for x in signals.ROLE_ORDER if x in roles)
        if any(sic.get(c) == signals.REIT_SIC for c in ciks) and "reit" not in (r.get("flags") or ""):
            r["flags"] = ",".join(x for x in ((r.get("flags") or "").split(",") + ["reit"]) if x)
        _set_timing(r, cik_of, reports)
        updates.append((json.dumps(r), k))
    conn.executemany("UPDATE bt_events SET data=? WHERE key=?", updates)
    conn.commit()
    return len(updates)


def _sync_fund_flags(conn, rows):
    """Keep stored rows' 'fund' flag in line with issuer_info, so a classification fix (like the
    funds whose SEC records say SIC 0000) reaches results stored before it."""
    cik_of = {r[0]: str(int(r[1] or 0)) for r in conn.execute(
        "SELECT ticker, MAX(issuer_cik) FROM insider_tx WHERE code='P' AND ticker<>'' GROUP BY ticker")}
    kinds = issuers.get_many(conn, set(cik_of.values()))
    updates = []
    for k, r in rows:
        fund = issuers.is_fund(kinds.get(cik_of.get(r["ticker"])), r["ticker"])
        flags = [f for f in (r.get("flags") or "").split(",") if f and f != "fund"]
        new = ",".join((["fund"] if fund else []) + flags)
        if new != (r.get("flags") or ""):
            r["flags"] = new
            updates.append((json.dumps(r), k))
    if updates:
        conn.executemany("UPDATE bt_events SET data=? WHERE key=?", updates)
        conn.commit()
    return len(updates)


def _set_timing(r, cik_of, reports):
    r["since_results"] = earnings.days_since_results(reports.get(cik_of.get(r["ticker"]), []), r["first_buy"])
    r["quarter_timing"] = earnings.timing(r["since_results"])


def _annotate_timing(conn, todo) -> int:
    """Quarter timing for stored rows that lack it, from the earnings table (no prices needed)."""
    if not todo:
        return 0
    cik = {r[0]: str(int(r[1] or 0)) for r in conn.execute(
        "SELECT ticker, MAX(issuer_cik) FROM insider_tx WHERE code='P' AND ticker<>'' GROUP BY ticker")}
    reports = earnings.dates_many(conn, set(cik.values()))
    updates = []
    for k, r in todo:
        _set_timing(r, cik, reports)
        updates.append((json.dumps(r), k))
    conn.executemany("UPDATE bt_events SET data=? WHERE key=?", updates)
    conn.commit()
    return len(updates)


TRACK_FROM = "2026-10-24"  # the CFO screen was chosen on data up to here: later clusters test it fresh
has_cfo = lambda r: "CFO" in (r.get("roles") or "").split(",")
cfo_mid = lambda r: has_cfo(r) and r.get("quarter_timing") == earnings.MID_QUARTER


def groups(rows, caps=None) -> list:
    """Which kinds of cluster have done better: median 60-day excess over IWM by each factor known
    at entry. caps is unused (kept for callers): size is judged by dollar volume at entry."""
    dv = lambda r: r.get("dollar_volume") or 0
    pr = lambda r: r.get("prior_30d")
    return [
        _slice("all clusters", rows, lambda r: True),
        _slice("a first-time buyer among them", rows, lambda r: "first buy" in (r.get("buyers") or "")),
        _slice("only routine buyers", rows, lambda r: (r.get("buyers") or "") == "routine"),
        _slice("CFO among the buyers", rows, has_cfo),
        _slice("CFO, bought 1-2 months after results", rows, cfo_mid),
        _slice("no CFO among the buyers", rows, lambda r: not has_cfo(r)),
        _slice("CEO or CFO among the buyers", rows, lambda r: r.get("top_exec")),
        _slice("no CEO or CFO", rows, lambda r: not r.get("top_exec")),
        _slice("stock fell 10%+ in the month before", rows, lambda r: pr(r) is not None and pr(r) <= -10),
        _slice("stock rose 10%+ in the month before", rows, lambda r: pr(r) is not None and pr(r) >= 10),
        _slice("trades under $1M a day", rows, lambda r: 0 < dv(r) < 1e6),
        _slice("trades $1M-$20M a day", rows, lambda r: 1e6 <= dv(r) < 2e7),
        _slice("trades over $20M a day", rows, lambda r: dv(r) >= 2e7),
        _slice("insiders bought over $1M", rows, lambda r: (r.get("total_value") or 0) >= 1e6),
    ]


def out_of_sample(rows) -> list:
    """Clusters since TRACK_FROM, the day the CFO screen was picked: the only fair test of it."""
    fresh = [r for r in rows if r["known_on"] >= TRACK_FROM]
    return [_slice(f"since {TRACK_FROM}: all clusters", fresh, lambda r: True),
            _slice(f"since {TRACK_FROM}: CFO among the buyers", fresh, has_cfo),
            _slice(f"since {TRACK_FROM}: CFO, 1-2 months after results", fresh, cfo_mid),
            _slice(f"since {TRACK_FROM}: no CFO", fresh, lambda r: not has_cfo(r))]


def by_period(rows) -> list:
    """The same 60-day comparison for the early years and the later ones: a factor that only works
    in one of them was probably luck. (Tune on the first, check on the second.)"""
    out = []
    for label, lo, hi in (("2015-2020 (tune on this)", "2015", "2021"), ("2021 onwards (check on this)", "2021", "9999")):
        part = [r for r in rows if lo <= r["known_on"][:4] < hi]
        for g in groups(part):
            out.append({**g, "group": f"{label}: {g['group']}"})
    return out
