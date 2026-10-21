"""Local web UI for exploring the database interactively.

Server-rendered HTML, no JavaScript framework, no new dependencies. Every
view's state lives in the query string, so any screen can be bookmarked or
pasted to someone else and it reproduces exactly.

Three layers, matching how you actually look at this data:
  /signals  the same queries report.py renders, but with their thresholds
            exposed as form controls instead of frozen at config defaults
  /raw      the underlying tables, with per-column filters and sorting
  /sql      an escape hatch for anything the first two can't express

Every tab, control and column heading carries an explanation in the GLOSSARY
section below. It renders as a `title` attribute, so it still works with
JavaScript disabled; TOOLTIP_JS upgrades it to a styled instant tooltip.
Keep new columns documented there -- an undocumented heading is a bug.

The SQL console is why this binds to localhost only. It is read-only several
ways over (PRAGMA query_only, a SELECT-only check, one statement, a timeout,
a forced LIMIT), but a database console on a routable port is still a bad
idea, so serve() refuses to bind anywhere but a loopback address.
"""
import csv
import datetime as dt
import html
import io
import os
import re
import sqlite3
import time
import urllib.parse
import webbrowser
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import chart, config, db, jobs, prices, profiles, signals
from .edgar_common import tickers_to_ciks
from .net import PoliteSession

ON = ' class="on"'  # the active tab's attribute; a constant so f-strings stay Python 3.11-safe
PARAM_PREFIX = "p_"
PAGE_SIZE = 100
MAX_SQL_ROWS = 1000
SQL_TIMEOUT_S = 5.0
LOOPBACK = {"127.0.0.1", "::1", "localhost"}

Param = namedtuple("Param", "name label default cast tip")
View = namedtuple("View", "key title fn params cols takes_since note tip date_filters", defaults=((),))


def _v(key, title, fn, params, cols, note="", tip="", takes_since=True, date_filters=()):
    return View(key, title, fn, params, cols, takes_since, note, tip, tuple(date_filters))


# ============================ GLOSSARY ============================
# Everything the UI explains on hover. One place, so the vocabulary stays
# consistent between a tab, its controls and its columns.

NAV_TABS = [("signals", "Signals"), ("raw", "Raw tables"), ("sql", "SQL"),
            ("watchlist", "Watchlist"), ("jobs", "Jobs"),
            ("status", "Status"), ("architecture", "Architecture")]

# Every other tab in this app only reads the database -- every connection they
# open runs PRAGMA query_only, and the SQL console rejects anything but SELECT.
# Watchlist and Jobs are the two deliberate exceptions: Watchlist writes a
# config file (never the database), and Jobs starts a background thread that
# calls the same ingest() functions the CLI does. Nothing else here writes
# anything, and both of these say so on the page.
NAV_HELP = {
    "signals": "Curated queries that turn raw filings into something worth reading, ordered "
               "roughly by how much the signal is worth. Start here. Each view's thresholds "
               "are editable, so you can see how sensitive a result is to where you draw the line.",
    "raw": "The four database tables exactly as stored, with per-column filters and sorting. "
           "Use this when a signal looks wrong and you want to see the underlying rows, or "
           "when you want to know what is actually in the database.",
    "sql": "Read-only SQL console for questions the other tabs can't express. Results are capped "
           "at 1,000 rows and 5 seconds. Nothing here can modify the database.",
    "watchlist": "Which tickers Form 4 filters to, and which institutional managers 13F tracks, "
                "when you don't run them unfiltered. Saved to watchlist.json. The only tab that "
                "writes anything other than the database.",
    "jobs": "Start an ingest without leaving the browser. Runs in the background, same as the "
           "CLI would, so a long Form 4 backfill doesn't block this page -- refresh or wait for "
           "the auto-refresh to see it finish.",
    "status": "How much data you have, how far back it goes, and whether the last ingest "
              "succeeded. Check here first when a view is unexpectedly empty.",
    "architecture": "How the pieces fit together: where each source's data comes from, what "
                    "shape it arrives in, and which module is responsible for each step from "
                    "the network to this screen.",
}

TABLE_HELP = {
    "insider_tx": "SEC Form 4 filings: one row per transaction line. Corporate insiders "
                  "(officers, directors, 10% owners) must file within two business days of "
                  "trading, so this is the freshest and most reliable source here.",
    "congress_tx": "House and Senate Periodic Transaction Reports under the STOCK Act. One row "
                   "per disclosed trade. Amounts are ranges, never exact, and filings can lag "
                   "the trade by 45 days or more.",
    "holdings_13f": "Quarterly 13F holdings for institutional managers: a snapshot of what each "
                    "manager held at quarter end. Filed up to 45 days after the quarter closes, "
                    "so it is always stale -- useful for direction, not for timing.",
    "processed": "Ingest bookkeeping: one row per document seen, with the outcome. This is what "
                 "makes runs incremental, and what to check when data seems to be missing.",
}

CONTROL_HELP = {
    "window": "How far back to look, counted from today and applied to the trade date (not the "
              "filing date). Widen it when a view looks empty; narrow it to see only what is "
              "recent. Congressional data often needs 90+ days because of filing lag.",
    "date_range": "Keep only rows whose {col} falls between these dates, inclusive; either end "
                  "may be left blank. This filters the finished list, separately from the window, "
                  "which decides which purchases count at all -- so the first buy shown is the "
                  "earliest purchase inside the window. Widen the window to see clusters that began "
                  "earlier.",
    "copy_png": "Copy the table on this page to the clipboard as a PNG, ready to paste into a "
                "chat, email or document. It is drawn as you see it -- your sort, filters and "
                "theme -- minus the filter row. Pages with two tables copy both, stacked.",
    "apply": "Re-run the query with these settings. The result is reflected in the URL, so you "
             "can bookmark it or send it to someone.",
    "csv": "Download exactly these rows as CSV, with the current thresholds applied.",
    "clear": "Remove all column filters and sorting from this table.",
    "run": "Execute the query. Read-only: SELECT and WITH only, one statement, "
           "1,000 rows and 5 seconds maximum.",
    "filters": "Type in a column box to filter. Plain text matches anywhere in the value "
               "(case-insensitive). Use >, <, >=, <=, = or != for comparisons, e.g. >1000000 "
               "or =P. Type !null to hide empty cells, or null to show only them. Click any "
               "column heading to sort by it.",
    "cols": "These tables are wide -- insider_tx has 26 columns -- so only the ones worth "
            "reading are shown by default. Switch to all columns when you need a field that "
            "isn't listed. Filters you set on a hidden column still apply.",
    "sql_box": "Any read-only SELECT or WITH query against the four tables. Try the examples "
               "below to get started. Table and column names are the ones shown on the Raw "
               "tables tab -- hover a heading there for what each column means.",
}

COLUMN_HELP = {
    # --- provenance -------------------------------------------------------
    "accession": "SEC accession number: the unique ID of one filing. Every row from the same "
                 "filing shares it.",
    "doc_id": "The source document's own ID -- the disclosure ID on the House Clerk or Senate "
              "EFD site, or the SEC accession number.",
    "seq": "Row number within a single filing. One Form 4 often reports several transactions; "
           "seq keeps them ordered and uniquely addressable.",
    "form_type": "4 is an original filing, 4/A an amendment restating an earlier one. Signal "
                 "queries dedupe on the economic fields, so a restatement that doesn't change "
                 "the numbers won't double-count.",
    "url": "Link to the original document on sec.gov, or the disclosure PDF for Congress. "
           "Worth opening before acting on anything -- footnotes carry context this table can't.",
    "source": "Which ingest produced the row: form4, house, senate or 13f.",
    "processed_at": "When this document was last processed.",
    "kind": "What happened: an insider open-market buy, or a congressional purchase or sale.",
    "person": "The insider, or the member of Congress.",
    "detail": "For an insider buy: shares, price paid and total dollars. For Congress: the "
              "disclosed amount range -- Congress reports bands, never exact amounts.",
    "status": "ok = stored. no_transactions = a valid filing with nothing to record (holdings "
              "only). needs_manual = a scanned or paper filing no parser can read. "
              "error = failed, and will be retried automatically on the next run.",
    "note": "Error detail, when status is error.",

    # --- dates ------------------------------------------------------------
    "filed_date": "When the filing was submitted. Not when the trade happened -- see trans_date.",
    "trans_date": "When the trade actually happened. This is the date the signals use. Filings "
                  "lag it by about two days for insiders and up to 45+ days for Congress.",
    "first_buy": "Earliest trade date in this cluster.",
    "last_buy": "Latest trade date in this cluster. A tight first/last spread means the insiders "
                "bought at essentially the same moment, which is a stronger signal than "
                "purchases scattered across weeks.",
    "period": "The quarter-end this 13F position is reported as of.",
    "prior": "The earlier quarter being compared against.",
    "lag_days": "Days between the trade and the filing. The STOCK Act deadline is 45 days; "
                "anything above that is a late filing and is listed as such.",

    # --- issuer and security ---------------------------------------------
    "issuer_cik": "The SEC's internal ID for the company whose stock was traded.",
    "issuer_name": "The company whose stock was traded.",
    "issuer": "The company named in this 13F position.",
    "ticker": "Stock symbol. Blank when the filing reported none -- private companies, funds and "
              "some foreign issuers. Signal queries skip blank tickers, so those trades are in "
              "the raw table but not in the signals. On 13F rows it is looked up from the CUSIP, "
              "and blank means no US-listed ticker exists (bonds, foreign lines) or the lookup "
              "hasn't run yet.",
    "security": "What was traded, as the filing words it: 'Common Stock', 'Class A Common Stock', "
                "'Employee Stock Option', and so on.",
    "asset": "The asset as described on the congressional disclosure. Free text, often without a "
             "clean ticker.",
    "asset_type": "The congressional asset category: stock, option, municipal security, and so on.",
    "cusip": "Nine-character security identifier. It is all a 13F filing gives for a holding; "
             "the ticker column is looked up from it (OpenFIGI) and cached.",
    "title_class": "Share class as reported in the 13F, usually COM.",
    "put_call": "Put or Call when the 13F position is an option; blank for ordinary shares.",

    # --- people -----------------------------------------------------------
    "owner_cik": "The SEC's internal ID for the reporting insider.",
    "owner_name": "The insider who filed. A filing with several reporting owners stores them as "
                  "one semicolon-joined string, so the same person can appear both alone and "
                  "inside a joined string.",
    "relationship": "The insider's role: Director, an officer title, 10% Owner, or a combination.",
    "is_director": "1 if the reporting owner is a director.",
    "is_officer": "1 if the reporting owner is an officer. Large buys requires officer or "
                  "director, which is why purchases by 10% owners don't appear in that view.",
    "is_ten_pct": "1 if the reporting owner holds 10% or more of the company. Usually a fund "
                  "rather than an individual.",
    "roles": "Who the buyers are, most senior first: CEO, President, CFO, COO, Chair, then Officer "
             "(any other officer title), Director, 10% owner. Read from each filer's Form 4 title, "
             "which is free text, so an unusual spelling lands under Officer rather than being lost. "
             "Filter to CEO or CFO to see the clusters the top executives joined.",
    "who": "The insiders making up this cluster. Watch for one person appearing twice -- once "
           "alone and once inside a semicolon-joined multi-owner string -- which inflates the "
           "insider count.",
    "member": "The member of Congress who filed the disclosure.",
    "member_names": "The members who traded this ticker in the window.",
    "chamber": "House or Senate.",
    "state_district": "The member's state and district.",
    "owner": "Who holds the congressional asset: Self, Spouse, Joint, or a dependent child.",
    "manager": "The institutional manager that filed the 13F.",
    "manager_name": "The institutional manager that filed the 13F.",
    "manager_cik": "The SEC's internal ID for the institutional manager.",

    # --- transaction mechanics -------------------------------------------
    "code": "Form 4 transaction code. P = open-market purchase and S = open-market sale are the "
            "only two the signals use, because they are the only ones where the insider chose "
            "the price. A = grant, M = option exercise, F = shares withheld for tax, G = gift, "
            "C = conversion -- those are compensation mechanics, not conviction.",
    "tx_type": "Congressional transaction type: P = purchase, S = sale, E = exchange.",
    "acq_disp": "A = securities acquired, D = disposed. Redundant with code for P and S, "
                "informative for the rest.",
    "is_derivative": "1 for options, warrants and convertibles; 0 for the stock itself. Signals "
                     "use non-derivative rows only, because a derivative's price is a strike "
                     "price, not what the market paid.",
    "plan_10b5_1": "1 if the trade was pre-scheduled under a Rule 10b5-1 plan. Plan trades are "
                   "set up months ahead, so they say nothing about what the insider believes "
                   "today. Every signal query excludes them.",
    "direct_indirect": "D = held directly. I = held indirectly, through a trust, fund, LLC or "
                       "family member.",
    "parse_quality": "ok = the filing stated the ticker outright. name_ticker = the ticker "
                     "column was empty and the symbol was recovered from the asset name, "
                     "which is common for Senate ADR listings. no_ticker = there is no symbol "
                     "to be had: Treasuries, municipal bonds, private companies. Check the "
                     "no_ticker rows before trusting any ticker-level total.",
    "share_type": "SH for a share count, PRN for the principal amount of a bond.",

    # --- amounts ----------------------------------------------------------
    "shares": "Number of shares in the transaction.",
    "price": "Price per share as reported. Blank when the filing put the price in a footnote "
             "instead of a value field -- common for option exercises and weighted-average fills.",
    "value": "shares x price, in dollars. Blank whenever price is blank, so a blank here means "
             "unknown, not zero.",
    "total_value": "Combined dollar value of this cluster's purchases.",
    "shares_after": "Shares the owner held immediately after the transaction. Compare against "
                    "shares to see how large a bite the trade took out of their position.",
    "amount_text": "The disclosure range exactly as Congress reported it. Members disclose bands, "
                   "never exact amounts.",
    "amount_low": "Bottom of the disclosed range. Totals sum this, so read every congressional "
                  "dollar figure as a floor rather than an estimate.",
    "amount_high": "Top of the disclosed range. Blank for open-ended bands like 'Over $50,000,000'.",
    "min_total_usd": "Sum of the lower bounds of every disclosed range -- purchases, sales and "
                     "exchanges -- so it can exceed bought + sold. A floor, not an estimate.",
    "min_bought_usd": "Sum of the lower bounds of every disclosed purchase. A floor: a "
                      "'$1,001 - $15,000' purchase counts as $1,001.",
    "min_sold_usd": "Sum of the lower bounds of every disclosed sale. A floor, like "
                    "min_bought_usd, so the two are comparable with each other.",
    "bought_sold_ratio": "min_bought_usd divided by min_sold_usd. Above 1 means more dollars "
                         "went into the stock than came out; below 1, the reverse. Blank means "
                         "nothing was sold, so there is no finite ratio -- check buys and "
                         "min_bought_usd for those. Built from range floors, so treat it as "
                         "directional, not precise.",
    "value_usd": "Reported dollar value of the 13F position, normalised to dollars (filings "
                 "before 2023-01-03 reported thousands).",
    "value_now": "Dollar value of the position in the current quarter.",
    "shares_now": "Shares held in the current quarter.",
    "shares_prior": "Shares held in the previous quarter.",
    "pct_change": "Change in share count against the prior quarter. Blank for NEW and EXIT rows, "
                  "where a percentage would be meaningless.",
    "change": "NEW = position opened this quarter. EXIT = closed entirely. ADD / TRIM = moved by "
              "more than the threshold.",

    # --- counts -----------------------------------------------------------
    "insiders": "How many distinct insiders bought. This is the whole point of a cluster: one "
                "buyer is noise, several buying at once is a pattern.",
    "members": "How many distinct members of Congress traded this ticker.",
    "buys": "Number of purchase transactions.",
    "sells": "Number of sale transactions.",
    "n": "Number of rows.",
}

VIEWS = [
    _v("cluster_buys", "Insider cluster buys", signals.cluster_buys,
       [Param("min_insiders", "min insiders", 2, int,
              "How many different insiders must buy before a company shows up. Two is the "
              "usual bar. Raise it to 3 or more to cut the list down to the strongest cases.")],
       ["ticker", "issuer_name", "insiders", "roles", "total_value", "first_buy", "last_buy", "who"],
       "Two or more insiders at one company buying on the open market (code P), "
       "outside 10b5-1 plans. The strongest signal here.",
       "Several insiders at the same company buying on the open market at the same time. The "
       "strongest signal in this tool: insiders sell for many reasons but buy for essentially "
       "one. Start here.",
       date_filters=("first_buy", "last_buy")),
    _v("large_buys", "Large officer / director purchases", signals.large_buys,
       [Param("min_value", "min $", config.LARGE_BUY_USD, float,
              "Smallest purchase to include, in dollars. Lower it to see more; raise it to keep "
              "only purchases big enough to matter to the buyer personally."),
        Param("limit", "rows", 30, int, "Maximum rows to return, largest first.")],
       ["trans_date", "ticker", "issuer_name", "owner_name", "relationship",
        "shares", "price", "value", "shares_after", "url"],
       "Open-market buys by an officer or director above the dollar threshold.",
       "Single large open-market purchases by an officer or director. Use it when no cluster has "
       "formed but one executive has put real money in. Compare value against shares_after to "
       "judge whether it was a meaningful commitment or a rounding error for them."),
    _v("exec_sales", "Executive sales outside 10b5-1 plans", signals.exec_sales,
       [Param("min_value", "min $", config.EXEC_SALE_USD, float,
              "Smallest sale to include, in dollars."),
        Param("limit", "rows", 30, int, "Maximum rows to return, largest first.")],
       ["trans_date", "ticker", "issuer_name", "owner_name", "relationship",
        "shares", "price", "value", "shares_after", "url"],
       "Weak signal: people sell for many reasons that have nothing to do with the business.",
       "Large discretionary sales -- not pre-scheduled plan trades. A deliberately weak signal: "
       "people sell to buy houses, pay tax and diversify. Worth a look only when a sale is huge "
       "relative to what the person still holds."),
    _v("overlap", "Congress / insider overlap", signals.overlap, [],
       ["ticker", "members", "member_names", "insiders"],
       "Tickers bought by both members of Congress and company insiders in the window.",
       "Tickers bought by both members of Congress and the company's own insiders in the same "
       "window -- two independent groups arriving at the same name. Needs congressional data "
       "ingested, and a wide window, because disclosures lag badly."),
    _v("congress_most_traded", "Most-traded tickers in Congress", signals.congress_most_traded,
       [Param("limit", "rows", 20, int, "Maximum rows to return.")],
       ["ticker", "members", "buys", "sells", "min_bought_usd", "min_sold_usd",
        "bought_sold_ratio", "min_total_usd"],
       "Amounts are the sum of disclosure-range lower bounds, so read them as a floor. "
       "The ratio is bought dollars over sold dollars; blank means nothing was sold.",
       "Which tickers the most members of Congress traded. Read the buys and sells columns "
       "together: heavy activity in both directions is churn, not a view."),
    _v("congress_recent", "Recent congressional trades", signals.congress_recent,
       [Param("limit", "rows", 100, int, "Maximum rows to return, newest filings first.")],
       ["filed_date", "trans_date", "chamber", "member", "owner", "ticker",
        "asset", "tx_type", "amount_text", "lag_days", "url"],
       tip="Every congressional trade disclosed in the window, newest filing first. The browse "
           "view for Congress -- use it to see what was just disclosed rather than to rank."),
    _v("congress_late", "Late congressional filings", signals.congress_late,
       [Param("days", "over N days", config.LATE_FILING_DAYS, int,
              "Report trades disclosed more than this many days after they happened. The STOCK "
              "Act deadline is 45. Lower it to see who files slowly even while technically "
              "compliant.")],
       ["member", "chamber", "ticker", "asset", "trans_date", "filed_date",
        "lag_days", "amount_text", "url"],
       "The STOCK Act deadline is 45 days from the transaction.",
       "Trades disclosed later than the STOCK Act's 45-day deadline. Interesting as a compliance "
       "signal, and because a very late disclosure means the market learned about it very late."),
    _v("thirteenf_changes", "13F position changes", signals.thirteenf_changes,
       [Param("min_change_pct", "min % change", 25.0, float,
              "How much a position must move quarter over quarter to be listed. NEW and EXIT "
              "always appear regardless of this.")],
       ["manager", "period", "prior", "change", "issuer", "ticker", "cusip", "put_call",
        "shares_prior", "shares_now", "pct_change", "value_now"],
       "Latest quarter against the prior one. Up to ~4.5 months stale by the time it files.",
       "What your tracked institutional managers opened, closed or materially resized last "
       "quarter. Ignores the global window: 13F is quarterly, and the comparison is always "
       "latest quarter against the one before.",
       takes_since=False),
]
VIEWS_BY_KEY = {v.key: v for v in VIEWS}
DIAGRAM = """      SEC EDGAR              House Clerk              Senate eFD
     Form 4, 13F            yearly PTR ZIP         CSRF search form
   (XML per filing)        (PDF per filing)         (HTML tables)
           |                       |                       |
------------------------- net.PoliteSession --------------------------
             rate limit, retry/backoff, UTF-8 decode fix
                                  |
 edgar_form4.py     edgar_13f.py        house.py         senate.py
parse Form 4 XML   parse 13F XML    pdfplumber text    BeautifulSoup
        |                 |                 |                 |
------------------------------- db.py --------------------------------
               one SQLite file: data/insider_tracker.db
         insider_tx | congress_tx | holdings_13f | processed
                |                      |                    |
           signals.py             ad-hoc SQL            alerts.py
     curated, ranked queries   raw table access   rules in alerts.toml
        |               |              |                    |
    report.py         ui.py          ui.py               Discord
 Markdown + CSV    Signals tab  Raw tables, SQL    one post per alert"""


MODULES = [
    ("cli.py", "Entry point: python -m insider_tracker <command>. Wires each source's "
               "ingest() to a subcommand and shares one PoliteSession per site."),
    ("config.py", "Environment variables, rate limits, report thresholds, and loading "
                  "watchlist.json. Everything here is overridable without touching code."),
    ("net.py", "Rate-limited HTTP session with retry/backoff. Also where the UTF-8 "
               "decoding fix lives: EDGAR serves text/plain with no charset, so plain "
               "requests silently mangles anything outside ASCII."),
    ("edgar_common.py", "Shared EDGAR plumbing: archive URLs, stripping XML namespaces "
                        "so .find() paths stay readable, and the ticker-to-CIK lookup."),
    ("edgar_form4.py", "Form 4 (insider transactions). Daily master.idx -> full-submission "
                       ".txt -> ownershipDocument XML -> insider_tx."),
    ("edgar_13f.py", "13F (institutional holdings). data.sec.gov submissions API -> "
                     "index.json -> info table XML -> holdings_13f."),
    ("cusip_map.py", "Maps 13F CUSIPs to tickers via OpenFIGI (the one third-party lookup), "
                    "cached forever in the cusip_ticker table so each CUSIP is asked about once."),
    ("house.py", "House PTRs. Clerk's yearly {year}FD.zip index -> one PDF per filing -> "
                "pdfplumber text -> an anchor-regex parser -> congress_tx."),
    ("senate.py", "Senate PTRs. efdsearch.senate.gov: accept a CSRF-protected agreement, "
                 "then POST a search form -> HTML tables -> congress_tx."),
    ("db.py", "SQLite schema and connection. insert_rows() and the processed table, "
              "which is what makes every source incremental."),
    ("signals.py", "SQL queries that turn raw rows into the findings both report.py and "
                   "this UI show, ordered roughly by how much each signal is worth."),
    ("report.py", "Renders signals.py's output to reports/YYYY-MM-DD/report.md plus one "
                  "CSV per section."),
    ("alerts.py", "Posts Form 4 buys that pass the rules in alerts.toml to a Discord channel, from "
                  "a nightly job on a server (see DEPLOY.md), never from this UI. Remembers every "
                  "trade it has posted, so an amended filing or a re-run doesn't post twice."),
    ("profiles.py", "Company descriptions for the chart page: the business summary from Yahoo "
                   "Finance, or just the SEC's industry category when Yahoo has nothing. Cached "
                   "for 30 days; failures degrade to no description."),
    ("prices.py", "Daily prices for the chart page: fetched from Yahoo Finance the first time a "
                 "ticker's chart is opened, then cached and topped up. Failures (unknown symbol, "
                 "source unreachable) are remembered so a bad ticker isn't re-fetched on every click."),
    ("chart.py", "Draws the price chart as monochrome inline SVG, with insider buys, congressional "
                "trades and cluster windows marked by shape. Pure functions; no I/O."),
    ("jobs.py", "In-memory job runner behind the Jobs tab: starts an ingest on a "
               "background thread, one at a time per source, so triggering it from "
               "a browser doesn't mean a page that hangs for an hour."),
    ("ui.py", "This app. The same signals.py queries with live, adjustable thresholds, "
              "a raw table browser, a read-only SQL console, and the two write paths "
              "above -- Watchlist and Jobs."),
]


RAW_TABLES = ["insider_tx", "congress_tx", "holdings_13f", "processed"]

# Raw tables are wide -- insider_tx alone has 26 columns, which no amount of
# wrapping fits on screen. Show the columns worth reading by default and keep the
# rest one click away. Filters on hidden columns still apply.
ESSENTIAL_COLS = {
    "insider_tx": ["trans_date", "ticker", "issuer_name", "owner_name", "relationship",
                   "code", "shares", "price", "value", "plan_10b5_1", "url"],
    "congress_tx": ["filed_date", "trans_date", "chamber", "member", "owner", "ticker",
                    "asset", "tx_type", "amount_text", "url"],
    "holdings_13f": ["manager_name", "period", "issuer", "cusip", "put_call",
                     "shares", "value_usd"],
    "processed": ["source", "doc_id", "status", "note", "processed_at"],
}

# Form 4 transaction codes, spelled out so the table reads without a legend.
CODES = {
    "P": "buy", "S": "sell", "A": "grant", "M": "exercise", "F": "tax",
    "G": "gift", "C": "convert", "D": "disposed", "J": "other", "X": "exercise",
    "I": "discretionary", "K": "equity swap", "L": "small acq", "U": "tender",
    "W": "will", "Z": "trust",
}
TX_TYPES = {"P": "buy", "S": "sell", "E": "exchange"}
# Columns holding a GROUP_CONCAT list rather than a single value.
LIST_COLS = {"who", "member_names", "roles"}

MONEY_COLS = {"value", "total_value", "min_total_usd", "value_now", "value_usd",
              "amount_low", "amount_high", "min_bought_usd", "min_sold_usd"}
COUNT_COLS = {"shares", "shares_after", "shares_now", "shares_prior", "insiders",
              "members", "buys", "sells", "lag_days", "seq"}
RATIO_COLS = {"bought_sold_ratio"}

CSS = """
:root{--bg:#fff;--fg:#000;--line:#000;--rule:#bbb;--head:#ececec;--muted:#555;--zebra:#f7f7f7}
@media (prefers-color-scheme:dark){
  :root{--bg:#000;--fg:#fff;--line:#fff;--rule:#555;--head:#1b1b1b;--muted:#aaa;--zebra:#0a0a0a}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:13px/1.4 -apple-system,"Segoe UI",system-ui,sans-serif}
a{color:var(--fg)}
a:hover{text-decoration:none}
header{border-bottom:2px solid var(--line);padding:8px 12px;display:flex;
       align-items:baseline;gap:14px;flex-wrap:wrap}
header h1{font-size:14px;margin:0;font-weight:700;letter-spacing:.04em;text-transform:uppercase}
header .meta{color:var(--muted);font-size:12px;margin-left:auto}
nav{display:flex;gap:0;border-bottom:1px solid var(--line);flex-wrap:wrap}
nav a{padding:5px 14px;text-decoration:none;border-right:1px solid var(--rule);
      text-transform:uppercase;font-size:11px;letter-spacing:.06em}
nav a.on{background:var(--fg);color:var(--bg);font-weight:700}
a.tk{text-decoration:underline dotted;text-underline-offset:3px;text-decoration-color:var(--muted)}
a.tk:hover{text-decoration-style:solid}
a.back{text-decoration:none;border:1px solid var(--line);padding:2px 12px;font-size:11px;
       text-transform:uppercase;letter-spacing:.06em;margin-right:4px}
a.back:hover{background:var(--fg);color:var(--bg)}
.about{padding:8px 12px 4px;border-bottom:1px solid var(--rule)}
.about>*{max-width:105ch}
.about p{margin:0 0 6px;line-height:1.5}
.about .facts{color:var(--muted);font-size:12px}
.about .src{color:var(--muted);font-size:11px}
.about details{margin:0 0 6px}
.about summary{cursor:pointer;font-size:12px;color:var(--muted);margin-bottom:4px}
.chartwrap{padding:10px 12px;border-bottom:1px solid var(--rule)}
.chartwrap svg{border:1px solid var(--rule)}
nav .navbtn{margin-left:auto;border:0;border-left:1px solid var(--rule);padding:5px 14px;
            background:var(--bg);color:var(--fg);font:11px -apple-system,"Segoe UI",system-ui,sans-serif;
            text-transform:uppercase;letter-spacing:.06em;min-width:9.5em}
nav .navbtn:hover:not(:disabled){background:var(--fg);color:var(--bg)}
nav .navbtn:disabled{color:var(--muted);cursor:not-allowed}
.tabs{display:flex;flex-wrap:wrap;border-bottom:1px solid var(--rule);background:var(--head)}
.tabs a{padding:4px 10px;text-decoration:none;border-right:1px solid var(--rule);font-size:12px}
.tabs a.on{background:var(--bg);font-weight:700;box-shadow:inset 0 -2px 0 var(--fg)}
.bar{padding:7px 12px;border-bottom:1px solid var(--rule);display:flex;
     gap:14px;align-items:center;flex-wrap:wrap}
.bar label{font-size:12px;color:var(--muted)}
.bar input,.bar select,.bar textarea{font:12px ui-monospace,Consolas,monospace;
     background:var(--bg);color:var(--fg);border:1px solid var(--line);padding:2px 4px}
.bar input[type=number]{width:9em}
button{font:11px -apple-system,"Segoe UI",system-ui,sans-serif;text-transform:uppercase;
       letter-spacing:.06em;background:var(--fg);color:var(--bg);border:1px solid var(--line);
       padding:3px 12px;cursor:pointer}
button.ghost{background:var(--bg);color:var(--fg)}
.note{padding:7px 12px;color:var(--muted);font-size:12px;border-bottom:1px solid var(--rule);max-width:80ch}
.wrap{overflow:auto}
table{border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:12px;max-width:100%}
th,td{border:1px solid var(--rule);padding:3px 7px;text-align:left;white-space:nowrap;
      vertical-align:top}
/* Long free-text columns reflow instead of stretching the table sideways. The width
   cap sits on an inner block because max-width on a table cell is ignored by the
   auto table layout algorithm. */
th.txt,td.txt{white-space:normal}
td.txt>span{display:block;max-width:52ch;min-width:14ch;overflow-wrap:anywhere}
thead th{background:var(--head);position:sticky;top:0;z-index:1;border-color:var(--line);
         font-size:11px;text-transform:uppercase;letter-spacing:.04em}
thead th a{text-decoration:none;display:block}
tbody tr:nth-child(even){background:var(--zebra)}
td.num{text-align:right;font-family:ui-monospace,Consolas,monospace}
td.nil{color:var(--muted)}
.filters input{width:100%;min-width:6em;font:11px ui-monospace,Consolas,monospace;
               background:var(--bg);color:var(--fg);border:1px solid var(--rule);padding:1px 3px}
.filters select{width:100%;min-width:5em;max-width:18em;font:11px ui-monospace,Consolas,monospace;
                background:var(--bg);color:var(--fg);border:1px solid var(--rule);padding:1px 2px}
.filters select.on{border-color:var(--line);font-weight:700;box-shadow:inset 0 0 0 1px var(--line)}
.empty{padding:26px 12px;color:var(--muted)}
.count{padding:6px 12px;font-size:12px;color:var(--muted);
       border-top:1px solid var(--rule);display:flex;gap:12px;flex-wrap:wrap}
.err{padding:10px 12px;border:1px solid var(--line);margin:12px;
     font:12px ui-monospace,Consolas,monospace;white-space:pre-wrap}
textarea{width:100%;height:7em}
dl{margin:0;padding:12px;display:grid;grid-template-columns:max-content 1fr;gap:2px 18px;font-size:12px}
dt{color:var(--muted)}
dd{margin:0;font-family:ui-monospace,Consolas,monospace}
.diagram{margin:0;padding:16px 12px;font:12px/1.4 ui-monospace,Consolas,monospace;
         white-space:pre;overflow-x:auto;border-bottom:1px solid var(--rule)}
/* anything explained on hover gets a quiet affordance */
[title],[data-tip]{cursor:help}
thead th[data-tip],thead th[title],.bar label[data-tip],.bar label[title]{
  text-decoration:underline dotted;text-underline-offset:3px;text-decoration-color:var(--muted)}
thead th[data-tip] a,thead th[title] a{text-decoration:inherit}
#tip{position:fixed;z-index:99;max-width:46ch;padding:7px 10px;border:1px solid var(--line);
     background:var(--bg);color:var(--fg);box-shadow:3px 3px 0 var(--line);
     font:12px/1.5 -apple-system,"Segoe UI",system-ui,sans-serif;
     pointer-events:none;display:none;white-space:normal}
"""

# Native `title` tooltips are slow and unstyled, and a pure-CSS tooltip would be
# clipped by the table's own overflow container. So: markup carries `title` (which
# works fine with JS off), and this promotes it to a position:fixed panel that can
# escape any scroll container.
TOOLTIP_JS = """
(function(){
  var tip=document.createElement('div'); tip.id='tip'; document.body.appendChild(tip);
  document.querySelectorAll('[title]').forEach(function(el){
    el.setAttribute('data-tip', el.getAttribute('title')); el.removeAttribute('title');
  });
  var cur=null;
  function place(x,y){
    var pad=14, w=tip.offsetWidth, h=tip.offsetHeight;
    var l=x+pad, t=y+pad;
    if(l+w>window.innerWidth-8) l=x-w-pad;
    if(t+h>window.innerHeight-8) t=y-h-pad;
    tip.style.left=Math.max(4,l)+'px'; tip.style.top=Math.max(4,t)+'px';
  }
  function show(el,x,y){
    if(el===cur) return;
    cur=el; tip.textContent=el.getAttribute('data-tip');
    tip.style.display='block'; place(x,y);
  }
  function hide(){ cur=null; tip.style.display='none'; }
  document.addEventListener('mouseover', function(e){
    var el=e.target.closest && e.target.closest('[data-tip]');
    if(el) show(el,e.clientX,e.clientY); else hide();
  });
  document.addEventListener('mousemove', function(e){ if(cur) place(e.clientX,e.clientY); });
  document.addEventListener('focusin', function(e){
    var el=e.target.closest && e.target.closest('[data-tip]');
    if(el){ var r=el.getBoundingClientRect(); show(el,r.left,r.bottom); }
  });
  document.addEventListener('focusout', hide);
  document.addEventListener('keydown', function(e){ if(e.key==='Escape') hide(); });
  window.addEventListener('scroll', hide, true);
})();
"""


# Draws the page's table(s) onto a canvas and puts the PNG on the clipboard. Hand-rolled
# rather than html2canvas: that would be a third-party script fetched from a CDN, and this
# app otherwise runs offline.
COPY_PNG_JS = r"""
(function(){
  var btn=document.getElementById('copypng');
  if(!btn||btn.disabled) return;
  var LABEL=btn.textContent, MAXH=16000, MAXPX=32000, PAD=8;
  var styles=new Map();

  function cs(el){var s=styles.get(el); if(!s){s=getComputedStyle(el); styles.set(el,s);} return s;}
  function fontOf(c){return c.fontStyle+' '+c.fontWeight+' '+c.fontSize+' '+c.fontFamily;}
  function themeVar(n){return getComputedStyle(document.documentElement).getPropertyValue(n).trim();}
  function opaque(c){return c&&c!=='transparent'&&!/rgba\(.*,\s*0\)$/.test(c);}

  // Every word of a cell with the rectangle the BROWSER put it in. Drawing at those rectangles
  // reproduces the page's own wrapping exactly, instead of re-deriving it (which drifts).
  function eachWord(cell,fn){
    var walker=document.createTreeWalker(cell,NodeFilter.SHOW_TEXT,null), node;
    while((node=walker.nextNode())){
      var el=node.parentNode;
      if(el.closest('select,input,option')) continue;
      var c=cs(el), re=/\S+/g, m, data=node.nodeValue;
      while((m=re.exec(data))){
        var r=document.createRange();
        r.setStart(node,m.index); r.setEnd(node,m.index+m[0].length);
        var txt=c.textTransform==='uppercase'?m[0].toUpperCase():m[0], rects=r.getClientRects();
        if(rects.length<=1){ fn(txt,rects[0]||r.getBoundingClientRect(),c); continue; }
        for(var i=0;i<m[0].length;i++){  // a word the browser broke across lines: place each letter
          var rr=document.createRange();
          rr.setStart(node,m.index+i); rr.setEnd(node,m.index+i+1);
          fn(txt[i],rr.getBoundingClientRect(),c);
        }
      }
    }
  }

  function tableRows(t){
    return Array.prototype.filter.call(t.querySelectorAll('tr'),function(r){return !r.classList.contains('filters');});
  }

  // The price chart is an inline SVG; as an image it can't see the page's CSS variables, so
  // bake the current theme's colours into a copy and rasterise that.
  function chartImage(){
    var svg=document.querySelector('svg.chart');
    if(!svg) return Promise.resolve(null);
    var r=svg.getBoundingClientRect(), clone=svg.cloneNode(true);
    clone.setAttribute('width',r.width); clone.setAttribute('height',r.height);
    clone.style.width=r.width+'px'; clone.style.height=r.height+'px'; clone.style.maxWidth='none';
    var xml=new XMLSerializer().serializeToString(clone)
      .replace(/var\(--([a-z-]+)\)/g,function(_,n){return themeVar('--'+n);});
    return new Promise(function(res){
      var img=new Image();
      img.onload=function(){res({img:img,w:r.width,h:r.height});};
      img.onerror=function(){res(null);};  // no chart is better than no copy
      img.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(xml);
    });
  }

  // Stack the chart (if any) and the tables, skipping the filter row (dropdowns) and anything past MAXH.
  function plan(tables,chart){
    var top=chart?chart.h+16:0, y=PAD+top, width=chart?chart.w:0, hidden=0, items=[];
    tables.forEach(function(t,ti){
      var tr=t.getBoundingClientRect(), rows=tableRows(t), keep=[], cursor=y;
      for(var i=0;i<rows.length;i++){
        var rect=rows[i].getBoundingClientRect();
        if(cursor+rect.height>MAXH && i>1){ hidden+=rows.length-i; break; }
        keep.push({row:rows[i],rect:rect,y:cursor});
        cursor+=rect.height;
      }
      items.push({tr:tr,rows:keep});
      y=cursor+(ti<tables.length-1?16:0);
      width=Math.max(width,tr.width);
    });
    return {items:items,w:width+2*PAD,h:y+PAD+(hidden?22:0),hidden:hidden,tail:y};
  }

  function line(ctx,color,x1,y1,x2,y2){
    ctx.strokeStyle=color; ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke();
  }

  function render(){
    styles.clear();
    return chartImage().then(function(chart){
    var p=plan(Array.prototype.slice.call(document.querySelectorAll('table')),chart);
    var scale=Math.max(1,Math.min(Math.max(2,Math.ceil(window.devicePixelRatio||1)),MAXPX/p.w,MAXPX/p.h));
    var cv=document.createElement('canvas');
    cv.width=Math.ceil(p.w*scale); cv.height=Math.ceil(p.h*scale);
    var ctx=cv.getContext('2d');
    ctx.scale(scale,scale);
    ctx.fillStyle=themeVar('--bg'); ctx.fillRect(0,0,p.w,p.h);
    ctx.textBaseline='middle';
    if(chart) ctx.drawImage(chart.img,PAD,PAD,chart.w,chart.h);

    p.items.forEach(function(it){
      it.rows.forEach(function(entry,ri){
        var rowBg=cs(entry.row).backgroundColor, cells=entry.row.children;
        Array.prototype.forEach.call(cells,function(cell,ci){
          var c=cs(cell), cr=cell.getBoundingClientRect();
          var x=PAD+(cr.left-it.tr.left), y=entry.y+(cr.top-entry.rect.top), w=cr.width, h=cr.height;
          ctx.fillStyle=opaque(c.backgroundColor)?c.backgroundColor:(opaque(rowBg)?rowBg:themeVar('--bg'));
          ctx.fillRect(x,y,w,h);
          // Collapsed borders sit on the cell boundaries, so draw each shared edge once.
          line(ctx,c.borderLeftColor,x,y,x,y+h);
          if(ri===0) line(ctx,c.borderTopColor,x,y,x+w,y);
          line(ctx,c.borderBottomColor,x,y+h,x+w,y+h);
          if(ci===cells.length-1) line(ctx,c.borderRightColor,x+w,y,x+w,y+h);

          eachWord(cell,function(txt,r,tc){
            ctx.font=fontOf(tc);
            if('letterSpacing' in ctx) ctx.letterSpacing=tc.letterSpacing==='normal'?'0px':tc.letterSpacing;
            ctx.fillStyle=tc.color;
            ctx.fillText(txt,PAD+(r.left-it.tr.left),entry.y+(r.top-entry.rect.top)+r.height/2);
          });
        });
      });
    });
    if(p.hidden){
      ctx.font='italic 12px '+getComputedStyle(document.body).fontFamily; ctx.fillStyle=themeVar('--muted');
      if('letterSpacing' in ctx) ctx.letterSpacing='0px';
      ctx.fillText('… '+p.hidden.toLocaleString()+' more rows not included in this image',PAD,p.tail+PAD+8);
    }
    return cv;
    });
  }

  function say(text,tip,ms){
    btn.textContent=text;
    if(tip) btn.setAttribute('data-tip',tip);
    if(ms) setTimeout(function(){btn.textContent=LABEL;},ms);
  }

  btn.addEventListener('click',function(){
    if(!navigator.clipboard||!window.ClipboardItem){
      say('Not supported','This browser cannot copy images to the clipboard (needs a current Chrome, Edge, Safari or Firefox 127+, on localhost or https).',3500);
      return;
    }
    say('Copying…');
    // The promise form keeps the click's permission alive while the PNG is drawn and encoded
    // (Safari requires it).
    var blob=render().then(function(cv){
      return new Promise(function(res,rej){cv.toBlob(function(b){b?res(b):rej(new Error('image too large to encode'));},'image/png');});
    });
    navigator.clipboard.write([new ClipboardItem({'image/png':blob})]).then(
      function(){say('Copied ✓',null,2000);},
      function(e){say('Copy failed','Could not copy: '+(e&&e.message?e.message:e),3500);});
  });
})();
"""


# ---------- formatting ----------

def _esc(s):
    return html.escape(str(s), quote=True)


def _tip(text):
    """Render help as a title attribute; TOOLTIP_JS upgrades it in the browser."""
    return f' title="{_esc(text)}"' if text else ""


def _cell(col, v):
    """Render one value as (html, css_class)."""
    if v is None or v == "":
        return "", "nil"
    if col == "url":
        return f'<a href="{_esc(v)}" target="_blank" rel="noopener">filing</a>', ""
    if col == "ticker":
        return (f'<a class="tk" href="/chart?{_qs(ticker=v)}" '
                f'title="Open the price chart for {_esc(v)}">{_esc(v)}</a>'), ""
    if col == "code":
        label = CODES.get(str(v), "")
        return _esc(v) + (f' <span class="nil">{_esc(label)}</span>' if label else ""), ""
    if col == "tx_type":
        label = TX_TYPES.get(str(v), "")
        return _esc(v) + (f' <span class="nil">{_esc(label)}</span>' if label else ""), ""
    if col in LIST_COLS:
        # GROUP_CONCAT joins with a bare comma, but names carry their own ", "
        # ("Ault & Company, Inc."). Render separators as a middle dot so the two
        # can't be confused, and so the browser has somewhere to wrap.
        return " &middot; ".join(_esc(p) for p in _split_list(v)), ""
    if col == "pct_change" and isinstance(v, (int, float)):
        return f"{v:+,.1f}%", "num"
    if col in RATIO_COLS and isinstance(v, (int, float)):
        return f"{v:,.2f}", "num"
    if col == "price" and isinstance(v, (int, float)):
        return f"{v:,.2f}", "num"
    if col in MONEY_COLS and isinstance(v, (int, float)):
        return f"{v:,.0f}", "num"
    if col in COUNT_COLS and isinstance(v, (int, float)):
        return f"{v:,.0f}", "num"
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip("."), "num"
    if isinstance(v, int):
        return f"{v:,}", "num"
    return _esc(v), ""


WRAP_AT = 28  # characters; longer than this and a column is worth reflowing


def _sort_key(v):
    """Numbers before text, so a mixed column still orders sensibly."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (0, float(v))
    return (1, str(v).casefold())


def _sort_rows(rows, col, direction):
    """Sort dict rows in memory. Blanks sink to the bottom whichever direction is
    chosen -- an empty cell is missing data, never 'the largest value'."""
    if not col or not rows or col not in rows[0]:
        return rows
    present = [r for r in rows if r.get(col) not in (None, "")]
    blank = [r for r in rows if r.get(col) in (None, "")]
    present.sort(key=lambda r: _sort_key(r.get(col)), reverse=(direction == "desc"))
    return present + blank


def _sorter(path, base, key="sort", dkey="dir"):
    """Build the href factory _table() uses for clickable column headings."""
    def href(col, nxt):
        params = dict(base)
        params[key] = col
        params[dkey] = nxt
        return f"{path}?{_qs(**params)}"
    return href


def _read_sort(qs, cols, key="sort", dkey="dir"):
    col = _one(qs, key)
    col = col if col in cols else None
    return col, ("desc" if _one(qs, dkey) == "desc" else "asc")


def _wrap_cols(rows, cols):
    """Columns holding prose rather than a short token. Decided from the data so
    that SQL results and any column added later get the same treatment."""
    out = set()
    for c in cols:
        if c in MONEY_COLS or c in COUNT_COLS or c in RATIO_COLS or c == "url":
            continue
        for r in rows:
            v = r.get(c)
            if v is not None and len(str(v)) > WRAP_AT:
                out.add(c)
                break
    return out


SIG_FILTER_PREFIX = "sf_"
BLANK = "__blank__"
NOT_BLANK = "__notblank__"
MAX_FILTER_OPTIONS = 200  # beyond this a dropdown is a scroll-hunt, not a filter


LIST_SEP = re.compile(r",(?!\s)")  # GROUP_CONCAT's bare comma, never the ", " inside a name


def _split_list(v):
    return [p.strip() for p in LIST_SEP.split(str(v or "")) if p.strip()]


def _matches(row, col, want):
    v = row.get(col)
    if want == BLANK:
        return v is None or v == ""
    if want == NOT_BLANK:
        return not (v is None or v == "")
    if col in LIST_COLS:  # match one name inside a GROUP_CONCAT list, not the whole string
        return want in _split_list(v)
    return ("" if v is None else str(v)) == want


def _read_signal_filters(qs, cols):
    out = {}
    for c in cols:
        v = _one(qs, SIG_FILTER_PREFIX + c)
        if v:
            out[c] = v
    return out


DATE_FILTER_PREFIX = "df_"


def _read_date_filters(qs, cols):
    """{col: (from, to)} for date-range controls. Bad or blank dates are ignored rather
    than erroring -- a hand-edited URL should degrade, not crash."""
    out = {}
    for c in cols:
        lo, hi = (_one(qs, f"{DATE_FILTER_PREFIX}{c}_{end}") for end in ("from", "to"))
        ok = []
        for v in (lo, hi):
            try:
                ok.append(dt.date.fromisoformat(v).isoformat() if v else None)
            except ValueError:
                ok.append(None)
        if any(ok):
            out[c] = tuple(ok)
    return out


def _apply_date_filters(rows, dfl):
    def keep(r):
        for c, (lo, hi) in dfl.items():
            v = str(r.get(c) or "")[:10]
            if not v or (lo and v < lo) or (hi and v > hi):
                return False
        return True
    return [r for r in rows if keep(r)] if dfl else rows


def _apply_filters(rows, flt, skip=None):
    return [r for r in rows if all(_matches(r, c, w) for c, w in flt.items() if c != skip)]


def _plain(col, v):
    txt, _ = _cell(col, v)
    return html.unescape(re.sub(r"<[^>]+>", "", txt))


def _filter_select(col, rows, current, form_id):
    """Excel-style autofilter: the distinct values of `col` among `rows` (which the
    caller has already narrowed by every OTHER active filter, so options cascade)."""
    if col == "url":
        return ""
    vals, blank = set(), False
    for r in rows:
        v = r.get(col)
        if v is None or v == "":
            blank = True
        elif col in LIST_COLS:
            vals.update(_split_list(v))
        else:
            vals.add(v)
    too_many = len(vals) > MAX_FILTER_OPTIONS
    if too_many and not blank and current not in (BLANK, NOT_BLANK):
        why = (f"{len(vals):,} distinct values -- too many to list. "
               "Sort by this column instead, or filter it on the Raw tables tab.")
        return f'<span class="nil"{_tip(why)}>&hellip;</span>'
    opts, seen = ['<option value="">(all)</option>'], {BLANK, NOT_BLANK}
    # Blank handling first: hiding the empty cells is the most common filter of all,
    # and stays available even when a column has too many values to list.
    if blank or current in (BLANK, NOT_BLANK):
        for key, label in ((NOT_BLANK, "(not blank)"), (BLANK, "(blank)")):
            sel = " selected" if current == key else ""
            opts.append(f'<option value="{key}"{sel}>{label}</option>')
    for v in ([] if too_many else sorted(vals, key=_sort_key)):
        key = v if col in LIST_COLS else str(v)
        label = v if col in LIST_COLS else _plain(col, v)
        seen.add(key)
        sel = " selected" if key == current else ""
        opts.append(f'<option value="{_esc(key)}"{sel}>{_esc(label)}</option>')
    if current and current not in seen:  # keep a stale choice visible
        opts.append(f'<option value="{_esc(current)}" selected>{_esc(current)}</option>')
    cls = ' class="on"' if current else ""
    tip = (f"Show only rows where {col} is the chosen value. (not blank) hides empty cells; "
           f"(blank) shows only them. Options reflect your other filters. This narrows the "
           f"rows the view returned -- a view with a rows limit picks its top N first.")
    if too_many:
        tip += f" {len(vals):,} distinct values is too many to list individually."
    return (f'<select name="{SIG_FILTER_PREFIX}{_esc(col)}" form="{form_id}"{cls} '
            f'onchange="this.form.submit()"{_tip(tip)}>{"".join(opts)}</select>')


def _table(rows, cols, sort_href=None, sort_col=None, sort_dir=None, filters=None,
           filter_cells=None):
    """One <table>. sort_href(col) makes headers clickable; filters adds a text-input
    row (raw tables); filter_cells adds a row of pre-rendered cells (signal dropdowns)."""
    if not rows and not cols:
        return '<p class="empty">No rows. Widen the window, relax the threshold, ' \
               'or ingest the source that feeds this view.</p>'
    out = ['<div class="wrap"><table><thead><tr>']
    wrappable = _wrap_cols(rows, cols)
    for c in cols:
        help_text = COLUMN_HELP.get(c, "")
        cls = ' class="txt"' if c in wrappable else ""
        if sort_href:
            mark = ""
            if c == sort_col:
                mark = " &#9650;" if sort_dir == "asc" else " &#9660;"
            # Numbers are almost always wanted largest-first on the first click;
            # text almost always A-Z. After that it just toggles.
            first = "desc" if (c in MONEY_COLS or c in COUNT_COLS or c in RATIO_COLS) else "asc"
            nxt = ("desc" if sort_dir == "asc" else "asc") if c == sort_col else first
            inner = f'<a href="{_esc(sort_href(c, nxt))}">{_esc(c)}{mark}</a>'
            help_text = (help_text + "  Click to sort." if help_text else "Click to sort.")
        else:
            inner = _esc(c)
        out.append(f"<th{cls}{_tip(help_text)}>{inner}</th>")
    out.append("</tr>")
    if filters is not None:
        out.append('<tr class="filters">')
        for c in cols:
            hint = (f"Filter {c}. Text matches anywhere; or use >, <, >=, <=, =, != for "
                    "comparisons; null for empty, !null for not empty.")
            out.append(f'<td><input name="f_{_esc(c)}" value="{_esc(filters.get(c, ""))}" '
                       f'autocomplete="off"{_tip(hint)}></td>')
        out.append("</tr>")
    if filter_cells is not None:
        out.append('<tr class="filters">')
        out.extend(f"<td>{filter_cells.get(c, '')}</td>" for c in cols)
        out.append("</tr>")
    out.append("</thead><tbody>")
    for r in rows:
        out.append("<tr>")
        for c in cols:
            txt, cls = _cell(c, r.get(c))
            if c in wrappable:
                cls = (cls + " txt").strip()
                txt = f"<span>{txt}</span>"
            out.append(f'<td class="{cls}">{txt}</td>' if cls else f"<td>{txt}</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    if not rows:
        # An empty table still teaches you its columns, and they stay hoverable.
        out.append('<p class="empty">No rows. Widen the window, relax the '
                   'threshold, or ingest the source that feeds this view. The '
                   'headings above still describe what each column means.</p>')
    return "".join(out)


def _page(title, nav_key, body, meta="", extra_head=""):
    nav = []
    for key, label in NAV_TABS:
        on = ' class="on"' if key == nav_key else ""
        nav.append(f'<a href="/{key}"{on}{_tip(NAV_HELP[key])}>{label}</a>')
    has_table = "<table" in body
    nav.append('<button type="button" id="copypng" class="navbtn"'
               f'{"" if has_table else " disabled"}'
               f'{_tip(CONTROL_HELP["copy_png"] if has_table else "There is no table on this page to copy.")}'
               '>Copy PNG</button>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title><style>{CSS}</style>{extra_head}</head><body>
<header><h1{_tip('Local browser over the SEC and congressional filings you have ingested. '
                 'Hover any tab, control or column heading for what it means. Everything here '
                 'is read-only except Watchlist (writes watchlist.json), Jobs (starts an '
                 'ingest) and the price chart (caches prices and the company description on first open) -- each says so.')}>
insider-tracker</h1><span class="meta">{meta}</span></header>
<nav>{''.join(nav)}</nav>
{body}
<script>{TOOLTIP_JS}</script>
<script>{COPY_PNG_JS}</script>
</body></html>"""


# ---------- query-string helpers ----------

def _one(qs, key, default=None, cast=None):
    vals = qs.get(key)
    if not vals or vals[0] == "":
        return default
    if cast is None:
        return vals[0]
    try:
        return cast(vals[0])
    except (TypeError, ValueError):
        return default


def _qparams(kwargs):
    """View params travel under a prefix; congress_late.days would otherwise
    collide with the global window control, and silently at that."""
    return {PARAM_PREFIX + k: v for k, v in kwargs.items()}


def _qs(**kw):
    clean = {k: v for k, v in kw.items() if v not in (None, "")}
    return urllib.parse.urlencode(clean)


# ---------- views ----------

def _connect(path, read_only=False):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    if read_only:
        # Belt and braces: this blocks writes at the engine level regardless of
        # file permissions, and unlike mode=ro it works fine on a WAL database.
        conn.execute("PRAGMA query_only = ON")
    return conn


def _window(qs):
    days = _one(qs, "days", 30, int) or 30
    days = max(1, min(days, 3650))
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    return days, since


def page_signals(conn, qs):
    key = _one(qs, "view", VIEWS[0].key)
    view = VIEWS_BY_KEY.get(key, VIEWS[0])
    days, since = _window(qs)

    kwargs, controls = {}, []
    for p in view.params:
        val = _one(qs, PARAM_PREFIX + p.name, p.default, p.cast)
        kwargs[p.name] = val
        step = "any" if p.cast is float else "1"
        controls.append(
            f'<label{_tip(p.tip)}>{_esc(p.label)} '
            f'<input type="number" step="{step}" name="{_esc(PARAM_PREFIX + p.name)}" '
            f'value="{_esc(val)}"></label>')

    all_rows = view.fn(conn, since, **kwargs) if view.takes_since else view.fn(conn, **kwargs)
    dfl = _read_date_filters(qs, view.date_filters)
    pool = _apply_date_filters(all_rows, dfl)  # date ranges narrow first, so dropdown options follow
    flt = _read_signal_filters(qs, view.cols)
    rows = _apply_filters(pool, flt)
    sort, direction = _read_sort(qs, view.cols)
    rows = _sort_rows(rows, sort, direction)
    date_params = {f"{DATE_FILTER_PREFIX}{c}_{end}": v
                   for c, (lo, hi) in dfl.items() for end, v in (("from", lo), ("to", hi))}
    base = dict(view=view.key, days=days, **_qparams(kwargs), **date_params,
                **{SIG_FILTER_PREFIX + c: v for c, v in flt.items()})
    filter_cells = {c: _filter_select(c, _apply_filters(pool, flt, skip=c), flt.get(c), "sigform")
                    for c in view.cols}
    unfiltered = dict(view=view.key, days=days, sort=sort, dir=direction, **_qparams(kwargs),
                      **date_params)
    clear_link = (f'<a href="/signals?{_qs(**unfiltered)}"'
                  f'{_tip("Remove every column filter; keeps the window, thresholds and sort.")}'
                  f'>clear filters</a>') if flt else ""

    tabs = "".join(
        f'<a href="/signals?{_qs(view=v.key, days=days)}"'
        f'{ON if v.key == view.key else ""}{_tip(v.tip)}>{_esc(v.title)}</a>'
        for v in VIEWS)

    window_ctl = ("" if not view.takes_since else
                  f'<label{_tip(CONTROL_HELP["window"])}>window '
                  f'<input type="number" name="days" value="{days}" min="1"> days</label>')
    date_ctl = "".join(
        f'<label{_tip(CONTROL_HELP["date_range"].format(col=c))}>{_esc(c.replace("_", " "))} '
        + "".join(
            f'{word}<input type="date" name="{DATE_FILTER_PREFIX}{c}_{end}" '
            f'value="{_esc((dfl.get(c) or (None, None))[i] or "")}"> '
            for i, (end, word) in enumerate((("from", "from "), ("to", "to "))))
        + '</label>' for c in view.date_filters)
    csv_href = f"/signals.csv?{_qs(sort=sort, dir=direction, **base)}"
    keep_sort = (f'<input type="hidden" name="sort" value="{_esc(sort)}">'
                 f'<input type="hidden" name="dir" value="{_esc(direction)}">') if sort else ""
    body = f"""<div class="tabs">{tabs}</div>
<form id="sigform" class="bar" method="get" action="/signals">
  <input type="hidden" name="view" value="{_esc(view.key)}">{keep_sort}
  {window_ctl}{''.join(controls)}{date_ctl}
  <button type="submit"{_tip(CONTROL_HELP['apply'])}>Apply</button>
  <a href="{_esc(csv_href)}"><button type="button" class="ghost"
     {_tip(CONTROL_HELP['csv'])}>CSV</button></a>
</form>
{f'<p class="note">{_esc(view.note)}</p>' if view.note else ''}
{_table(rows, view.cols, sort_href=_sorter("/signals", base), sort_col=sort, sort_dir=direction,
        filter_cells=filter_cells)}
<div class="count"><span>{f"{len(rows):,} of {len(all_rows):,} rows" if (flt or dfl) else f"{len(rows):,} rows"}</span>
  {clear_link}
  {'<span' + _tip("Only trades on or after this date are included. Change it with the window "
                  "control above.") + '>since ' + since + '</span>' if view.takes_since else ''}</div>"""
    return _page(view.title, "signals", body, meta=f"{len(rows):,} rows")


RANGES = {"3m": ("3M", 92), "6m": ("6M", 183), "1y": ("1Y", 366), "5y": ("5Y", 1830)}
CHART_TRADE_LIMIT = 200


def _safe_back(url):
    """A URL on this site that the back arrow may point at, else None. Only relative paths
    ('/signals?...'): anything else could turn the arrow into a link to somewhere else, and
    the chart page itself is never a place to go "back" to."""
    if (not url or not url.startswith("/") or url.startswith("//") or "\\" in url
            or "\n" in url or "\r" in url):
        return None
    return None if urllib.parse.urlparse(url).path.rstrip("/") == "/chart" else url


def referer_path(referer, host):
    """The page the browser says the user clicked from, as a safe relative URL, if it was ours."""
    if not referer:
        return None
    u = urllib.parse.urlparse(referer)
    if u.netloc and u.netloc != host:
        return None
    return _safe_back(u.path + ("?" + u.query if u.query else ""))


def _back_label(url):
    """'Insider cluster buys' / 'Raw tables' ... for the arrow's hover text."""
    u = urllib.parse.urlparse(url)
    path = u.path.rstrip("/") or "/signals"
    if path in ("/", "/signals"):
        view = (urllib.parse.parse_qs(u.query).get("view") or [""])[0]
        return VIEWS_BY_KEY.get(view, VIEWS[0]).title
    return dict(NAV_TABS).get(path.lstrip("/"), "the previous page")


# Back means "the page I clicked this ticker from", with its filters, sort and scroll position.
# The browser's own Back does exactly that, so use it when this tab got here from one of our
# pages; otherwise (new tab, bookmark, typed URL) fall back to the link, which still restores
# everything that lives in the URL. Range tabs replace this history entry instead of adding
# one, so changing the range never makes Back mean "the previous range".
BACK_JS = r"""
(function(){
  var back=document.getElementById('back'); if(!back) return;
  var KEY='chartFrom';
  try{
    var same=false, path='';
    if(document.referrer){var u=new URL(document.referrer); same=u.origin===location.origin; path=u.pathname;}
    if(same && path!=='/chart') sessionStorage.setItem(KEY,document.referrer);
    else if(!same) sessionStorage.removeItem(KEY);  // typed or external: no history of ours to return to
  }catch(e){}
  back.addEventListener('click',function(e){
    var from=null; try{from=sessionStorage.getItem(KEY);}catch(x){}
    if(from && history.length>1){ e.preventDefault(); history.back(); }
  });
  document.querySelectorAll('.tabs a[data-range]').forEach(function(a){
    a.addEventListener('click',function(e){
      if(e.ctrlKey||e.metaKey||e.shiftKey||e.altKey||e.button) return;
      e.preventDefault(); location.replace(a.href);
    });
  });
})();
"""


ABOUT_LEAD_CHARS = 320


def _about_html(prof) -> str:
    """The company description block: a facts line, the first few sentences of the business
    summary, and the rest behind 'read more' (no script needed -- it's a <details>)."""
    if not prof or prof.get("status") != "ok":
        return ""
    facts = [x for x in (prof.get("sector"), prof.get("industry"), prof.get("hq")) if x]
    if prof.get("employees"):
        facts.append(f"{prof['employees']:,} employees")
    site = (prof.get("website") or "").strip()
    out = [_esc(f) for f in facts]
    if site.lower().startswith(("http://", "https://")):  # only ever a web link, whatever the source says
        label = site.split("//", 1)[1].removeprefix("www.").rstrip("/")
        out.append(f'<a href="{_esc(site)}" target="_blank" rel="noopener">{_esc(label)}</a>')
    parts = []
    if out:
        parts.append(f'<p class="facts">{" &middot; ".join(out)}</p>')
    text = (prof.get("description") or "").strip()
    if text:
        sentences = re.split(r"(?<=[.!?])\s+", text)
        lead, i = "", 0
        while i < len(sentences) and len(lead) < ABOUT_LEAD_CHARS:
            lead = (lead + " " + sentences[i]).strip()
            i += 1
        rest = " ".join(sentences[i:])
        parts.append(f"<p>{_esc(lead)}</p>")
        if rest:
            parts.append(f"<details><summary>read more</summary><p>{_esc(rest)}</p></details>")
        parts.append('<p class="src">Description: Yahoo Finance (an unofficial source).</p>')
    elif facts:
        parts.append('<p class="src">Category from the issuer\'s SEC filings; no business summary '
                     'is available for it.</p>')
    return f'<div class="about">{"".join(parts)}</div>' if parts else ""


def page_chart(conn, qs, meta, referer=None):
    """Price chart for one ticker with the app's signal data drawn on it. `meta` is the
    ticker's price_meta (prices.ensure ran before this, on its own connection). `referer`
    is the page the click came from (already made safe), used when the URL has no ?back=."""
    ticker = (_one(qs, "ticker", "") or "").strip()
    back = _safe_back(_one(qs, "back", "")) or referer or "/signals"
    rng = _one(qs, "range", "6m")
    rng = rng if rng in RANGES else "6m"
    if not ticker:
        return _page("Chart", "signals", '<p class="empty">Click any ticker in a table to see its '
                                          'price chart.</p>')
    since = (dt.date.today() - dt.timedelta(days=RANGES[rng][1])).isoformat()
    cands = prices.candidates(ticker)
    want = set(cands)
    root = cands[0].split("-")[0] if cands else ticker
    like = f"%{root}%"
    same = lambda t: bool(want & set(prices.candidates(t))) if want else t == ticker

    ins = [dict(r) for r in conn.execute(
        """SELECT DISTINCT trans_date, ticker, issuer_name, owner_name, shares, price, value
           FROM insider_tx WHERE code='P' AND is_derivative=0 AND plan_10b5_1=0
             AND ticker LIKE ? AND trans_date>=? ORDER BY trans_date DESC""", (like, since))
        if same(r["ticker"])]
    cong = [dict(r) for r in conn.execute(
        """SELECT trans_date, ticker, member, tx_type, amount_text FROM congress_tx
           WHERE ticker LIKE ? AND trans_date>=? ORDER BY trans_date DESC""", (like, since))
        if same(r["ticker"]) and r["tx_type"] in ("P", "S")]
    issuer = next((r[0] for r in conn.execute(
        "SELECT issuer_name FROM insider_tx WHERE ticker LIKE ? AND issuer_name<>'' LIMIT 50", (like,))
        if r[0]), "")
    bands = [{"first": b["first_buy"], "last": b["last_buy"],
              "label": f"Cluster: {b['insiders']} insiders bought {b['first_buy']} to {b['last_buy']}, "
                       f"${b['total_value'] or 0:,.0f} in total"}
             for b in signals.cluster_buys(conn, since) if same(b["ticker"])]

    symbol = meta.get("symbol") if meta else None
    bar_rows = prices.bars(conn, symbol, since) if symbol else []
    dates, closes = [b["date"] for b in bar_rows], [b["close"] for b in bar_rows]
    svg = chart.build_svg(bar_rows, chart.insider_markers(ins, dates, closes),
                          chart.congress_markers(cong, dates, closes), bands,
                          title=" · ".join(x for x in (ticker, issuer, f"last {RANGES[rng][0]}") if x))

    status = (meta or {}).get("status")
    if svg:
        main = f'<div class="chartwrap">{svg}</div>'
    elif status == "none":
        main = f'<p class="empty">No price chart for {_esc(ticker)}. {_esc((meta or {}).get("note") or "")}</p>'
    elif status == "error":
        main = (f'<p class="empty">Couldn\'t reach the price source ({_esc((meta or {}).get("note") or "")}). '
                f'Reload in a few minutes to try again.</p>')
    else:
        main = '<p class="empty">No price data in this range.</p>'

    range_tabs = "".join(
        f'<a href="/chart?{_qs(ticker=ticker, range=k, back=back)}" data-range="{k}"'
        f'{ON if k == rng else ""}'
        f'{_tip(f"Show the last {label}. Prices for up to 5 years are stored, so changing the range is instant.")}'
        f'>{label}</a>' for k, (label, _) in RANGES.items())

    rows = [{"trans_date": r["trans_date"], "kind": "insider buy", "person": r["owner_name"],
             "detail": (f"{r['shares']:,.0f} sh @ ${r['price']:,.2f} = ${r['value']:,.0f}"
                        if r["price"] and r["shares"] and r["value"] else
                        f"{r['shares']:,.0f} sh" if r["shares"] else "")} for r in ins]
    rows += [{"trans_date": r["trans_date"], "kind": "congress " + ("buy" if r["tx_type"] == "P" else "sale"),
              "person": r["member"], "detail": r["amount_text"]} for r in cong]
    rows.sort(key=lambda r: r["trans_date"], reverse=True)
    shown = rows[:CHART_TRADE_LIMIT]

    fetched = (meta or {}).get("fetched_at") or ""
    info = (f"{len(ins)} insider buy rows, {len(cong)} congressional trades and {len(bands)} cluster "
            f"window{'s' if len(bands) != 1 else ''} in this range. ")
    if symbol and svg:
        info += (f"Prices: <b>{_esc(symbol)}</b> daily closes from Yahoo Finance (an unofficial source), "
                 f"adjusted for stock splits but not dividends; last fetched {_esc(fetched[:16].replace('T', ' '))} UTC. "
                 f"A reported trade price far from that day's close is drawn at the close instead "
                 f"(usually a typo in the filing, or a stock split since).")
    table = (_table(shown, ["trans_date", "kind", "person", "detail"]) if rows else
             '<p class="empty">No insider buys or congressional trades for this ticker in this range.</p>')
    body = f"""<div class="bar"><a id="back" class="back" href="{_esc(back)}"
  {_tip(f"Back to {_back_label(back)}, as you left it: same filters, sort and place on the page.")}
  >&larr; Back</a><strong>{_esc(ticker)}</strong>
  <span class="nil">{_esc(issuer)}</span></div>
{_about_html(profiles.get(conn, ticker))}
<div class="tabs">{range_tabs}</div>
{main}
<p class="note">{info}</p>
{table}
{f'<div class="count"><span>showing {len(shown)} of {len(rows)} trades</span></div>' if len(rows) > len(shown) else ''}
<script>{BACK_JS}</script>"""
    return _page(f"{ticker} chart", "signals", body)


def _columns(conn, table):
    return [r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _filter_clause(col, expr):
    """'>100000' -> ('"col" > ?', [100000.0]); bare text -> LIKE %text%;
    'null' / '!null' -> empty or not. 'Empty' covers both SQL NULL and '' --
    the ingesters store a missing ticker as '' but a missing price as NULL, and
    nobody filtering should have to know which."""
    expr = expr.strip()
    if expr.lower() == "null":
        return f'("{col}" IS NULL OR "{col}" = \'\')', []
    if expr.lower() in ("!null", "!= null", "not null"):
        return f'("{col}" IS NOT NULL AND "{col}" <> \'\')', []
    for op in ("<=", ">=", "!=", "<", ">", "="):
        if expr.startswith(op):
            val = expr[len(op):].strip()
            try:
                val = float(val)
            except ValueError:
                pass
            return f'"{col}" {op} ?', [val]
    return f'"{col}" LIKE ?', [f"%{expr}%"]


def page_raw(conn, qs):
    table = _one(qs, "table", RAW_TABLES[0])
    if table not in RAW_TABLES:
        table = RAW_TABLES[0]
    all_cols = _columns(conn, table)
    show_all = _one(qs, "cols", "") == "all"
    cols = all_cols if show_all else (
        [c for c in ESSENTIAL_COLS.get(table, all_cols) if c in all_cols] or all_cols)

    # Read filters across every column, not just the visible ones, so narrowing the
    # column set never silently drops a filter the user set earlier.
    filters = {c: _one(qs, f"f_{c}", "") for c in all_cols}
    filters = {c: v for c, v in filters.items() if v}
    where, params = [], []
    for c, expr in filters.items():
        clause, vals = _filter_clause(c, expr)
        where.append(clause)
        params.extend(vals)
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""

    sort = _one(qs, "sort")
    sort = sort if sort in all_cols else None
    direction = "asc" if _one(qs, "dir") == "asc" else "desc"
    order = f' ORDER BY "{sort}" {direction.upper()}' if sort else ""

    offset = max(0, _one(qs, "offset", 0, int) or 0)
    total = conn.execute(f'SELECT COUNT(*) FROM "{table}"{sql_where}', params).fetchone()[0]
    rows = [dict(r) for r in conn.execute(
        f'SELECT * FROM "{table}"{sql_where}{order} LIMIT ? OFFSET ?', (*params, PAGE_SIZE, offset))]

    def sort_href(col, nxt):
        return f"/raw?{_qs(table=table, sort=col, dir=nxt, **{f'f_{k}': v for k, v in filters.items()})}"

    tabs = "".join(
        f'<a href="/raw?{_qs(table=t)}"{ON if t == table else ""}'
        f'{_tip(TABLE_HELP[t])}>{_esc(t)}</a>'
        for t in RAW_TABLES)

    hidden = f'<input type="hidden" name="table" value="{_esc(table)}">'
    if show_all:
        hidden += '<input type="hidden" name="cols" value="all">'
    # keep filters on columns that aren't currently displayed
    for c, v in filters.items():
        if c not in cols:
            hidden += f'<input type="hidden" name="f_{_esc(c)}" value="{_esc(v)}">'
    if sort:
        hidden += f'<input type="hidden" name="sort" value="{_esc(sort)}">' \
                  f'<input type="hidden" name="dir" value="{_esc(direction)}">'

    nav_links = []
    base = {f"f_{k}": v for k, v in filters.items()}
    if offset:
        nav_links.append(f'<a href="/raw?{_qs(table=table, sort=sort, dir=direction, offset=max(0, offset - PAGE_SIZE), **base)}"{_tip("Previous " + str(PAGE_SIZE) + " rows.")}>&#8592; prev</a>')
    if offset + PAGE_SIZE < total:
        nav_links.append(f'<a href="/raw?{_qs(table=table, sort=sort, dir=direction, offset=offset + PAGE_SIZE, **base)}"{_tip("Next " + str(PAGE_SIZE) + " rows.")}>next &#8594;</a>')

    body = f"""<div class="tabs">{tabs}</div>
<form method="get" action="/raw">
  {hidden}
  <div class="bar">
    <label{_tip(CONTROL_HELP['filters'])}>Filters: type text to match, or <code>&gt;1000000</code>
      / <code>=P</code> for comparisons, <code>!null</code> to hide empty cells.
      Click a column heading to sort.</label>
    <button type="submit"{_tip(CONTROL_HELP['apply'])}>Apply</button>
    <a href="/raw?{_qs(table=table)}"><button type="button" class="ghost"
       {_tip(CONTROL_HELP['clear'])}>Clear</button></a>
    <a href="/raw?{_qs(table=table, cols=None if show_all else 'all', sort=sort, dir=direction, **base)}"
       ><button type="button" class="ghost"{_tip(CONTROL_HELP['cols'])}>{
       'Essential columns' if show_all else f'All {len(all_cols)} columns'}</button></a>
    <label>{len(cols)} of {len(all_cols)} columns</label>
  </div>
  {_table(rows, cols, sort_href=sort_href, sort_col=sort, sort_dir=direction, filters=filters)}
</form>
<div class="count">
  <span{_tip("Rows matching the current filters, across the whole table -- not just this page.")}>{total:,} rows{' matching' if filters else ''}</span>
  <span{_tip("Which slice of the matching rows this page shows.")}>showing {offset + 1:,}&#8211;{min(offset + PAGE_SIZE, total):,}</span>
  {' '.join(nav_links)}
</div>"""
    return _page(f"raw: {table}", "raw", body, meta=f"{total:,} rows")


SQL_OK = re.compile(r"^\s*(select|with)\b", re.I)


def run_sql(conn, query):
    """Read-only, one statement, capped rows, capped time. Returns (cols, rows, error)."""
    q = query.strip().rstrip(";").strip()
    if not q:
        return [], [], None
    if not SQL_OK.match(q):
        return [], [], "Only SELECT and WITH queries are allowed."
    if ";" in q:
        return [], [], "One statement at a time."
    deadline = time.monotonic() + SQL_TIMEOUT_S
    conn.set_progress_handler(lambda: time.monotonic() > deadline, 5000)
    try:
        cur = conn.execute(f"SELECT * FROM ({q}) LIMIT {MAX_SQL_ROWS + 1}")
        rows = [dict(r) for r in cur.fetchall()]
        cols = [d[0] for d in cur.description] if cur.description else []
        return cols, rows, None
    except sqlite3.OperationalError as exc:
        msg = str(exc)
        if "interrupted" in msg.lower():
            msg = f"Query exceeded the {SQL_TIMEOUT_S:.0f}s limit and was cancelled."
        return [], [], msg
    except sqlite3.Error as exc:
        return [], [], f"{type(exc).__name__}: {exc}"
    finally:
        conn.set_progress_handler(None, 0)


EXAMPLES = [
    ("Buys by ticker, last 90 days",
     "Rank tickers by how much insiders bought on the open market recently.",
     "SELECT ticker, COUNT(DISTINCT owner_name) AS insiders, SUM(value) AS total\n"
     "FROM insider_tx\n"
     "WHERE code='P' AND is_derivative=0 AND plan_10b5_1=0 AND ticker<>''\n"
     "  AND trans_date >= date('now','-90 days')\n"
     "GROUP BY ticker ORDER BY total DESC"),
    ("Who bought and sold the same ticker",
     "Insiders on both sides of the same stock -- usually exercise-and-sell, "
     "occasionally something more interesting.",
     "SELECT ticker, owner_name,\n"
     "       SUM(CASE WHEN code='P' THEN value END) AS bought,\n"
     "       SUM(CASE WHEN code='S' THEN value END) AS sold\n"
     "FROM insider_tx WHERE is_derivative=0 AND ticker<>''\n"
     "GROUP BY ticker, owner_name HAVING bought > 0 AND sold > 0"),
    ("10b5-1 share of sales",
     "How much insider selling is pre-scheduled plan activity versus discretionary.",
     "SELECT plan_10b5_1, COUNT(*) AS rows, SUM(value) AS total\n"
     "FROM insider_tx WHERE code='S' AND is_derivative=0 GROUP BY plan_10b5_1"),
]


def page_sql(path, qs):
    query = _one(qs, "q", "")
    cols, rows, error = [], [], None
    if query:
        ro = _connect(path, read_only=True)
        try:
            cols, rows, error = run_sql(ro, query)
        finally:
            ro.close()
    truncated = len(rows) > MAX_SQL_ROWS
    rows = rows[:MAX_SQL_ROWS]
    sort, direction = _read_sort(qs, cols)
    rows = _sort_rows(rows, sort, direction)

    examples = " &middot; ".join(
        f'<a href="/sql?{_qs(q=sql)}"{_tip(tip)}>{_esc(label)}</a>'
        for label, tip, sql in EXAMPLES)
    body = f"""<form class="bar" method="get" action="/sql" style="display:block">
  <textarea name="q" placeholder="SELECT ..." spellcheck="false"
    {_tip(CONTROL_HELP['sql_box'])}>{_esc(query)}</textarea>
  <div style="margin-top:6px;display:flex;gap:12px;align-items:center;flex-wrap:wrap">
    <button type="submit"{_tip(CONTROL_HELP['run'])}>Run</button>
    <label{_tip("These limits are enforced by the server and cannot be raised from the page. "
                "Writes are blocked several ways over.")}>Read-only. One statement.
      {MAX_SQL_ROWS:,}-row and {SQL_TIMEOUT_S:.0f}s caps.</label>
  </div>
</form>
<p class="note">Examples: {examples}</p>
{f'<div class="err">{_esc(error)}</div>' if error else ''}
{_table(rows, cols, sort_href=_sorter("/sql", dict(q=query)), sort_col=sort, sort_dir=direction) if query and not error else ''}
{f'<div class="count"><span>{len(rows):,} rows{" (truncated)" if truncated else ""}</span></div>' if query and not error else ''}"""
    return _page("SQL", "sql", body, meta=f"{len(rows):,} rows" if query else "")


# ---------- watchlist ----------

def save_watchlist_from_form(fields: dict):
    """Parse the Watchlist form, validate tickers against SEC where possible,
    save regardless of whether validation could run, and return a human
    summary of what happened."""
    raw_tickers = (fields.get("tickers") or [""])[0]
    tickers = sorted({t.strip().upper() for t in raw_tickers.replace(",", "\n").splitlines()
                      if t.strip()})

    raw_managers = (fields.get("managers_13f") or [""])[0]
    managers = {}
    for line in raw_managers.splitlines():
        line = line.strip()
        if not line:
            continue
        cik, _, name = line.partition(" ")
        managers[cik.strip()] = name.strip()

    detail = f"{len(tickers)} ticker(s), {len(managers)} manager(s)."
    if tickers:
        try:
            http = PoliteSession(config.sec_user_agent(), config.SEC_MIN_INTERVAL)
            found = tickers_to_ciks(http, tickers)
            missing = sorted(set(tickers) - found.keys())
            if missing:
                detail += f" Not on SEC's ticker list (saved anyway): {', '.join(missing)}."
        except SystemExit as exc:
            detail += f" Could not validate against SEC: {exc}"
        except Exception as exc:
            detail += f" Could not validate against SEC: {type(exc).__name__}: {exc}"

    config.save_watchlist({"tickers": tickers, "managers_13f": managers})
    return detail


def page_watchlist(qs):
    wl = config.load_watchlist()
    tickers_text = "\n".join(wl.get("tickers", []))
    managers_text = "\n".join(f"{cik} {name}".rstrip()
                              for cik, name in wl.get("managers_13f", {}).items())

    banner = ""
    detail = _one(qs, "detail")
    if detail:
        banner += f'<p class="note" style="border-bottom:none">Saved. {_esc(detail)}</p>'

    body = f"""{banner}
<form method="post" action="/watchlist">
  <div class="bar"><label{_tip("Stock tickers form4 filters to unless you run it with --all / "
      "all issuers. One per line, or comma-separated. Checked against SEC's own ticker list on "
      "save, so a typo shows up immediately instead of silently matching zero filings.")}
      >Tickers</label></div>
  <textarea name="tickers" style="height:8em" spellcheck="false"
    placeholder="NVDA&#10;AAPL&#10;MSFT">{_esc(tickers_text)}</textarea>
  <div class="bar"><label{_tip("Institutional managers 13F tracks: one per line, CIK then an "
      "optional name, e.g. '1067983 Berkshire Hathaway'. Find a CIK on the manager's own EDGAR "
      "filing page.")}>13F managers</label></div>
  <textarea name="managers_13f" style="height:6em" spellcheck="false"
    placeholder="1067983 Berkshire Hathaway">{_esc(managers_text)}</textarea>
  <div class="bar">
    <button type="submit"{_tip("Validates tickers against SEC where possible, then writes "
        "watchlist.json. Always saves even if validation can't run (no network, or "
        "SEC_USER_AGENT isn't set).")}>Save</button>
  </div>
</form>
<p class="note" style="border-bottom:none">Used by <code>form4</code> (unless run with --all)
and <code>13f</code>. House and Senate always ingest every member regardless of what's here --
there is no per-ticker filter for Congress, only per-source date windows.</p>"""
    return _page("Watchlist", "watchlist", body)


# ---------- jobs ----------

JOB_LABELS = {"form4": "Form 4", "house": "House", "senate": "Senate",
             "13f": "13F", "update": "Update (all + report)"}


def _job_form(source, label, fields_html, tip):
    return (f'<form method="post" action="/jobs" class="bar">'
           f'<input type="hidden" name="source" value="{source}">'
           f'{fields_html}'
           f'<button type="submit"{_tip(tip)}>Run {_esc(label)}</button></form>')


def page_jobs(qs):
    statuses = jobs.status()
    rows = [{"source": JOB_LABELS[s], "status": statuses[s]["status"],
            "started_at": statuses[s].get("started_at") or "",
            "finished_at": statuses[s].get("finished_at") or "",
            "result": statuses[s].get("message") or statuses[s].get("error") or ""}
           for s in jobs.SOURCES]

    banner = ""
    started = _one(qs, "started")
    if started:
        banner = (f'<p class="note" style="border-bottom:none">Started '
                 f'{_esc(JOB_LABELS.get(started, started))} in the background.</p>')
    busy = _one(qs, "busy")
    if busy:
        why = ("it, or one of the sources it runs, is already running" if busy == "update"
               else "it, or Update (which runs it), is already running")
        banner += (f'<div class="err">Can\'t start {_esc(JOB_LABELS.get(busy, busy))}: '
                   f'{why} -- wait for it to finish.</div>')

    days_tip = "Lookback window in days, counted back from today."
    all_tip = "Ignore the watchlist and pull every issuer. This is the slow one -- see Architecture for why."
    forms = "".join([
        _job_form("form4", "Form 4",
                 f'<label{_tip(days_tip)}>days <input type="number" name="days" value="3" min="1"></label>'
                 f'<label{_tip(all_tip)}>'
                 f'<input type="checkbox" name="all" value="1"> all issuers</label>',
                 "Run Form 4 now, in the background."),
        _job_form("house", "House",
                 f'<label{_tip("Clerk filing year.")}>year '
                 f'<input type="number" name="year" value="{dt.date.today().year}"></label>'
                 f'<label{_tip("Only filings on/after this date. Leave blank for the whole year.")}>'
                 f'since <input type="text" name="since" placeholder="YYYY-MM-DD"></label>',
                 "Run House now, in the background."),
        _job_form("senate", "Senate",
                 f'<label{_tip(days_tip)}>days <input type="number" name="days" value="30" min="1"></label>',
                 "Run Senate now, in the background."),
        _job_form("13f", "13F",
                 f'<label{_tip("How many past quarters to fetch per manager.")}>quarters '
                 f'<input type="number" name="quarters" value="4" min="1"></label>',
                 "Run 13F now, in the background. Needs managers in the Watchlist tab."),
        _job_form("update", "Update (all + report)", "",
                 "Run every source in turn -- Form 4 for all issuers over the last 3 days, "
                 "House for this year, Senate for the last 30 days, then 13F -- and rebuild the "
                 "report. Each source's own row above updates as Update reaches it."),
    ])

    refresh = '<meta http-equiv="refresh" content="4">' if jobs.any_running() else ""
    body = f"""{banner}
{_table(rows, ["source", "status", "started_at", "finished_at", "result"])}
<p class="note" style="border-bottom:none">Each button starts that source on a background
thread, the same ingest() the CLI calls -- this page returns immediately rather than
waiting. Only one instance of a given source runs at a time. Update runs Form 4
(all issuers), House, Senate and 13F in turn; their rows show <em>queued</em> until
Update reaches them, then their own status, times and result. A source with nothing
to do (13F with no managers) shows <em>skipped</em>. While Update runs, the individual
buttons are refused, and Update is refused while any of them runs, so two jobs never
write to the database at once. This auto-refreshes every 4 seconds while anything is
running or queued.</p>
{forms}"""
    return _page("Jobs", "jobs", body, extra_head=refresh)


STATUS_HELP = {
    "insider transactions": "Total Form 4 transaction rows stored.",
    "distinct Form 4 filings": "How many separate filings those rows came from. One filing "
                               "commonly reports several transactions.",
    "congressional trades": "Total House and Senate disclosure rows. Zero until you run the "
                            "house and senate ingests.",
    "13F filings": "How many quarterly institutional filings are stored. Zero until you run "
                   "the 13f ingest with managers configured.",
    "insider date range": "Earliest and latest trade date in the database. If a view looks "
                          "empty, check the window against this range first.",
}


PROC_COLS = ["source", "status", "n"]
ERR_COLS = ["source", "doc_id", "note", "processed_at"]


def page_status(conn, qs):
    def scalar(sql, *a):
        row = conn.execute(sql, a).fetchone()
        return row[0] if row else None

    counts = [
        ("insider transactions", scalar("SELECT COUNT(*) FROM insider_tx")),
        ("distinct Form 4 filings", scalar("SELECT COUNT(DISTINCT accession) FROM insider_tx")),
        ("congressional trades", scalar("SELECT COUNT(*) FROM congress_tx")),
        ("13F filings", scalar("SELECT COUNT(DISTINCT accession) FROM holdings_13f")),
        ("insider date range", f'{scalar("SELECT MIN(trans_date) FROM insider_tx") or "-"} '
                               f'to {scalar("SELECT MAX(trans_date) FROM insider_tx") or "-"}'),
    ]
    proc = [dict(r) for r in conn.execute(
        "SELECT source, status, COUNT(*) AS n FROM processed GROUP BY source, status "
        "ORDER BY source, status")]
    errors = [dict(r) for r in conn.execute(
        "SELECT source, doc_id, note, processed_at FROM processed WHERE status='error' "
        "ORDER BY processed_at DESC LIMIT 50")]

    psort, pdir = _read_sort(qs, PROC_COLS)
    proc = _sort_rows(proc, psort, pdir)
    esort, edir = _read_sort(qs, ERR_COLS, "esort", "edir")
    errors = _sort_rows(errors, esort, edir)

    dl = "".join(
        f"<dt{_tip(STATUS_HELP.get(k, ''))}>{_esc(k)}</dt>"
        f"<dd>{_esc(f'{v:,}' if isinstance(v, int) else v)}</dd>"
        for k, v in counts)
    body = f"""<dl>{dl}</dl>
<p class="note">Ingest status by source. Anything in <code>error</code> retries on the next run.</p>
{_table(proc, PROC_COLS, sort_href=_sorter("/status", {}), sort_col=psort, sort_dir=pdir)}
{'<p class="note">Most recent errors.</p>' + _table(errors, ERR_COLS, sort_href=_sorter("/status", {}, "esort", "edir"), sort_col=esort, sort_dir=edir) if errors else ''}"""
    return _page("Status", "status", body)


DESIGN_NOTES = [
    ("One database, no server",
     "A single SQLite file. No migrations tool -- the schema in db.py is just "
     "applied on every connect, and CREATE TABLE IF NOT EXISTS makes that safe "
     "to repeat."),
    ("Incremental by construction",
     "Every document's outcome is recorded in processed as (source, doc_id) -> "
     "status. Re-running a source only fetches documents it hasn't seen, or "
     "that previously errored -- there is no separate 'since last run' state "
     "to keep in sync."),
    ("Parsers, not APIs",
     "None of these sources publish a real bulk API for this data. Each ingest "
     "module fetches whatever format the source actually exposes -- a daily "
     "index file, a PDF, a paginated HTML search form -- and reduces it to "
     "plain rows. That is also why they are the most fragile part of the app."),
    ("Read-only by construction, except three narrow, named exceptions",
     "Signals, Raw tables, SQL, Status and Architecture never write: every "
     "connection they open runs PRAGMA query_only, so even a bug there cannot "
     "touch the database, including from the SQL console. Watchlist, Jobs and "
     "the price chart are the deliberate exceptions -- Watchlist writes watchlist.json "
     "(never the database), Jobs starts a background thread that calls the same "
     "ingest() functions the CLI does, and opening a ticker's chart fetches its prices and "
     "description once and caches them (prices, price_meta and profiles, on a separate connection). "
     "Nothing else here writes anything."),
    ("Rate-limited per site's own rules",
     f"net.PoliteSession enforces {config.SEC_MIN_INTERVAL}s between requests to SEC EDGAR "
     f"(its fair-access policy caps at 10/s) and {config.CONGRESS_MIN_INTERVAL}s to the "
     "House and Senate sites, which are small government servers with no such policy "
     "published -- just a reason to be gentle."),
]


def page_architecture(conn, qs):
    schema_rows = []
    for t in RAW_TABLES:
        n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        schema_rows.append({"table": t, "purpose": TABLE_HELP[t], "rows": n})

    modules_rows = [{"module": f, "responsibility": desc} for f, desc in MODULES]

    notes_html = "".join(
        f'<p class="note" style="border-bottom:none">'
        f'<strong style="color:var(--fg)">{_esc(k)}.</strong> {_esc(v)}</p>'
        for k, v in DESIGN_NOTES)

    body = f"""<p class="note">Four independent scrapers write into one SQLite file;
three independent consumers read it back out. Nothing here talks to anything
else here directly -- every arrow below is either an HTTP request or a SQL
query against <code>data/insider_tracker.db</code>.</p>
<pre class="diagram">{_esc(DIAGRAM)}</pre>
<p class="note">The <a href="/raw">Raw tables</a> and <a href="/sql">SQL</a> tabs
in this app are the "ad-hoc SQL" branch above -- they read the four tables
directly rather than going through signals.py. The <a href="/status">Status</a>
tab reads the <code>processed</code> table, off to the side of this diagram.</p>
<p class="note">Nothing in that diagram starts on its own -- a source's
ingest() only runs when something calls it: <code>python -m insider_tracker
&lt;source&gt;</code> from a terminal (<code>cli.py</code>), a weekday cron job
running <code>update</code> (see the README), the Discord alerts job on a server,
which runs <code>alerts --ingest-days 4</code> each weekday morning (DEPLOY.md), or clicking a button on the
<a href="/jobs">Jobs</a> tab, which runs the same ingest() on a background
thread and writes to <code>watchlist.json</code> via the <a href="/watchlist"
>Watchlist</a> tab for which tickers and managers to use.</p>

<p class="note" style="border-bottom:none;padding-bottom:0">Module map, roughly
in the order data flows through them:</p>
{_table(modules_rows, ["module", "responsibility"])}

<p class="note" style="border-bottom:none;padding-bottom:0">The schema -- four
tables, row counts from the database you have right now:</p>
{_table(schema_rows, ["table", "purpose", "rows"])}

<p class="note" style="border-bottom:none;padding-bottom:0">Design choices worth
knowing before you change any of this:</p>
{notes_html}"""
    return _page("Architecture", "architecture", body)


def _csv_bytes(rows, cols):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


# ---------- server ----------

class _Handler(BaseHTTPRequestHandler):
    db_path = None
    server_version = "insider-tracker"

    def log_message(self, fmt, *args):  # the console is for ingest logs, not hits
        pass

    def _send(self, body: bytes, ctype="text/html; charset=utf-8", extra=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        route = parsed.path.rstrip("/") or "/"
        if route == "/favicon.ico":
            self._send(b"", "image/x-icon")
            return
        conn = _connect(self.db_path, read_only=True)
        try:
            if route in ("/", "/signals"):
                html_out = page_signals(conn, qs)
            elif route == "/signals.csv":
                view = VIEWS_BY_KEY.get(_one(qs, "view", ""), VIEWS[0])
                days, since = _window(qs)
                kwargs = {p.name: _one(qs, PARAM_PREFIX + p.name, p.default, p.cast)
                          for p in view.params}
                rows = view.fn(conn, since, **kwargs) if view.takes_since else view.fn(conn, **kwargs)
                rows = _apply_date_filters(rows, _read_date_filters(qs, view.date_filters))
                rows = _apply_filters(rows, _read_signal_filters(qs, view.cols))
                csort, cdir = _read_sort(qs, view.cols)
                rows = _sort_rows(rows, csort, cdir)
                self._send(_csv_bytes(rows, view.cols), "text/csv; charset=utf-8",
                           {"Content-Disposition": f'attachment; filename="{view.key}.csv"'})
                return
            elif route == "/raw":
                html_out = page_raw(conn, qs)
            elif route == "/sql":
                html_out = page_sql(self.db_path, qs)
            elif route == "/chart":
                # The one GET that writes: it tops up the price cache on first open. Not through
                # `conn` (read-only), and prices.safe_ensure can't raise.
                t = (_one(qs, "ticker", "") or "").strip()
                price_meta = prices.safe_ensure(self.db_path, t) if t else None
                if t:  # the description needs the symbol the price lookup settled on
                    profiles.safe_ensure(self.db_path, t, (price_meta or {}).get("symbol"))
                html_out = page_chart(conn, qs, price_meta,
                                      referer_path(self.headers.get("Referer"), self.headers.get("Host")))
            elif route == "/watchlist":
                html_out = page_watchlist(qs)
            elif route == "/jobs":
                html_out = page_jobs(qs)
            elif route == "/status":
                html_out = page_status(conn, qs)
            elif route == "/architecture":
                html_out = page_architecture(conn, qs)
            else:
                self.send_error(404, "No such page")
                return
        except Exception as exc:  # a broken view shouldn't take the server down
            html_out = _page("Error", "", f'<div class="err">{_esc(type(exc).__name__)}: {_esc(exc)}</div>')
        finally:
            conn.close()
        self._send(html_out.encode("utf-8"))

    def do_POST(self):
        """The only writes this app performs: saving watchlist.json, and starting
        a background ingest thread. Both redirect back to a GET (303, not 302,
        so a refresh replays the harmless GET rather than resubmitting the form)."""
        route = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        fields = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)

        if route == "/watchlist":
            try:
                detail = save_watchlist_from_form(fields)
                location = f"/watchlist?{_qs(detail=detail)}"
            except Exception as exc:
                location = f"/watchlist?{_qs(detail=f'Save failed: {type(exc).__name__}: {exc}')}"
        elif route == "/jobs":
            source = (fields.get("source") or [""])[0]
            if source not in jobs.SOURCES:
                location = "/jobs"
            else:
                params = {k: v[0] for k, v in fields.items() if k != "source"}
                started = jobs.start(source, self.db_path, **params)
                location = f"/jobs?{_qs(started=source)}" if started else f"/jobs?{_qs(busy=source)}"
        else:
            self.send_error(404, "No such page")
            return

        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _Server(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets a second process bind a port that is already
    # in use and then steal connections from the first at random -- so a stale
    # server silently shadows the one you just started. Fail loudly instead.
    allow_reuse_address = os.name != "nt"


def serve(db_path=None, host="127.0.0.1", port=8765, open_browser=True):
    """Serve the UI. Refuses any non-loopback host: the SQL console makes
    exposing this to a network a bad idea, so that is not a decision we leave
    to a flag."""
    if host not in LOOPBACK:
        raise SystemExit(
            f"Refusing to bind {host}: this UI has a SQL console and is meant for "
            f"localhost only. Use 127.0.0.1, or tunnel over SSH if you need remote access.")
    path = str(Path(db_path or config.DB_PATH))
    db.connect(path).close()  # make sure the file and schema exist before serving

    _Handler.db_path = path
    try:
        httpd = _Server((host, port), _Handler)
    except OSError as exc:
        raise SystemExit(
            f"Can't bind {host}:{port} ({exc.strerror or exc}). Something is already "
            f"listening there -- most likely a UI you started earlier. Stop it, or "
            f"pass --port with a free port.")
    url = f"http://{host}:{httpd.server_address[1]}/"
    print(f"insider-tracker UI on {url}  (database: {path})")
    print("Ctrl-C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
