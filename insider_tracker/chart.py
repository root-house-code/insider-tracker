"""Monochrome SVG price chart with trade markers. Pure functions, no I/O.

Drawn on the server as inline SVG: no JavaScript library, prints cleanly, and matches the
black-and-white look of the rest of the app. Colours are CSS variables (var(--fg) ...) so it
follows light/dark; the Copy PNG button swaps them for real values when it rasterises this.

Marker vocabulary (shapes, never colour):
    line        daily close
    up-triangle one or more insider open-market buys that day, at the price paid
    filled dot  congressional purchase    hollow dot  congressional sale (at that day's close;
                Congress discloses amount ranges, not prices)
    hatched box a cluster's first-buy to last-buy window
"""
import bisect
import datetime as dt
import html
import math

W, H = 1000, 430
ML, MR, MT, MB = 62, 16, 40, 58   # MT holds the title line, MB the x axis and the legend

FG, MUTED, RULE, BG = "var(--fg)", "var(--muted)", "var(--rule)", "var(--bg)"
FONT = "font:11px -apple-system,'Segoe UI',system-ui,sans-serif"
# A reported trade price this far from that day's close is treated as bad data or a split
# artefact: the marker goes at the close, and its tooltip says so.
SANE = (0.5, 2.0)


def close_on(dates: list, closes: list, day: str):
    """Last close on or before `day` (weekends, holidays), or None if before the first bar."""
    i = bisect.bisect_right(dates, day) - 1
    return closes[i] if i >= 0 else None


def insider_markers(rows, dates, closes) -> list:
    """One marker per day. rows: dicts with trans_date, owner_name, shares, price, value."""
    by_day = {}
    for r in rows:
        by_day.setdefault(r["trans_date"], []).append(r)
    out = []
    for day, rs in sorted(by_day.items()):
        shares = sum(r["shares"] or 0 for r in rs if r.get("price"))
        value = sum(r["value"] or 0 for r in rs if r.get("price"))
        vwap = value / shares if shares else None
        close = close_on(dates, closes, day)
        y, note = vwap, ""
        if vwap is not None and close and not (SANE[0] <= vwap / close <= SANE[1]):
            y, note = close, (f" Reported price ${vwap:,.2f} is far from that day's close "
                              f"(${close:,.2f}), so it is drawn at the close.")
        if y is None:
            y = close
        if y is None:
            continue
        who = sorted({r["owner_name"] for r in rs})
        label = (f"{day}: {len(who)} insider{'s' if len(who) != 1 else ''} bought "
                 f"{sum(r['shares'] or 0 for r in rs):,.0f} shares"
                 + (f", ${value:,.0f}, avg ${vwap:,.2f}" if vwap else "") + f". {'; '.join(who[:4])}"
                 + (f" +{len(who) - 4} more" if len(who) > 4 else "") + "." + note)
        out.append({"date": day, "y": y, "label": label})
    return out


def congress_markers(rows, dates, closes) -> list:
    """One marker per (day, buy/sale). rows: dicts with trans_date, tx_type, member, amount_text."""
    groups = {}
    for r in rows:
        if r["tx_type"] in ("P", "S"):
            groups.setdefault((r["trans_date"], r["tx_type"]), []).append(r)
    out = []
    for (day, kind), rs in sorted(groups.items()):
        close = close_on(dates, closes, day)
        if close is None:
            continue
        who = sorted({r["member"] for r in rs})
        amounts = sorted({r["amount_text"] for r in rs if r["amount_text"]})
        out.append({"date": day, "y": close, "kind": kind,
                    "label": f"{day}: {len(rs)} congressional {'purchase' if kind == 'P' else 'sale'}"
                             f"{'s' if len(rs) != 1 else ''} -- {'; '.join(who[:4])}"
                             + (f" +{len(who) - 4} more" if len(who) > 4 else "")
                             + (f" ({'; '.join(amounts[:3])})" if amounts else "")})
    return out


def nice_ticks(lo: float, hi: float, n: int = 5) -> list:
    span = (hi - lo) or abs(hi) or 1.0
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if raw <= m * mag)
    v = math.ceil(lo / step) * step
    ticks = []
    while v <= hi + 1e-9:
        ticks.append(round(v, 10))
        v += step
    return ticks


def _money(v: float, step: float) -> str:
    return f"${v:,.0f}" if step >= 1 else f"${v:,.2f}"


def _month_starts(d0: dt.date, d1: dt.date, step: int) -> list:
    out, y, m = [], d0.year, d0.month
    while True:
        d = dt.date(y, m, 1)
        if d > d1:
            return out
        if d >= d0 and (d.month - 1) % step == 0:
            out.append(d)
        m += 1
        if m > 12:
            y, m = y + 1, 1


def build_svg(bars: list, buys: list, congress: list, bands: list, title: str = "") -> str:
    """bars: [{date, close}] ascending. Returns '' if there is nothing to draw a line through."""
    bars = [b for b in bars if b.get("close") is not None]
    if len(bars) < 2:
        return ""
    d0, d1 = dt.date.fromisoformat(bars[0]["date"]), dt.date.fromisoformat(bars[-1]["date"])
    span = max((d1 - d0).days, 1)
    closes = [b["close"] for b in bars]
    lo, hi = min(closes), max(closes)
    pad = (hi - lo) * 0.06 or hi * 0.02 or 1.0
    lo, hi = lo - pad, hi + pad
    pw, ph = W - ML - MR, H - MT - MB

    def X(day: str) -> float:
        return ML + ((dt.date.fromisoformat(day) - d0).days / span) * pw

    def Y(v: float) -> float:
        return MT + (1 - (v - lo) / (hi - lo)) * ph

    inside = lambda day: bars[0]["date"] <= day <= bars[-1]["date"]
    e = html.escape
    out = [f'<svg class="chart" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
           f'width="{W}" height="{H}" role="img" aria-label="{e(title or "Price chart")}" '
           f'style="{FONT};width:100%;max-width:{W}px;height:auto;display:block">',
           f'<defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse">'
           f'<path d="M0,6 L6,0" style="stroke:{MUTED};stroke-width:1"/></pattern></defs>',
           f'<rect x="0" y="0" width="{W}" height="{H}" style="fill:{BG}"/>']

    if title:  # inside the svg so a copied PNG says what it is
        out.append(f'<text x="{ML}" y="22" style="fill:{FG};font-weight:700;font-size:14px">{e(title)}</text>')

    # y grid + labels
    ticks = nice_ticks(lo, hi)
    step = (ticks[1] - ticks[0]) if len(ticks) > 1 else 1
    for t in ticks:
        out.append(f'<line x1="{ML}" x2="{W - MR}" y1="{Y(t):.1f}" y2="{Y(t):.1f}" '
                   f'style="stroke:{RULE};stroke-width:1;stroke-dasharray:2 3"/>'
                   f'<text x="{ML - 7}" y="{Y(t) + 4:.1f}" text-anchor="end" style="fill:{MUTED}">'
                   f'{_money(t, step)}</text>')
    # x axis + labels
    months = span / 30.4
    mstep = 1 if months <= 8 else 2 if months <= 16 else 3 if months <= 24 else 6 if months <= 48 else 12
    out.append(f'<line x1="{ML}" x2="{W - MR}" y1="{MT + ph}" y2="{MT + ph}" style="stroke:{FG};stroke-width:1"/>')
    for i, d in enumerate(_month_starts(d0, d1, mstep)):
        x = X(d.isoformat())
        label = d.strftime("%b %Y") if (mstep >= 3 or d.month == 1 or i == 0) else d.strftime("%b")
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{MT + ph}" y2="{MT + ph + 4}" style="stroke:{FG}"/>'
                   f'<text x="{x:.1f}" y="{MT + ph + 17}" text-anchor="middle" style="fill:{MUTED}">{label}</text>')

    # cluster windows, behind everything else
    for b in bands:
        a, z = max(b["first"], bars[0]["date"]), min(b["last"], bars[-1]["date"])
        if a > z:
            continue
        x1, x2 = X(a), X(z)
        w = max(x2 - x1, 5.0)
        out.append(f'<g><title>{e(b["label"])}</title>'
                   f'<rect x="{x1 - (w - (x2 - x1)) / 2:.1f}" y="{MT}" width="{w:.1f}" height="{ph}" '
                   f'style="fill:url(#hatch);stroke:{MUTED};stroke-width:1;stroke-dasharray:3 2"/></g>')

    pts = " ".join(f"{X(b['date']):.1f},{Y(b['close']):.1f}" for b in bars)
    out.append(f'<polyline points="{pts}" style="fill:none;stroke:{FG};stroke-width:1.6;'
               f'stroke-linejoin:round"/>')

    for m in congress:
        if not inside(m["date"]):
            continue
        x, y = X(m["date"]), Y(m["y"])
        fill, sw = (FG, 1) if m["kind"] == "P" else (BG, 1.6)
        out.append(f'<g><title>{e(m["label"])}</title><circle cx="{x:.1f}" cy="{y:.1f}" r="4.6" '
                   f'style="fill:{fill};stroke:{BG if m["kind"] == "P" else FG};stroke-width:{sw}"/></g>')
    for m in buys:
        if not inside(m["date"]):
            continue
        x, y = X(m["date"]), Y(m["y"])
        out.append(f'<g><title>{e(m["label"])}</title><path d="M{x:.1f},{y - 7:.1f} L{x - 6:.1f},{y + 4:.1f} '
                   f'L{x + 6:.1f},{y + 4:.1f} Z" style="fill:{FG};stroke:{BG};stroke-width:1.2"/></g>')

    # legend, inside the svg so a copied PNG explains itself
    ly = H - 12
    items = [("tri", "insider buy"), ("dot", "congress buy"), ("ring", "congress sale"),
             ("box", "cluster buying window"), ("line", "daily close")]
    x = ML
    for kind, text in items:
        if kind == "tri":
            out.append(f'<path d="M{x + 6},{ly - 9} L{x},{ly + 1} L{x + 12},{ly + 1} Z" style="fill:{FG}"/>')
        elif kind == "dot":
            out.append(f'<circle cx="{x + 6}" cy="{ly - 4}" r="4.6" style="fill:{FG}"/>')
        elif kind == "ring":
            out.append(f'<circle cx="{x + 6}" cy="{ly - 4}" r="4.6" style="fill:{BG};stroke:{FG};stroke-width:1.6"/>')
        elif kind == "box":
            out.append(f'<rect x="{x}" y="{ly - 10}" width="12" height="11" style="fill:url(#hatch);'
                       f'stroke:{MUTED};stroke-dasharray:3 2"/>')
        else:
            out.append(f'<line x1="{x}" x2="{x + 14}" y1="{ly - 4}" y2="{ly - 4}" style="stroke:{FG};stroke-width:1.6"/>')
        out.append(f'<text x="{x + 20}" y="{ly}" style="fill:{FG}">{text}</text>')
        x += 20 + 6.4 * len(text) + 26
    out.append("</svg>")
    return "".join(out)
