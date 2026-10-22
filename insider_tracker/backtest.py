"""How insider cluster buys have done since, replayed from whatever is in the database.

Recomputed every time it is asked for, so it stays current on its own as filings and prices come
in: there is no stored result to go stale and nothing to re-run.

A cluster counts only from the day it could have been seen: the FILING date of the purchase that
brought a second distinct insider (by SEC ID) into a bout of buying (purchases no more than 14
days apart). Entry is the next trading day's open; returns are measured at fixed trading-day
horizons and to the latest close, and against SPY and IWM over exactly the same days. Filings
that are implausible (a $10 trillion "purchase", a per-share typo) are skipped the way the Discord
bot skips them, and Form 4/A restatements count once.

What this can't fix: a stock Yahoo has no prices for (often delisted -- the losers) drops out, so
results lean optimistic; small, thinly traded stocks cost more to trade than their prices show;
and a few months of events from one market are a small, overlapping sample.
"""
import datetime as dt
import statistics as st

from . import alerts, prices, signals

HORIZONS = (5, 10, 20, 60, 120)   # trading days after entry
BENCHMARKS = ("SPY", "IWM")       # large caps; small caps, where most clusters are
GAP_DAYS = signals.CLUSTER_GAP_DAYS
MAX_FILING_VALUE = 1_000_000_000
TOO_FEW = 30                      # fewer events than this: say so next to the numbers
READABLE = 100                    # and below this, call it a small sample


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
    """[{known_on, first_buy, insiders, value}] for each bout that reached `min_insiders`
    different insiders, dated by the filing that got it there."""
    out = []
    for bout in _bouts(trades):
        seen = set()
        for t in sorted(bout, key=lambda t: (t["filed_date"], t["accession"])):
            seen.add(t["who"])
            if len(seen) >= min_insiders:
                out.append({"known_on": t["filed_date"], "first_buy": bout[0]["trans_date"],
                            "insiders": len({x["who"] for x in bout}),
                            "value": sum(x["value"] or 0 for x in bout)})
                break
    return out


def _bars(conn, ticker, since):
    meta = prices.get_meta(conn, ticker)
    return prices.bars(conn, meta["symbol"], since) if meta and meta.get("symbol") else []


def events(conn, min_insiders=2, since="2000-01-01", today=None) -> list:
    """One row per cluster: when it became known, entry, and its returns so far, in percent."""
    today = today or dt.date.today().isoformat()
    start = (dt.date.fromisoformat(since) - dt.timedelta(days=60)).isoformat()
    trades, _ = alerts.load_trades(conn, start, today, MAX_FILING_VALUE)
    first_filed = conn.execute("SELECT MIN(filed_date) FROM insider_tx").fetchone()[0] or today
    by_ticker = {}
    for t in trades:
        by_ticker.setdefault(t["ticker"], []).append(t)
    bench = {b: _bars(conn, b, start) for b in BENCHMARKS}
    out = []
    for ticker, ts in by_ticker.items():
        for sig in signals_for(ts, min_insiders):
            # a cluster "known" before our first filing is one we never could have seen
            if sig["known_on"] < max(since, first_filed):
                continue
            bars = _bars(conn, ticker, sig["known_on"])
            row = {"ticker": ticker, "issuer_name": ts[0]["issuer_name"], "known_on": sig["known_on"],
                   "first_buy": sig["first_buy"], "insiders": sig["insiders"], "total_value": sig["value"],
                   "entry_date": None, "days_held": None}
            i = next((k for k, b in enumerate(bars) if b["date"] > sig["known_on"] and b["open"]), None)
            if i is None:
                row["status"] = "no prices yet" if not bars else "awaiting entry"
                out.append(row)
                continue
            entry, entry_day = bars[i]["open"], bars[i]["date"]
            row.update(entry_date=entry_day, status="open", days_held=len(bars) - i)
            for h in HORIZONS:
                if i + h - 1 < len(bars):
                    row[f"ret_{h}d"] = 100 * (bars[i + h - 1]["close"] / entry - 1)
            row["ret_to_date"] = 100 * (bars[-1]["close"] / entry - 1)
            for b, bb in bench.items():
                bi = next((k for k, x in enumerate(bb) if x["date"] >= entry_day), None)
                bj = max((k for k, x in enumerate(bb) if x["date"] <= bars[-1]["date"]), default=None)
                if bi is not None and bj is not None and bj >= bi:
                    row[f"vs_{b.lower()}"] = row["ret_to_date"] - 100 * (bb[bj]["close"] / bb[bi]["open"] - 1)
                    for h in HORIZONS:  # excess at each horizon, for the summary
                        if f"ret_{h}d" in row:
                            end = bars[i + h - 1]["date"]
                            bk = max(k for k, x in enumerate(bb) if x["date"] <= end)
                            row[f"_x{h}_{b}"] = row[f"ret_{h}d"] - 100 * (bb[bk]["close"] / bb[bi]["open"] - 1)
            out.append(row)
    return sorted(out, key=lambda r: r["known_on"], reverse=True)


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
            "reliability": ("none yet" if not got else
                            f"too few to read (under {TOO_FEW})" if len(got) < TOO_FEW else
                            f"small sample (under {READABLE})" if len(got) < READABLE else "readable"),
        })
    return out


def groups(rows, caps) -> list:
    """Median excess over IWM to date, by company size and by how many insiders bought."""
    def bucket(label, sel):
        got = [r for r in rows if r.get("vs_iwm") is not None and sel(r)]
        return {"group": label, "events": len(got),
                "median_vs_iwm": st.median(r["vs_iwm"] for r in got) if got else None,
                "median_vs_spy": st.median(r["vs_spy"] for r in got) if got else None}
    cap = lambda r: (caps.get(r["ticker"]) or {}).get("market_cap")
    return [bucket("market cap under $300M", lambda r: cap(r) is not None and cap(r) < 3e8),
            bucket("market cap $300M to $2B", lambda r: cap(r) is not None and 3e8 <= cap(r) < 2e9),
            bucket("market cap over $2B", lambda r: cap(r) is not None and cap(r) >= 2e9),
            bucket("2 insiders", lambda r: r["insiders"] == 2),
            bucket("3 or more insiders", lambda r: r["insiders"] >= 3)]
