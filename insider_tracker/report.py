"""Render signals to a Markdown report plus one CSV per section."""
import csv
import datetime as dt
from pathlib import Path

from . import signals


def _money(v):
    if v is None or v == "":
        return ""
    v = float(v)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"${v / div:.1f}{unit}"
    return f"${v:,.0f}"


def _fmt(key, v):
    if v is None:
        return ""
    if key in ("value", "total_value", "value_now", "min_total_usd"):
        return _money(v)
    if key == "price":
        return f"${v:,.2f}"
    if key in ("shares", "shares_after", "shares_prior", "shares_now") and isinstance(v, (int, float)):
        return f"{v:,.0f}"
    if key == "url":
        return f"[link]({v})"
    s = str(v).replace("|", "/")
    return s if len(s) <= 60 else s[:57] + "..."


def _table(rows, cols):
    if not rows:
        return "_Nothing in this window._\n"
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(_fmt(c, r.get(c)) for c in cols) + " |" for r in rows]
    return "\n".join(out) + "\n"


def _csv(path: Path, rows):
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


SECTIONS = [
    ("cluster_buys", "Insider cluster buys (2+ insiders, open market, no 10b5-1)",
     lambda c, s: signals.cluster_buys(c, s),
     ["ticker", "issuer_name", "insiders", "roles", "total_value", "first_buy", "last_buy", "who"]),
    ("large_buys", "Large insider purchases by officers and directors",
     lambda c, s: signals.large_buys(c, s),
     ["trans_date", "ticker", "owner_name", "relationship", "shares", "price", "value", "url"]),
    ("overlap", "Tickers bought by both Congress members and insiders",
     lambda c, s: signals.overlap(c, s),
     ["ticker", "members", "member_names", "insiders"]),
    ("congress_most_traded", "Most-traded tickers in Congress (by filing date)",
     lambda c, s: signals.congress_most_traded(c, s),
     ["ticker", "members", "buys", "sells", "min_bought_usd", "min_sold_usd",
      "bought_sold_ratio", "min_total_usd"]),
    ("congress_recent", "Recent congressional trades",
     lambda c, s: signals.congress_recent(c, s),
     ["filed_date", "trans_date", "member", "owner", "ticker", "asset", "tx_type", "amount_text", "lag_days"]),
    ("congress_late", "Late congressional filings (>45 days after trade)",
     lambda c, s: signals.congress_late(c, s),
     ["member", "chamber", "ticker", "trans_date", "filed_date", "lag_days", "amount_text"]),
    ("exec_sales", "Executive sales outside 10b5-1 plans (weaker signal)",
     lambda c, s: signals.exec_sales(c, s),
     ["trans_date", "ticker", "owner_name", "relationship", "value", "shares_after", "url"]),
    ("thirteenf_changes", "13F position changes (latest vs prior quarter)",
     lambda c, s: signals.thirteenf_changes(c),
     ["manager", "period", "change", "issuer", "ticker", "put_call", "shares_now", "pct_change", "value_now"]),
]


def build(conn, days: int, out_dir: Path) -> Path:
    today = dt.date.today()
    since = (today - dt.timedelta(days=days)).isoformat()
    run_dir = out_dir / today.isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)

    counts = conn.execute("""
        SELECT (SELECT COUNT(*) FROM insider_tx),
               (SELECT COUNT(*) FROM congress_tx),
               (SELECT COUNT(DISTINCT accession) FROM holdings_13f),
               (SELECT COUNT(*) FROM processed WHERE status='needs_manual'),
               (SELECT COUNT(*) FROM processed WHERE status='error')""").fetchone()
    md = [f"# Insider & Congressional Trading Report — {today:%B %d, %Y}",
          f"Window: last {days} days (since {since}).  ",
          f"Database: {counts[0]:,} insider transactions, {counts[1]:,} congressional trades, "
          f"{counts[2]} 13F filings. {counts[3]} filings need manual review (scanned/paper), "
          f"{counts[4]} errored and will retry next run.\n"]
    for key, title, fn, cols in SECTIONS:
        rows = fn(conn, since)
        _csv(run_dir / f"{key}.csv", rows)
        md.append(f"## {title}\n")
        md.append(_table(rows[:40], cols))
        if len(rows) > 40:
            md.append(f"_…{len(rows) - 40} more in {key}.csv_\n")
    md.append("\n---\nNot investment advice. Congressional amounts are disclosure ranges, "
              "not exact values, and trades may be reported up to 45 days late.")
    path = run_dir / "report.md"
    path.write_text("\n".join(md), encoding="utf-8")
    return path
