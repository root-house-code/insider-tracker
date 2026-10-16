"""Command-line entry point: python -m insider_tracker <command>."""
import argparse
import datetime as dt
import logging
import os
import sys

from . import alerts, config, db, edgar_13f, edgar_form4, house, report, senate, ui
from .edgar_common import tickers_to_ciks
from .net import PoliteSession


def _date(s):
    return dt.date.fromisoformat(s)


def _sec():
    return PoliteSession(config.sec_user_agent(), config.SEC_MIN_INTERVAL)


def _congress():
    return PoliteSession(config.sec_user_agent(), config.CONGRESS_MIN_INTERVAL)


def cmd_form4(args, conn):
    http = _sec()
    end = args.end or dt.date.today()
    start = args.start or end - dt.timedelta(days=args.days)
    tickers = args.tickers.split(",") if args.tickers else config.load_watchlist().get("tickers", [])
    ciks = set(tickers_to_ciks(http, tickers).values()) if tickers and not args.all else None
    n = edgar_form4.ingest(conn, http, start, end, ciks)
    print(f"Form 4: stored {n} transactions ({start} to {end}, "
          f"{'watchlist of ' + str(len(ciks)) if ciks else 'all issuers'})")


def cmd_house(args, conn):
    n = house.ingest(conn, _congress(), args.year, since=args.since)
    print(f"House: stored {n} transactions for {args.year}")


def cmd_senate(args, conn):
    since = args.since or dt.date.today() - dt.timedelta(days=args.days)
    n = senate.ingest(conn, _congress(), since)
    print(f"Senate: stored {n} transactions since {since}")


def cmd_13f(args, conn):
    managers = ({c.strip(): "" for c in args.managers.split(",")} if args.managers
                else config.load_watchlist().get("managers_13f", {}))
    if not managers:
        sys.exit("No 13F managers given. Use --managers CIK,CIK or add managers_13f to watchlist.json")
    n = edgar_13f.ingest(conn, _sec(), managers, quarters=args.quarters)
    print(f"13F: stored {n} positions for {len(managers)} managers")


def cmd_update(args, conn):
    """Incremental daily run: everything, small lookback windows."""
    today = dt.date.today()
    for label, fn in (
        ("form4", lambda: cmd_form4(argparse.Namespace(
            start=None, end=None, days=args.days, tickers=None, all=args.all), conn)),
        ("house", lambda: cmd_house(argparse.Namespace(year=today.year, since=None), conn)),
        ("senate", lambda: cmd_senate(argparse.Namespace(since=None, days=max(args.days, 14)), conn)),
        ("13f", lambda: cmd_13f(argparse.Namespace(managers=None, quarters=2), conn)),
    ):
        try:
            fn()
        except SystemExit as exc:
            print(f"{label}: skipped ({exc})")
        except Exception as exc:  # one broken source shouldn't kill the run
            logging.exception("%s failed: %s", label, exc)
    cmd_report(argparse.Namespace(days=30, out=None), conn)


def cmd_ui(args, conn):
    """Interactive browser UI. Opens its own connections, so close ours first."""
    conn.close()
    ui.serve(args.db, host=args.host, port=args.port, open_browser=not args.no_browser)


def cmd_alerts(args, conn):
    try:
        cfg = alerts.load_config(args.config or os.environ.get("INSIDER_ALERTS", "alerts.toml"))
    except alerts.ConfigError as exc:
        sys.exit(f"alert rules: {exc}")

    if args.backtest:
        hits = alerts.backtest(conn, cfg, args.backtest)
        weeks = max(args.backtest / 7, 1)
        print(f"Backtest over the last {args.backtest} days (weekday nights), nothing posted:\n")
        for rule in cfg["rules"]:
            mine = [a for _, a in hits if a["rule"] == rule["name"] and not a["suppressed"]]
            quiet = sum(1 for _, a in hits if a["rule"] == rule["name"] and a["suppressed"])
            print(f"  {rule['name']}: {len(mine)} post(s), about {len(mine) / weeks:.1f} a week"
                  + (f" ({quiet} more held back by cooldown_days)" if quiet else ""))
        print()
        for day, a in hits:
            if a["suppressed"]:
                continue
            n = len(a["insiders"])
            print(f"  {day}  {a['rule']:<28} {a['ticker']:<8} {n} insider{'s' if n != 1 else ' '} "
                  f"${a['total']:>14,.0f}{'  (update)' if a['follow_up'] else ''}")
        oldest = conn.execute("SELECT MIN(filed_date) FROM insider_tx").fetchone()[0]
        print(f"\n(The database holds filings from {oldest}; earlier days can't show anything.)")
        return

    if args.ingest_days:
        try:
            http = _sec()
            end = dt.date.today()
            n = edgar_form4.ingest(conn, http, end - dt.timedelta(days=args.ingest_days), end, None)
            print(f"Form 4: stored {n} transactions")
            problem = alerts.stale_feed(conn)
        except (Exception, SystemExit) as exc:  # still evaluate what we have, but say so
            problem = f"Form 4 ingest failed: {exc}"
        if problem:
            print(problem, file=sys.stderr)
            if not args.dry_run:
                alerts.notify_error(cfg, problem)

    try:
        result = alerts.run(conn, cfg, dry_run=args.dry_run)
    except alerts.DiscordError as exc:
        sys.exit(f"alerts: {exc}")
    verb = "would post" if args.dry_run else "posted"
    print(f"Alerts: {result['alerts']} matched, {verb} "
          f"{result['alerts'] if args.dry_run else result['posted']}"
          + (f", {result['cooldown']} held back by cooldown" if result["cooldown"] else "")
          + (f", {result['skipped_filings']} implausible filing(s) skipped" if result["skipped_filings"] else ""))
    if result["failed"]:
        alerts.notify_error(cfg, f"{len(result['failed'])} alert(s) could not be posted and will retry next run: "
                                 + "; ".join(result["failed"][:3]))
        sys.exit(1)


def cmd_report(args, conn):
    path = report.build(conn, args.days, args.out or config.REPORT_DIR)
    print(f"Report written to {path}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="insider_tracker", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--db", help="SQLite path (default data/insider_tracker.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("form4", help="Ingest SEC Form 4 insider filings")
    s.add_argument("--start", type=_date)
    s.add_argument("--end", type=_date)
    s.add_argument("--days", type=int, default=3, help="lookback if --start not given")
    s.add_argument("--tickers", help="comma-separated watchlist (overrides watchlist.json)")
    s.add_argument("--all", action="store_true", help="ignore watchlist, ingest every issuer")
    s.set_defaults(fn=cmd_form4)

    s = sub.add_parser("house", help="Ingest House PTRs for a year")
    s.add_argument("--year", type=int, default=dt.date.today().year)
    s.add_argument("--since", help="only filings on/after YYYY-MM-DD")
    s.set_defaults(fn=cmd_house)

    s = sub.add_parser("senate", help="Ingest Senate PTRs")
    s.add_argument("--since", type=_date)
    s.add_argument("--days", type=int, default=30)
    s.set_defaults(fn=cmd_senate)

    s = sub.add_parser("13f", help="Ingest 13F holdings for managers")
    s.add_argument("--managers", help="comma-separated CIKs (overrides watchlist.json)")
    s.add_argument("--quarters", type=int, default=4)
    s.set_defaults(fn=cmd_13f)

    s = sub.add_parser("update", help="Daily incremental run of all sources + report")
    s.add_argument("--days", type=int, default=3)
    s.add_argument("--all", action="store_true", help="Form 4 for all issuers, not just watchlist")
    s.set_defaults(fn=cmd_update)

    s = sub.add_parser("ui", help="Explore the database in a browser (localhost only)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1", help="loopback addresses only")
    s.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    s.set_defaults(fn=cmd_ui)

    s = sub.add_parser("alerts", help="Post Form 4 buys that pass the rules in alerts.toml to Discord")
    s.add_argument("--config", help="rules file (default $INSIDER_ALERTS or alerts.toml)")
    s.add_argument("--ingest-days", type=int, default=0,
                   help="first pull Form 4 filings for every issuer over the last N days")
    s.add_argument("--dry-run", action="store_true", help="print what would be posted; post and record nothing")
    s.add_argument("--backtest", type=int, metavar="DAYS",
                   help="replay the rules over the last DAYS days; post and record nothing")
    s.set_defaults(fn=cmd_alerts)

    s = sub.add_parser("report", help="Build the Markdown/CSV report")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--out", type=lambda x: __import__("pathlib").Path(x))
    s.set_defaults(fn=cmd_report)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    conn = db.connect(args.db)
    try:
        args.fn(args, conn)
    finally:
        conn.close()
