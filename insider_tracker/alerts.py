"""Post Form 4 insider buys that pass configurable rules to a Discord channel.

Designed to run unattended once a night (see DEPLOY.md):

    python -m insider_tracker alerts --ingest-days 4     # pull new filings, post what qualifies
    python -m insider_tracker alerts --dry-run           # print what would be posted
    python -m insider_tracker alerts --backtest 20       # what these rules would have posted

Rules live in a TOML file (alerts.example.toml documents every key). Two rule types:

  cluster_buy  several different insiders at one company buying on the open market within a
               window -- the strongest signal this tool has.
  large_buy    one insider's open-market purchase in a single filing above a dollar amount.

Both only ever look at open-market purchases (code P) of the stock itself, outside 10b5-1
plans -- the same population as the Signals tab.

Three protections, each from something seen in real data:
  * An insider is identified by their SEC ID (the first CIK on the filing), not their name.
    A joint filing ("AULT MILTON C III; Ault & Company") is the same person as their solo
    filing, so it can't make a one-person "cluster".
  * Every trade has a fingerprint (ticker, insider, date, shares, price). A Form 4/A that
    restates a trade has the same fingerprint, so an amendment never re-posts. A cluster
    re-posts only when a genuinely new trade joins it, and marks what's new.
  * Filings claiming more than settings.max_filing_value are skipped as implausible -- one
    real filing put a $2.27M total into the per-share price field, a "$10 trillion" buy.

Text written by filers goes into Discord with markdown escaped and mentions disabled, and
the webhook URL (a secret) is never written to logs or error messages.
"""
import datetime as dt
import logging
import math
import re
import time
import tomllib
from collections import defaultdict
from pathlib import Path

import requests

from . import signals

log = logging.getLogger(__name__)

COMMON_KEYS = {"name", "type", "tickers", "exclude_tickers", "webhook_env", "cooldown_days"}
RULE_KEYS = {
    "cluster_buy": COMMON_KEYS | {"window_days", "min_insiders", "min_value_per_insider", "min_total_value"},
    "large_buy": COMMON_KEYS | {"window_days", "min_value", "roles"},
}
RULE_DEFAULTS = {
    "cluster_buy": {"window_days": 14, "min_insiders": 2, "min_value_per_insider": 25_000,
                    "min_total_value": 250_000, "cooldown_days": 0},
    "large_buy": {"window_days": 14, "min_value": 500_000, "roles": ["officer", "director"],
                  "cooldown_days": 7},
}
SETTINGS_DEFAULTS = {"max_age_days": 4, "max_filing_value": 1_000_000_000,
                     "footer": "Not investment advice.", "webhook_env": "DISCORD_WEBHOOK_URL",
                     "username": "insider-tracker"}
ROLES = {"officer": "is_officer", "director": "is_director", "ten_pct": "is_ten_pct"}
NUMBER_KEYS = {"window_days", "min_insiders", "min_value_per_insider", "min_total_value",
               "min_value", "max_age_days", "max_filing_value", "cooldown_days"}
MAX_LINES = 15          # insiders listed per post before "+N more"
DESC_LIMIT = 4000       # Discord's embed description limit is 4096
STALE_WEEKDAYS = 3      # EDGAR takes hundreds of Form 4s every business day; this many quiet ones is a fault


class ConfigError(Exception):
    pass


class DiscordError(Exception):
    pass


# ---------------------------------------------------------------- configuration

def load_config(path) -> dict:
    """Read and validate the rules file. Every mistake is reported at once, by name, because the
    person editing it will be on a server with nothing else to go on."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"{path} not found -- copy alerts.example.toml to {path.name} and edit it")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    problems = []
    unknown_top = set(raw) - {"settings", "rule"}
    if unknown_top:
        problems.append(f"unknown section(s): {', '.join(sorted(unknown_top))} (expected [settings] and [[rule]])")

    settings = dict(SETTINGS_DEFAULTS)
    given = raw.get("settings", {})
    for k in set(given) - set(SETTINGS_DEFAULTS):
        problems.append(f"[settings] unknown key '{k}' (allowed: {', '.join(sorted(SETTINGS_DEFAULTS))})")
    settings.update({k: v for k, v in given.items() if k in SETTINGS_DEFAULTS})

    rules, names = [], set()
    if not raw.get("rule"):
        problems.append("no [[rule]] sections: nothing would ever be posted")
    for i, r in enumerate(raw.get("rule", []), 1):
        label = f"rule #{i}" + (f" ('{r['name']}')" if r.get("name") else "")
        rtype = r.get("type")
        if rtype not in RULE_KEYS:
            problems.append(f"{label}: type must be one of {', '.join(RULE_KEYS)} (got {rtype!r})")
            continue
        for k in set(r) - RULE_KEYS[rtype]:
            problems.append(f"{label}: unknown key '{k}' for a {rtype} rule "
                            f"(allowed: {', '.join(sorted(RULE_KEYS[rtype]))})")
        rule = {**RULE_DEFAULTS[rtype], "tickers": [], "exclude_tickers": [],
                "webhook_env": settings["webhook_env"], **r}
        if not rule.get("name"):
            problems.append(f"{label}: needs a name (it is how already-posted trades are remembered)")
        elif rule["name"] in names:
            problems.append(f"{label}: name '{rule['name']}' is used twice")
        names.add(rule.get("name"))
        for k in ("roles",) if rtype == "large_buy" else ():
            bad = [x for x in rule[k] if x not in ROLES]
            if bad or not rule[k]:
                problems.append(f"{label}: roles must be a non-empty list of {', '.join(ROLES)} (got {rule[k]!r})")
        for k in ("tickers", "exclude_tickers"):
            if not isinstance(rule[k], list) or not all(isinstance(t, str) for t in rule[k]):
                problems.append(f"{label}: {k} must be a list of ticker strings")
            else:
                rule[k] = [t.strip().upper() for t in rule[k] if t.strip()]
        rules.append(rule)
    for scope in [settings] + rules:
        for k, v in scope.items():
            if k in NUMBER_KEYS and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
                problems.append(f"{scope.get('name', '[settings]')}: {k} must be a non-negative number (got {v!r})")
    if problems:
        raise ConfigError(f"{path}:\n  - " + "\n  - ".join(problems))
    return {"settings": settings, "rules": rules}


# ---------------------------------------------------------------- data

def _who(row) -> str:
    """One insider = the first CIK on the filing (joint filings list the person first)."""
    cik = (row["owner_cik"] or "").split(";")[0].strip().lstrip("0")
    return cik or (row["owner_name"] or "").split(";")[0].strip().upper()


def fingerprint(row) -> str:
    return "|".join([row["ticker"], _who(row), row["trans_date"] or "",
                     f"{row['shares'] or 0:g}", f"{row['price'] or 0:g}"])


PEER_DAYS = 45          # how far around a trade to look for the company's other market trades
PEER_BAND = (0.2, 5.0)  # a price this far off the typical one is a typo, not a trade
PEER_MIN = 2            # need at least this many other filings before judging


def _price_outliers(conn, trades, until: str) -> dict:
    """Filings whose price is wildly off the same company's other open-market trades (buys and
    sales, any insider). Catches typos the dollar cap can't: one real filing reported $5,134 a
    share for a stock the same insider bought at about $5.00 all month. Returns {accession: reason}."""
    if not trades:
        return {}
    lo = (dt.date.fromisoformat(min(t["trans_date"] for t in trades)) - dt.timedelta(days=PEER_DAYS)).isoformat()
    peers = defaultdict(list)
    for r in conn.execute(
            """SELECT issuer_cik, ticker, accession, price FROM insider_tx
               WHERE code IN ('P','S') AND is_derivative=0 AND price>0
                 AND trans_date>=? AND filed_date<=?""", (lo, until)):
        peers[r["issuer_cik"] or r["ticker"]].append((r["accession"], r["price"]))
    out = {}
    for t in trades:
        if not t["price"] or t["accession"] in out:
            continue
        others = sorted(p for acc, p in peers[t["issuer_cik"] or t["ticker"]] if acc != t["accession"])
        if len({acc for acc, _ in peers[t["issuer_cik"] or t["ticker"]] if acc != t["accession"]}) < PEER_MIN:
            continue
        typical = others[len(others) // 2]
        if not PEER_BAND[0] <= t["price"] / typical <= PEER_BAND[1]:
            out[t["accession"]] = f"reports ${t['price']:,.2f}/share vs about ${typical:,.2f} in its other trades"
    return out


def load_trades(conn, since: str, until: str, max_filing_value: float):
    """Open-market buys traded since `since` and filed by `until`, one row per real trade
    (amendments collapsed), with implausible filings removed. Returns (trades, skipped) where
    skipped is {accession: (ticker, reason)}."""
    rows = conn.execute(
        """SELECT ticker, issuer_name, issuer_cik, owner_name, owner_cik, relationship,
                  is_officer, is_director, is_ten_pct, trans_date, filed_date, shares, price,
                  value, shares_after, security, direct_indirect, accession, url, form_type
           FROM insider_tx
           WHERE code='P' AND is_derivative=0 AND plan_10b5_1=0 AND ticker<>''
             AND trans_date>=? AND filed_date<=?
           ORDER BY filed_date, accession, seq""", (since, until)).fetchall()
    per_filing = defaultdict(float)
    for r in rows:
        per_filing[r["accession"]] += r["value"] or 0
    trades, seen, skipped = [], set(), {}
    for r in rows:
        if per_filing[r["accession"]] > max_filing_value:
            skipped[r["accession"]] = (r["ticker"], f"claims {_money(per_filing[r['accession']])} in one filing")
            continue
        t = dict(r)
        t["fp"], t["who"] = fingerprint(t), _who(t)
        if t["fp"] in seen:  # the same trade restated by a later 4/A
            continue
        seen.add(t["fp"])
        trades.append(t)
    odd = _price_outliers(conn, trades, until)
    for t in trades:
        if t["accession"] in odd:
            skipped[t["accession"]] = (t["ticker"], odd[t["accession"]])
    return [t for t in trades if t["accession"] not in odd], skipped


def _ticker_ok(rule, ticker) -> bool:
    tickers = {p.strip().upper() for p in re.split(r"[,;]", ticker)}
    if rule["tickers"] and not tickers & set(rule["tickers"]):
        return False
    return not tickers & set(rule["exclude_tickers"])


def _role(t) -> str:
    return (t["relationship"] or "").strip() or "insider"


def _insiders(trades, conn=None) -> list:
    by = defaultdict(list)
    for t in trades:
        by[t["who"]].append(t)
    out = []
    for who, ts in by.items():
        shares = sum(t["shares"] or 0 for t in ts)
        value = sum(t["value"] or 0 for t in ts)
        names = sorted({(t["owner_name"] or "").split(";")[0].strip() for t in ts})
        out.append({"who": who, "name": names[0] if names else who, "role": _role(ts[0]),
                    "shares": shares, "value": value, "avg_price": value / shares if shares else None,
                    "trades": ts, "first": min(t["trans_date"] for t in ts),
                    "last": max(t["trans_date"] for t in ts),
                    # growth of everything they hold, other trusts included when we have conn
                    "stake": signals.insider_stake(conn, ts) if conn else signals.stake_change(ts)})
    return sorted(out, key=lambda i: -i["value"])


# ---------------------------------------------------------------- rules

def evaluate(conn, cfg, today: dt.date, is_notified, last_posted=lambda rule, group: None) -> tuple:
    """Alerts due as of `today`. is_notified(rule_name, fingerprint) -> bool; last_posted(rule_name,
    group) -> date of that rule's last post about the group, or None. An alert inside its rule's
    cooldown comes back with suppressed=True: it should be recorded as handled, not posted.
    Returns (alerts, skipped_filings)."""
    s = cfg["settings"]
    fresh_from = (today - dt.timedelta(days=s["max_age_days"])).isoformat()
    longest = max(r["window_days"] for r in cfg["rules"])
    trades, skipped = load_trades(conn, (today - dt.timedelta(days=longest)).isoformat(),
                                  today.isoformat(), s["max_filing_value"])
    is_new = lambda rule, t: t["filed_date"] >= fresh_from and not is_notified(rule["name"], t["fp"])
    alerts = []
    for rule in cfg["rules"]:
        since = (today - dt.timedelta(days=rule["window_days"])).isoformat()
        pool = [t for t in trades if t["trans_date"] >= since and _ticker_ok(rule, t["ticker"])]
        if rule["type"] == "cluster_buy":
            by_issuer = defaultdict(list)
            for t in pool:
                by_issuer[t["issuer_cik"] or t["ticker"]].append(t)
            for issuer, ts in by_issuer.items():
                ins = [i for i in _insiders(ts, conn) if i["value"] >= rule["min_value_per_insider"]]
                total = sum(i["value"] for i in ins)
                if len(ins) < rule["min_insiders"] or total < rule["min_total_value"]:
                    continue
                members = [t for i in ins for t in i["trades"]]
                new = [t for t in members if is_new(rule, t)]
                if new:
                    alerts.append(_alert(rule, members, ins, new, total, issuer, today, last_posted, is_notified))
        else:  # large_buy: one insider, one filing
            by_filing = defaultdict(list)
            for t in pool:
                if any(t[ROLES[r]] for r in rule["roles"]):
                    by_filing[(t["accession"], t["who"])].append(t)
            for ts in by_filing.values():
                ins = _insiders(ts, conn)
                total = sum(i["value"] for i in ins)
                new = [t for t in ts if is_new(rule, t)]
                if total >= rule["min_value"] and new:
                    group = f"{ts[0]['issuer_cik'] or ts[0]['ticker']}|{ts[0]['who']}"
                    alerts.append(_alert(rule, ts, ins, new, total, group, today, last_posted, is_notified))
    return alerts, skipped


def _alert(rule, members, insiders, new, total, group, today, last_posted, is_notified) -> dict:
    last = last_posted(rule["name"], group)
    suppressed = bool(rule["cooldown_days"] and last and (today - last).days < rule["cooldown_days"])
    newfps = {t["fp"] for t in new}
    for i in insiders:
        i["is_new"] = any(t["fp"] in newfps for t in i["trades"])
    tickers = sorted({t["ticker"] for t in members}, key=len)
    filings = {}
    for t in sorted(new, key=lambda t: t["filed_date"]):
        filings.setdefault(t["accession"], t["url"])
    return {"rule": rule["name"], "type": rule["type"], "webhook_env": rule["webhook_env"],
            "ticker": tickers[0], "issuer": members[0]["issuer_name"] or "", "insiders": insiders,
            "total": total, "first": min(t["trans_date"] for t in members),
            "last": max(t["trans_date"] for t in members),
            "fps": sorted({t["fp"] for t in members}), "new_links": list(filings.values()),
            # an update only if this rule really posted part of it before -- not merely because
            # some of its trades were too old to post (that's every cluster on a first run)
            "follow_up": any(is_notified(rule["name"], t["fp"]) for t in members),
            "group": group, "suppressed": suppressed}


# ---------------------------------------------------------------- rendering

_MD = re.compile(r"([\\*_~`|>\[\]()#])")


def md(text) -> str:
    """Filer-written text, made inert in Discord: no bold/links/headings smuggled in."""
    return _MD.sub(r"\\\1", str(text or ""))


def _money(v) -> str:
    return f"${v:,.0f}"


def _dates(a, b) -> str:
    return a if a == b else f"{a} to {b}"


def _stake(i) -> str:
    """How much the buys grew the insider's holding (signals.insider_stake)."""
    pct = i.get("stake")
    if pct is None:
        return ""
    return ", new position" if pct == math.inf else f", {signals.stake_text(pct)} to holding"


def describe(alert) -> tuple:
    """(title, description lines) shared by the Discord embed and the dry-run printout."""
    kind = "cluster buy" if alert["type"] == "cluster_buy" else "large buy"
    title = f"{alert['ticker']} · {alert['issuer']} — {kind}"
    if alert["follow_up"]:
        title += " (update)"
    n = len(alert["insiders"])
    head = (f"**{n} insider{'s' if n != 1 else ''}** · **{_money(alert['total'])}** · "
            f"{_dates(alert['first'], alert['last'])}")
    lines = [head, ""]
    for i in alert["insiders"][:MAX_LINES]:
        price = f" @ ${i['avg_price']:,.2f}" if i["avg_price"] else ""
        mark = "🆕 " if alert["follow_up"] and i["is_new"] else ""
        lines.append(f"{mark}{md(i['name'])} ({md(i['role'])}): {i['shares']:,.0f} sh{price} = "
                     f"{_money(i['value'])}{_stake(i)}")
    if n > MAX_LINES:
        lines.append(f"+{n - MAX_LINES} more insiders")
    if alert["new_links"]:
        links = " ".join(f"[{k}]({u})" for k, u in enumerate(alert["new_links"][:8], 1))
        lines += ["", f"SEC filing{'s' if len(alert['new_links']) > 1 else ''}: {links}"]
    return title, lines


def payload(alert, settings) -> dict:
    title, lines = describe(alert)
    desc = "\n".join(lines)
    if len(desc) > DESC_LIMIT:
        desc = desc[:DESC_LIMIT - 1] + "…"
    return {"username": settings["username"],
            "allowed_mentions": {"parse": []},  # a filer named "@everyone" must not ping anyone
            "embeds": [{"title": title[:256], "url": alert["new_links"][0] if alert["new_links"] else None,
                        "description": desc,
                        "footer": {"text": f"{alert['rule']} · {settings['footer']}"[:2048]},
                        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}]}


def as_text(alert) -> str:
    title, lines = describe(alert)
    plain = re.sub(r"\[(\d+)\]\(([^)]+)\)", r"\2", "\n".join(lines)).replace("**", "")
    return f"=== [{alert['rule']}] {title}\n" + re.sub(r"\\(.)", r"\1", plain)


# ---------------------------------------------------------------- Discord

def post(webhook_url, body, http=None, sleep=time.sleep):
    """POST one message, honouring Discord's rate limits. Errors never contain the URL, which
    carries the webhook's secret token."""
    http = http or requests
    for attempt in range(4):
        try:
            r = http.post(webhook_url, params={"wait": "true"}, json=body, timeout=15)
        except requests.RequestException as exc:
            raise DiscordError(f"could not reach Discord ({type(exc).__name__})") from None
        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 1))
            except ValueError:
                wait = 2.0
            sleep(min(wait, 30))
            continue
        if r.status_code >= 500:
            sleep(2 * (attempt + 1))
            continue
        if r.status_code >= 400:
            hint = {401: "the webhook token is wrong", 404: "the webhook was deleted or the URL is wrong"}
            raise DiscordError(f"Discord refused the post (HTTP {r.status_code}"
                               f"{': ' + hint[r.status_code] if r.status_code in hint else ''})")
        return
    raise DiscordError("Discord kept rate-limiting or failing; the post will be retried next run")


def _notified(conn):
    return lambda rule, fp: conn.execute(
        "SELECT 1 FROM alert_log WHERE rule=? AND fp=?", (rule, fp)).fetchone() is not None


def _last_posted(conn):
    def get(rule, group):
        r = conn.execute("SELECT last_posted FROM alert_groups WHERE rule=? AND grp=?", (rule, group)).fetchone()
        return dt.date.fromisoformat(r[0]) if r else None
    return get


def _record(conn, alert, today, posted: bool):
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    conn.executemany("INSERT OR IGNORE INTO alert_log (rule, fp, posted_at) VALUES (?,?,?)",
                     [(alert["rule"], fp, now) for fp in alert["fps"]])
    if posted:
        conn.execute("INSERT OR REPLACE INTO alert_groups (rule, grp, last_posted) VALUES (?,?,?)",
                     (alert["rule"], alert["group"], today.isoformat()))
    conn.commit()


def run(conn, cfg, today=None, dry_run=False, env=None, http=None, out=print, sleep=time.sleep) -> dict:
    """Evaluate the rules and post (or print) every alert. A trade is only recorded as posted
    after Discord accepted the post, so a failure is retried on the next run."""
    import os
    env = os.environ if env is None else env
    today = today or dt.date.today()
    alerts, skipped = evaluate(conn, cfg, today, _notified(conn), _last_posted(conn))
    for acc, (ticker, reason) in skipped.items():
        log.info("skipped %s filing %s as implausible: %s", ticker, acc, reason)
        if dry_run:
            out(f"--- skipped as implausible: {ticker} {acc}: {reason}\n")
    posted, failed, quiet = 0, [], 0
    for a in alerts:
        if a["suppressed"]:
            quiet += 1
            if dry_run:
                out(f"--- in cooldown, not posted: [{a['rule']}] {a['ticker']}\n")
            else:
                _record(conn, a, today, posted=False)
            continue
        if dry_run:
            out(as_text(a) + "\n")
            continue
        url = env.get(a["webhook_env"], "").strip()
        if not url:
            raise DiscordError(f"{a['webhook_env']} is not set: no webhook to post '{a['rule']}' alerts to")
        try:
            post(url, payload(a, cfg["settings"]), http=http, sleep=sleep)
        except DiscordError as exc:
            failed.append(f"{a['ticker']} ({a['rule']}): {exc}")
            log.warning("posting %s failed: %s", a["ticker"], exc)
            continue
        _record(conn, a, today, posted=True)
        posted += 1
        sleep(1)  # Discord allows 5 posts per 2 s per webhook; stay well inside it
    return {"alerts": len(alerts) - quiet, "posted": posted, "failed": failed,
            "skipped_filings": len(skipped), "cooldown": quiet}


def notify_error(cfg, message, env=None, http=None):
    """Tell the channel the nightly run broke, so it doesn't fail silently for weeks."""
    import os
    env = os.environ if env is None else env
    url = (env.get("DISCORD_ADMIN_WEBHOOK_URL") or env.get(cfg["settings"]["webhook_env"]) or "").strip()
    if not url:
        return
    try:
        post(url, {"username": cfg["settings"]["username"], "allowed_mentions": {"parse": []},
                   "content": f"⚠️ insider-tracker: {md(message)[:1800]}"}, http=http)
    except DiscordError as exc:
        log.warning("could not report the error to Discord either: %s", exc)


def stale_feed(conn, today=None, max_weekdays=STALE_WEEKDAYS):
    """A warning if no Form 4 has been stored for more than `max_weekdays` weekdays, else None.

    The ingest treats a 403/404 daily index as "holiday, or not published yet" -- but 403 is also
    how the SEC turns away a client it has blocked, so one quiet night proves nothing. Several do:
    filings arrive by the hundreds every business day, and holidays come one or two at a time."""
    today = today or dt.date.today()
    latest = conn.execute("SELECT MAX(filed_date) FROM insider_tx").fetchone()[0]
    if not latest:
        return "the database has no Form 4 filings at all; seed it first (DEPLOY.md, step 5)"
    latest = dt.date.fromisoformat(latest)
    quiet = sum(1 for n in range(1, (today - latest).days + 1)
                if (latest + dt.timedelta(days=n)).weekday() < 5)
    if quiet <= max_weekdays:
        return None
    return (f"no new Form 4 filings since {latest} ({quiet} weekdays). The SEC may be refusing this "
            "server: check SEC_USER_AGENT is a real name and email, and the log for 403 errors.")


def backtest(conn, cfg, days: int, today=None) -> list:
    """Replay the rules day by day over the last `days` days, as if they had run each weekday
    night, without posting or recording anything. Returns [(date, alert), ...]."""
    today = today or dt.date.today()
    seen, last = set(), {}
    out = []
    for n in range(days, -1, -1):
        d = today - dt.timedelta(days=n)
        if d.weekday() >= 5:
            continue
        alerts, _ = evaluate(conn, cfg, d, lambda rule, fp: (rule, fp) in seen,
                             lambda rule, group: last.get((rule, group)))
        for a in alerts:
            seen.update((a["rule"], fp) for fp in a["fps"])
            if not a["suppressed"]:
                last[(a["rule"], a["group"])] = d
            out.append((d, a))
    return out
