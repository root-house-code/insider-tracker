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

from . import background, backtest, chart, config, db, earnings, funds, issuers, jobs, news, prices, profiles, quotes, signals, tone
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
            ("watchlist", "Watchlist"), ("tone", "Tone"), ("jobs", "Jobs"),
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
    "tone": "Tune how news headlines are scored good or bad (the news_tone column). Label real "
            "headlines yourself, adjust the word weights and phrase rules, and see how often the "
            "scores agree with you before saving. Writes tone.toml and tone_labels.json, never the "
            "database.",
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
    "stake_change": "How much this purchase grew the holding it went into: shares bought divided by "
                    "the shares held just before (shares_after minus shares). 'new' means they held "
                    "none before -- the strongest form of conviction. Sort by it to rank purchases by "
                    "commitment rather than dollar size. Blank when the filing's numbers don't add up.",
    "stored": "A file this app keeps, or (indented) a table inside the database file.",
    "rows": "How many rows the table holds.",
    "size": "Files: exact size on disk. Tables: an estimate -- the stored values added up, without "
            "indexes or SQLite's own overhead -- so tables won't add up to the database file.",
    "what": "What it holds.",
    "news_tone": "The tone of the headlines from the month before the first buy, scored by VADER "
                 "with finance-specific word weights: negative, mixed, neutral or positive, then the "
                 "average score (-1 to +1) and how many headlines it rests on. Insiders buying after "
                 "negative news is the more telling case. A handful of headlines is a rough guide, "
                 "not a verdict -- check the news column. Tune the scoring on the Tone tab.",
    "news": "Recent news for the company: Yahoo Finance headlines and its own SEC 8-K filings "
            "(earnings, an executive change, a deal). Click to expand. Items before the first buy are "
            "marked -- insiders buying right after bad news is a stronger signal than buying into "
            "good news. Sorts by the latest item's date.",
    "last_30d": "Daily closes over the last 30 days, with the cluster's buying window shaded, and the "
                "change over the period. Did insiders buy into a fall, or chase a rise? Sorts by the "
                "change. Prices come from Yahoo Finance and are fetched in the background the first "
                "time a ticker appears; click the ticker for the full chart.",
    "flags": "Why a cluster is probably not discretionary buying. fund: the issuer is a fund (closed-end "
             "or interval), per its SEC record -- its directors buy routinely. reit: a real estate investment "
             "trust -- REIT clusters trailed small caps in both halves of the 2015-2026 backtest. lockstep: every insider paid "
             "the identical price on the same day -- an IPO allocation, a placement or a purchase plan. "
             "routine: every buyer bought in this same month each of the last three years -- a habit, "
             "not news. Hidden unless hide noise is 0. A lockstep flag can catch two directors who genuinely bought "
             "together, so look before dismissing one.",
    "buyers": "What kind of buyer each insider was, judged by their own purchases of this stock in the "
              "three years before: routine (bought in this same month each of the last three years -- "
              "research finds these carry little information), first buy (none in three years), "
              "occasional (in between), unknown (the data doesn't reach back three years).",
    "prior_30d": "The stock's move over the month before entry: insiders buying into a fall, or chasing "
                 "a rise?",
    "beat_iwm": "Share of these clusters that beat IWM over the same days.",
    "avg_vs_iwm": "Average excess return over IWM, leaving out the most extreme 1% at each end. Closest to "
                  "what holding every one of these clusters equally would have earned. Usually above the "
                  "median: a minority of big winners lifts the average while the typical cluster lags.",
    "quarter_timing": "Where in the company's quarter the buying began: days since its last earnings report "
                      "(SEC 8-K, Item 2.02). Most companies bar insider trading in the weeks before results. "
                      "In the 2015-2026 backtest, CFO clusters bought 1-2 months after results were the only "
                      "group where more than half beat small caps in both halves of the period; those bought "
                      "2+ months after (close to the next report) did worse. unknown: the company doesn't file "
                      "Item 2.02 8-Ks, or its dates haven't been fetched yet.",
    "known_on": "The filing date of the purchase that made this a cluster (brought in the last insider "
                "needed): the first day anyone could have known. Trade dates come earlier and would flatter "
                "the results.",
    "entry_date": "The next trading day after known_on. Returns are measured from that day's open.",
    "ret_5d": "Return from the entry open to the close 5 trading days later (about a week).",
    "ret_10d": "Return to the close 10 trading days after entry (about two weeks).",
    "ret_20d": "Return to the close 20 trading days after entry (about a month).",
    "ret_60d": "Return to the close 60 trading days after entry (about three months).",
    "ret_120d": "Return to the close 120 trading days after entry (about six months).",
    "ret_to_date": "Return from the entry open to the latest close.",
    "vs_spy": "ret_to_date minus SPY's return over the same days: above zero, it beat the S&P 500.",
    "vs_iwm": "ret_to_date minus IWM's return over the same days: the fairer yardstick for small "
              "companies, which most clusters are.",
    "days_held": "Trading days from entry to the latest close.",
    "status": "open: has returns. no prices yet: Yahoo has no history for it (often delisted, which "
              "leaves the results looking better than they were). awaiting entry: too recent to have "
              "traded since.",
    "horizon": "How long after entry the return is measured.",
    "events": "How many clusters this figure rests on.",
    "median_return": "The middle return: half did better, half worse. Steadier than the mean.",
    "mean_return": "The average return. One huge winner or loser can move it a lot.",
    "beat_spy": "Share of clusters that beat SPY over the same days.",
    "median_vs_spy": "The middle cluster's return minus SPY's over the same days.",
    "median_vs_iwm": "The middle cluster's return minus IWM's (small caps) over the same days.",
    "reliability": "Whether the number of clusters is enough to tell a real effect from luck. A few "
                   "weeks of overlapping events from one market is a small sample whatever the count.",
    "group": "A slice of the clusters, to see which kinds have done better.",
    "net_funds": "Institutions that opened or added to a position, minus those that cut or closed one, "
                 "in the latest 13F quarter -- among every manager that filed both quarters. Above zero: "
                 "institutions were net accumulating.",
    "fund_breadth": "Net funds as a share of all holders: (opened + added - cut - closed) / holders. "
                    "Size-neutral, unlike net_funds, which favours mega-caps simply because they have "
                    "thousands of holders churning. High breadth on a smaller stock means it was being "
                    "discovered. Sort by it to compare companies of different sizes. Blank when the stock had "
                    "fewer than 10 holders the quarter before or has fewer than 20 now: usually a fresh "
                    "listing (every holder is 'new'), or too few to mean anything.",
    "funds_new": "13F managers that held none of this stock the quarter before and some now.",
    "funds_added": "Managers that grew an existing position by more than 5% (smaller moves are index "
                   "rebalancing noise and count as held).",
    "funds_cut": "Managers that shrank a position by more than 5%.",
    "funds_exited": "Managers that held this stock the quarter before and none now.",
    "fund_holders": "How many 13F managers held this stock at the end of the latest quarter.",
    "fund_shares_change": "Change in the total shares those managers held, quarter on quarter.",
    "tracked_funds": "Your Watchlist managers holding this stock, and what each did with it: +12%, new, "
                     "exited, held. Add managers on the Watchlist tab; they are picked up the next time "
                     "Funds runs on the Jobs tab.",
    "market_cap": "The company's stock market value (Yahoo Finance, refreshed daily). Weigh the buying "
                  "against it: $500k is a rounding error at a $50B company and a statement at a $50M one.",
    "avg_volume": "Average shares traded per day over the last three months (Yahoo Finance). Low volume "
                  "means a thinly traded stock, where insider buying can move the price by itself and "
                  "where getting in or out at a fair price is harder.",
    "max_stake_change": "The biggest stake change among this cluster's buyers: how much one insider's "
                        "purchases in the window grew their holding ('new' = held none before). "
                        "Separate holdings -- shares held directly and each trust or fund -- are "
                        "tracked apart and added up, so a small top-up to one trust can't look huge.",
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
              "usual bar. Raise it to 3 or more to cut the list down to the strongest cases."),
        Param("hide_noise", "hide noise", 1, int,
              "1 hides clusters flagged as probably not discretionary buying: a fund's directors (funds "
              "buy their own shares routinely), or every insider at one price on one day (an IPO "
              "allocation, placement or purchase plan). 0 shows them, with the reason in flags.")],
       ["ticker", "issuer_name", "last_30d", "insiders", "roles", "buyers", "flags", "quarter_timing", "max_stake_change",
        "total_value", "market_cap", "avg_volume", "first_buy", "last_buy", "news", "news_tone", "who"],
       "Two or more insiders at one company buying on the open market (code P), "
       "outside 10b5-1 plans. The strongest signal here.",
       "Several insiders at the same company buying on the open market at the same time. The "
       "strongest signal in this tool: insiders sell for many reasons but buy for essentially "
       "one. Start here.",
       date_filters=("first_buy", "last_buy")),
    _v("fund_overlap", "Fund / insider overlap", signals.fund_overlap,
       [Param("min_insiders", "min insiders", 1, int,
              "How many different insiders must have bought in the window. 1 shows every stock an "
              "insider bought; 2 or more keeps only clusters."),
        Param("min_net_funds", "min net funds", 1, int,
              "How many more institutions must have opened or added to a position than cut or closed "
              "one, in the latest 13F quarter. Raise it to keep only stocks institutions were clearly "
              "accumulating."),
        Param("hide_noise", "hide noise", 1, int,
              "1 hides clusters flagged as probably not discretionary buying: a fund's directors (funds "
              "buy their own shares routinely), or every insider at one price on one day (an IPO "
              "allocation, placement or purchase plan). 0 shows them, with the reason in flags.")],
       ["ticker", "issuer_name", "last_30d", "insiders", "roles", "buyers", "flags", "quarter_timing", "max_stake_change",
        "total_value", "net_funds", "fund_breadth", "funds_new", "funds_added", "funds_cut", "funds_exited", "fund_holders",
        "fund_shares_change", "tracked_funds", "market_cap", "avg_volume", "first_buy", "last_buy",
        "news_tone"],  # no "who" (a 30-name list made rows tall) or "news": both are on Insider cluster buys
       "Stocks insiders bought on the open market (code P, outside 10b5-1 plans) that institutions "
       "were also net buying in the latest 13F quarter, across every 13F filer.",
       "Insider buying that institutions were also accumulating: more 13F managers opened or added "
       "to a position than cut or closed one. 13F data is quarterly and lags by months, so read it as "
       "what funds were doing before the insiders bought, not alongside them.",
       date_filters=("first_buy", "last_buy")),
    _v("backtest", "Backtest: cluster buys", lambda conn, min_insiders=2, hide_noise=1: backtest.events(
           conn, min(max(min_insiders, backtest.LEVELS[0]), backtest.LEVELS[-1]), hide_noise),
       [Param("min_insiders", "min insiders", 2, int,
              "How many different insiders make a cluster: 2, 3 or 4. Each is dated by the filing that "
              "brought in the last of them -- the first day anyone could have acted on it -- so a 3-insider "
              "cluster is entered later than the same cluster at 2."),
        Param("hide_noise", "hide noise", 1, int,
              "1 hides clusters flagged as probably not discretionary buying: a fund's directors (funds "
              "buy their own shares routinely), or every insider at one price on one day (an IPO "
              "allocation, placement or purchase plan). 0 shows them, with the reason in flags.")],
       ["ticker", "issuer_name", "known_on", "entry_date", "insiders", "buyers", "total_value", "prior_30d",
        "ret_5d", "ret_10d", "ret_20d", "ret_60d", "ret_120d", "ret_to_date", "vs_spy", "vs_iwm", "days_held",
        "status", "flags"],
       "Every insider cluster buy in the database, replayed as if you had bought at the next open after "
       "it became public, with its returns since -- against SPY and IWM. Years of history are stored; "
       "the last few months are recomputed each time you open it.",
       "How the cluster-buy signal has actually done since each cluster became public. Read the summary "
       "first: it says how many clusters each figure rests on and whether that is enough to mean anything.",
       takes_since=False, date_filters=("known_on",)),
    _v("large_buys", "Large officer / director purchases", signals.large_buys,
       [Param("min_value", "min $", config.LARGE_BUY_USD, float,
              "Smallest purchase to include, in dollars. Lower it to see more; raise it to keep "
              "only purchases big enough to matter to the buyer personally."),
        Param("limit", "rows", 30, int, "Maximum rows to return, largest first.")],
       ["trans_date", "ticker", "issuer_name", "owner_name", "relationship",
        "shares", "price", "value", "shares_after", "stake_change", "url"],
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
    ("form345.py", "Years of insider purchases (2015 on by default) from the SEC's quarterly Insider "
                   "Transactions Data Sets, loaded into the same tables as the live Form 4 ingest. "
                   "Feeds the backtest and each insider's routine / first-buy history."),
    ("backtest.py", "Replays every insider cluster from the day it became public, at 2, 3 and 4 "
                    "insiders, and measures returns against SPY and IWM. Settled history is computed "
                    "once by the Backtest job and stored; recent clusters are recomputed live."),
    ("issuers.py", "Operating company or fund, from each issuer's SEC record (funds have no industry code "
                   "and file N-CSR/N-2 reports). Feeds the noise filters: a fund's directors buying "
                   "its shares, or every insider at one price on one day, is not a signal."),
    ("funds.py", "Every 13F filer at once, from the SEC's quarterly Form 13F data sets: how many "
                 "institutions opened, added to, cut or closed each stock, quarter on quarter. Stores "
                 "a per-stock summary (fund_flows), not the millions of positions behind it."),
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
    ("news.py", "Recent news per ticker: Yahoo Finance headlines and the company's SEC 8-K filings, "
                "for the Insider cluster buys tab and the chart page. Headlines are kept as they arrive, "
                "since Yahoo only shows the last week or two; refreshed every six hours."),
    ("tone.py", "Scores headlines from negative to positive with VADER plus finance word weights and "
                "phrase rules, for the news_tone column. Every weight is tunable on the Tone tab, which "
                "measures agreement with your own labels; settings in tone.toml, labels in tone_labels.json."),
    ("quotes.py", "Market cap and average daily volume for the Insider cluster buys tab, from Yahoo "
                  "Finance, cached for a day. Fetched on a background thread the first time a ticker "
                  "appears, so the page never waits on Yahoo; failures leave the cells blank."),
    ("background.py", "One background thread per cache fill (prices, quotes, news), so a "
                       "page shows what is cached at once and the rest fills in while you read."),
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
LIST_COLS = {"who", "member_names", "roles", "tracked_funds", "flags", "buyers"}

MONEY_COLS = {"value", "total_value", "min_total_usd", "value_now", "value_usd",
              "amount_low", "amount_high", "min_bought_usd", "min_sold_usd"}
COUNT_COLS = {"net_funds", "funds_new", "funds_added", "funds_cut", "funds_exited", "fund_holders",
              "shares", "shares_after", "shares_now", "shares_prior", "insiders",
              "members", "buys", "sells", "lag_days", "seq"}
RATIO_COLS = {"bought_sold_ratio"}
BACKTEST_PCT = ("prior_30d", "beat_iwm", "ret_5d", "ret_10d", "ret_20d", "ret_60d", "ret_120d", "ret_to_date", "vs_spy", "vs_iwm",
                "median_return", "mean_return", "median_vs_spy", "median_vs_iwm", "avg_vs_iwm")
COMPACT_COLS = {"market_cap": "$", "avg_volume": ""}  # shown as $1.2B / 340K; sorted on the number
STAKE_COLS = {"stake_change", "max_stake_change"}  # percent; math.inf = a brand-new position

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
.news{margin:0;padding:0;list-style:none}
.news li{margin:0 0 3px;line-height:1.4}
.news .when{color:var(--muted);font-size:11px;white-space:nowrap}
.news .pre{font-size:11px;border:1px solid var(--line);padding:0 3px;margin-right:3px}
td details summary{cursor:pointer}
td details[open]{min-width:34ch}
.newsbox{padding:8px 12px;border-bottom:1px solid var(--rule)}
svg.spark{vertical-align:middle;margin-right:4px}
.newsbox h3{margin:0 0 6px;font-size:13px}
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
.filters select{width:100%;min-width:5.5em;max-width:7em;  /* an open list still shows whole names */
                font:11px ui-monospace,Consolas,monospace;
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
      // A closed <details> (the news column) keeps its hidden text laid out in Chromium
      // (content-visibility), so it still has rectangles: skip all of it but the summary line.
      var fold=el.closest('details');
      if(fold && !fold.open && !el.closest('summary')) continue;
      var c=cs(el), re=/\S+/g, m, data=node.nodeValue;
      while((m=re.exec(data))){
        var r=document.createRange();
        r.setStart(node,m.index); r.setEnd(node,m.index+m[0].length);
        var txt=c.textTransform==='uppercase'?m[0].toUpperCase():m[0], rects=r.getClientRects();
        if(!rects.length) continue;  // not rendered: inside a closed <details>, display:none, ...
        if(rects.length===1){ fn(txt,rects[0],c); continue; }
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

  // The price chart and the sparklines are inline SVG; as images they can't see the page's CSS
  // variables, so bake the current theme's colours into a copy and rasterise that.
  function chartImage(){
    var svg=document.querySelector('svg.chart');
    return svg?svgImage(svg):Promise.resolve(null);
  }
  function sparkImages(){
    var list=Array.prototype.slice.call(document.querySelectorAll('table svg.spark'));
    return Promise.all(list.map(svgImage)).then(function(imgs){
      var m=new Map(); list.forEach(function(s,i){ if(imgs[i]) m.set(s,imgs[i]); }); return m;
    });
  }
  function svgImage(svg){
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
    return Promise.all([chartImage(),sparkImages()]).then(function(got){
    var chart=got[0], sparks=got[1];
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
          cell.querySelectorAll('svg.spark').forEach(function(sv){
            var im=sparks.get(sv), r=sv.getBoundingClientRect();
            if(im) ctx.drawImage(im.img,PAD+(r.left-it.tr.left),entry.y+(r.top-entry.rect.top),im.w,im.h);
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
    if isinstance(v, NewsCell):
        return v.html(), ""
    if isinstance(v, ToneCell):
        return v.html(), ""
    if isinstance(v, SparkCell):
        return v.html(), "num"
    if col in COMPACT_COLS and isinstance(v, (int, float)):
        return COMPACT_COLS[col] + _compact(v), "num"
    if col in STAKE_COLS and isinstance(v, (int, float)):
        return signals.stake_text(v), "num"
    if col in ("beat_spy", "beat_iwm") and isinstance(v, (int, float)):
        return f"{v:.0f}%", "num"
    if col in ("pct_change", "fund_shares_change", "fund_breadth") + BACKTEST_PCT and isinstance(v, (int, float)):
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
        if c in MONEY_COLS or c in COUNT_COLS or c in RATIO_COLS or c in STAKE_COLS or c in COMPACT_COLS or c == "last_30d" or c == "url":
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
    if col in ("url", "news", "last_30d"):  # a link, a fold-out list, a chart: nothing to pick from
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
        label = v if col in LIST_COLS else str(v) if isinstance(v, ToneCell) else _plain(col, v)
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


def _head(col) -> str:
    """A column name for a header cell, allowed to wrap after each underscore: long names like
    fund_shares_change would otherwise set the column's width on their own."""
    return "_<wbr>".join(_esc(part) for part in col.split("_"))


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
            first = "desc" if (c in MONEY_COLS or c in COUNT_COLS or c in RATIO_COLS or c in STAKE_COLS or c in COMPACT_COLS or c in ("last_30d", "fund_shares_change", "fund_breadth") + BACKTEST_PCT) else "asc"
            nxt = ("desc" if sort_dir == "asc" else "asc") if c == sort_col else first
            inner = f'<a href="{_esc(sort_href(c, nxt))}">{_head(c)}{mark}</a>'
            help_text = (help_text + "  Click to sort." if help_text else "Click to sort.")
        else:
            inner = _head(c)
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


def _compact(v) -> str:
    for div, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v / div:.1f}{unit}"
    return f"{v:,.0f}"


NEWS_LEAD_DAYS = 30   # how far before a cluster's first buy its news column reaches
NEWS_IN_CELL = 12
NEWS_CHART_SINCE = lambda: (dt.date.today() - dt.timedelta(days=60)).isoformat()


def _news_item(i, before=None, settings=None) -> str:
    """One headline or 8-K as a list item. Titles are third-party text: always escaped, and only
    ever an http(s) link."""
    day = (i["published"] or "")[:10]
    mark = '<span class="pre" title="Published before the first buy">before</span>' if before and day < before else ""
    title = _esc(i["title"])
    if (i["url"] or "").startswith(("http://", "https://")):
        title = f'<a href="{_esc(i["url"])}" target="_blank" rel="noopener">{title}</a>'
    src = "SEC 8-K" if i["kind"] == "8k" else i["source"]
    score = tone.score_item(i, settings) if settings else None
    tone_txt = f" &middot; tone {score:+.2f}" if score is not None else ""
    return f'<li>{mark}{title} <span class="when">{_esc(src)} &middot; {_esc(day)}{tone_txt}</span></li>'


class NewsCell(str):
    """A cluster's news: compares and sorts as its latest item's date, renders as a fold-out list."""
    def __new__(cls, items, first_buy, settings=None):
        cell = super().__new__(cls, (items[0]["published"] or "")[:10] if items else "")
        cell.items, cell.first_buy, cell.settings = items, first_buy, settings
        return cell

    def html(self):
        if not self.items:
            return ""
        pre = sum(1 for i in self.items if (i["published"] or "")[:10] < (self.first_buy or ""))
        filings = sum(1 for i in self.items if i["kind"] == "8k")
        head = f"{len(self.items)} item{'s' if len(self.items) != 1 else ''}"
        head += f" &middot; {pre} before" if pre else ""
        head += f" &middot; {filings} 8-K" if filings else ""
        body = "".join(_news_item(i, self.first_buy, self.settings) for i in self.items)
        return f'<details><summary>{head}</summary><ul class="news">{body}</ul></details>'


SPARK_DAYS = 30


class SparkCell(float):
    """A ticker's last-30-days line: sorts as the percent change, draws as a sparkline."""
    def __new__(cls, change, svg, first, last):
        cell = super().__new__(cls, change)
        cell.svg, cell.first, cell.last = svg, first, last
        return cell

    def html(self):
        tip = (f"Close {self.first:,.2f} to {self.last:,.2f} over the last {SPARK_DAYS} days ({self:+.1f}%). "
               "Shaded: the days the cluster was buying.")
        return f'<span{_tip(tip)}>{self.svg}{self:+.1f}%</span>'


def _add_sparks(conn, rows, fetch=False) -> str:
    """Attach each row's 30-day sparkline from the price cache. With fetch, tickers whose prices
    are missing or stale are fetched in the background, and the note says so."""
    since = (dt.date.today() - dt.timedelta(days=SPARK_DAYS)).isoformat()
    tickers = list(dict.fromkeys(r["ticker"] for r in rows if r.get("ticker")))
    for r in rows:
        meta = prices.get_meta(conn, r["ticker"]) if r.get("ticker") else None
        bars = prices.bars(conn, meta["symbol"], since) if meta and meta.get("symbol") else []
        svg = chart.sparkline_svg(bars, (r.get("first_buy"), r.get("last_buy")))
        r["last_30d"] = (SparkCell(100 * (bars[-1]["close"] / bars[0]["close"] - 1), svg,
                                   bars[0]["close"], bars[-1]["close"]) if svg and bars[0]["close"] else None)
    need = prices.stale(conn, tickers) if fetch else []
    if not (need and prices.refresh_in_background(_db_file(conn), need)):
        return ""
    return (f'<p class="note">Fetching prices for {len(need):,} ticker{"s" if len(need) != 1 else ""} from '
            "Yahoo Finance in the background, for the last_30d column. Reload in a minute.</p>")


# One-click screens: a label, the query they set, and what they're for. Each is just a link, so
# the result can be bookmarked, adjusted with the controls, or shared like any other view.
CFO_TIP = ("The CFO among the buyers. In the 2015-2026 backtest, clusters with the CFO buying beat small caps "
           "(IWM) by about 2% on average over the following three and six months, in both halves of the "
           "period -- the one pattern that held. Still under half of them beat IWM: the edge comes from a "
           "minority of big winners, so it shows up across many such clusters, not in any one.")
# The CFO mid-quarter screen shows clusters as the backtest bought them: just after they became
# public. A 21-day window holds one bout of buying (buys at most 14 days apart) without reaching
# back into an older one; a buy in the last 5 days means the filing that completed it is fresh.
FRESH_BUY_DAYS = 5


def _days_ago(n):
    return lambda: (dt.date.today() - dt.timedelta(days=n)).isoformat()


CFO_MID_TIP = ("CFO among the buyers, and the buying began one to two months after the company's last earnings "
               "report (mid-quarter). In the 2015-2026 backtest this was the one group where more than half beat "
               "small caps (IWM) in both halves of the period. It rests on a few hundred clusters and was found "
               "by looking, so the backtest tracks it from 2026-10-24 to see whether it holds. Set to show "
               "only fresh clusters, as the backtest bought them: 2+ insiders within a 21-day window, the latest "
               "buy in the last 5 days. Buy at the next open; the edge showed up across many such clusters, "
               "not in any one.")
PRESETS = {
    "cluster_buys": [
        ("CFO mid-quarter", dict(days=21, p_min_insiders=2, p_hide_noise=1, sf_roles="CFO",
                                 sf_quarter_timing=earnings.MID_QUARTER, df_last_buy_from=_days_ago(FRESH_BUY_DAYS),
                                 sort="total_value", dir="desc"), CFO_MID_TIP),
        ("CFO buying", dict(days=30, p_min_insiders=2, p_hide_noise=1, sf_roles="CFO", sort="total_value", dir="desc"),
         CFO_TIP),
    ],
    "fund_overlap": [
        ("CFO mid-quarter", dict(days=21, p_min_insiders=2, p_min_net_funds=1, p_hide_noise=1, sf_roles="CFO",
                                 sf_quarter_timing=earnings.MID_QUARTER, df_last_buy_from=_days_ago(FRESH_BUY_DAYS),
                                 sort="fund_breadth", dir="desc"), CFO_MID_TIP),
        ("CFO buying", dict(days=30, p_min_insiders=1, p_min_net_funds=1, p_hide_noise=1, sf_roles="CFO",
                            sort="fund_breadth", dir="desc"), CFO_TIP),
        ("Conviction screen", dict(days=30, p_min_insiders=2, p_min_net_funds=1, sort="fund_breadth", dir="desc"),
         "Two or more insiders buying in the last 30 days, in stocks institutions were net buying, ranked by "
         "fund breadth (net buyers as a share of holders) so smaller stocks being discovered rise to the top."),
        ("Top executives", dict(days=30, p_min_insiders=1, p_min_net_funds=1, sf_roles="CEO", sort="fund_breadth",
                                dir="desc"),
         "Any purchase that includes the CEO, in stocks institutions were net buying, ranked by fund breadth."),
    ],
}


def _presets(view_key) -> str:
    # a value can be a function, worked out when the page is shown (e.g. "5 days ago")
    links = [f'<a href="/signals?{_qs(view=view_key, **{k: v() if callable(v) else v for k, v in params.items()})}"'
             f'{_tip(tip)}>{_esc(label)}</a>'
             for label, params, tip in PRESETS.get(view_key, [])]
    return f'<p class="note">Screens: {" &middot; ".join(links)}</p>' if links else ""


def _backtest_summary(conn, rows) -> str:
    """The headline tables above the Backtest list. Keeps itself current: recent clusters' prices
    are topped up in the background, and the stored history is refreshed if it's over a week old."""
    cutoff = (dt.date.today() - dt.timedelta(days=backtest.LIVE_DAYS)).isoformat()
    recent = [r["ticker"] for r in rows if r["known_on"] >= cutoff]
    notes = []
    need = prices.stale(conn, [*dict.fromkeys(recent), *backtest.BENCHMARKS])
    if need and prices.refresh_in_background(_db_file(conn), need):
        notes.append(f"Updating prices for {len(need):,} recent ticker{'s' if len(need) != 1 else ''} in the background.")
    last = backtest.last_refresh(conn)
    stale = not last or dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last) > dt.timedelta(days=7)
    if background.running("backtest"):
        notes.append("Updating the stored history in the background (the first run fetches price history for "
                     "thousands of tickers and takes up to an hour; later runs take minutes).")
    elif stale and backtest.refresh_in_background(_db_file(conn)):
        notes.append("Started updating the stored history in the background.")
    first = signals.data_start(conn) or "-"
    priced = [r for r in rows if r.get("status") == "open"]
    missing = sum(1 for r in rows if r.get("status") == "no prices")
    head = (f'<p class="note">{len(rows):,} clusters since {_esc(first)}, each bought at the next open after it became '
            f"public; {len(priced):,} have prices. "
            + (f"{missing:,} have no price history (often delisted) and are left out, which flatters the results. "
               if missing else "")
            + f"History last updated: {_esc((last or 'never')[:16].replace('T', ' '))}."
            + "".join(f" <strong>{_esc(n)}</strong>" for n in notes) + "</p>")
    group_cols = ["group", "events", "avg_vs_iwm", "median_vs_iwm", "beat_iwm", "reliability"]
    return (head
            + _table(backtest.summary(rows), ["horizon", "events", "median_return", "mean_return", "beat_spy",
                                              "median_vs_spy", "median_vs_iwm", "reliability"])
            + f'<p class="note" style="border-bottom:none">Which kinds of cluster did better: excess return over IWM '
              f"{backtest.FACTOR_HORIZON} trading days after entry, by what was known at entry. The average (extremes "
              "trimmed) is what holding all of them earned; the median is the typical one -- usually lower, "
              "because a minority of big winners lifts the average:</p>"
            + _table(backtest.groups(rows), group_cols)
            + f'<p class="note" style="border-bottom:none">The live test: clusters since {backtest.TRACK_FROM}, when the '
              "CFO screen was picked from the history above. Only these say whether it holds up going forward; "
              "expect months before there are enough.</p>"
            + _table(backtest.out_of_sample(rows), group_cols)
            + '<p class="note" style="border-bottom:none">The same, early years against later ones. A difference that '
              "shows up in only one period was probably luck; one that holds in both is worth trusting:</p>"
            + _table(backtest.by_period(rows), group_cols)
            + '<p class="note" style="border-bottom:none">Every cluster:</p>')


def _fund_note(conn) -> str:
    """Which 13F quarters the fund columns compare, or how to load them."""
    meta = funds.latest(conn)
    if not meta:
        return ('<p class="note"><strong>No 13F data loaded yet.</strong> Run <em>Funds</em> on the '
                '<a href="/jobs">Jobs</a> tab (or <code>python -m insider_tracker funds</code>): it downloads the '
                "SEC's two latest quarterly 13F data sets, about 200 MB, and takes a minute or two.</p>")
    return (f'<p class="note"{_tip("A 13F shows positions on the last day of a quarter, filed up to 45 days later; the SEC publishes each quarterly data set after that. Run Funds on the Jobs tab once a new one is out (early March, June, September, December).")}>'
            f"Fund columns: 13F positions on <strong>{_esc(meta['period'])}</strong> against "
            f"{_esc(meta['prior_period'])}, across {meta['compared']:,} institutions that filed both quarters. "
            "That quarter ended before most of this insider buying, so it shows what funds were doing "
            "beforehand.</p>")


def _tone_settings():
    """The saved tone settings, or None when VADER isn't installed or tone.toml is broken (the
    Tone tab says which); every tone cell is then blank rather than an error."""
    if not tone.available():
        return None
    try:
        return tone.load()
    except tone.SettingsError:
        return None


class ToneCell(str):
    """A cluster's headline tone: compares, sorts and filters as its label, shows the evidence."""
    def __new__(cls, summary):
        cell = super().__new__(cls, summary[0] if summary else "")
        cell.avg, cell.n = (summary[1], summary[2]) if summary else (None, 0)
        return cell

    def html(self):
        if not self:
            return ""
        what = f"{self.n} headline{'s' if self.n != 1 else ''}"
        return (f'{_esc(str(self))} <span class="nil" title="Average score from -1 to +1, over '
                f'{what} from before the first buy">{self.avg:+.2f} &middot; {self.n}</span>')


def _cluster_news(items, first_buy) -> list:
    """What a cluster's cell lists, newest first: every 8-K, and headlines split so that a burst of
    recent stories can't crowd out the ones from before the buying started (the useful context)."""
    day = lambda i: (i["published"] or "")[:10]
    filings = [i for i in items if i["kind"] == "8k"]
    heads = [i for i in items if i["kind"] != "8k"]
    before = [i for i in heads if first_buy and day(i) < first_buy][:NEWS_IN_CELL // 2]
    after = [i for i in heads if not (first_buy and day(i) < first_buy)][:NEWS_IN_CELL - len(before)]
    return sorted(filings + before + after, key=lambda i: i["published"] or "", reverse=True)


def _add_news(conn, rows, fetch=False) -> str:
    """Attach each cluster's news (from a month before its first buy) as a NewsCell. With fetch,
    tickers not looked at recently are fetched in the background, and the note says so."""
    tickers = [r["ticker"] for r in rows if r.get("ticker")]
    settings = _tone_settings()
    for r in rows:
        if not r.get("ticker"):
            continue
        since = (dt.date.fromisoformat(r["first_buy"]) - dt.timedelta(days=NEWS_LEAD_DAYS)).isoformat() \
            if r.get("first_buy") else None
        items = news.get(conn, r["ticker"], since, 200)
        r["news"] = NewsCell(_cluster_news(items, r.get("first_buy")), r.get("first_buy"), settings)
        lead = (dt.date.fromisoformat(r["first_buy"]) - dt.timedelta(days=settings["lead_days"])).isoformat() \
            if settings and r.get("first_buy") else None
        before = [i for i in items if lead and lead <= (i["published"] or "")[:10] < r["first_buy"]]
        r["news_tone"] = ToneCell(tone.summarize(before, settings)) if settings else ToneCell(None)
    need = news.stale(conn, tickers) if fetch else []
    if not (need and news.refresh_in_background(_db_file(conn), need)):
        return ""
    return (f'<p class="note">Fetching news for {len(need):,} ticker{"s" if len(need) != 1 else ""} '
            "(Yahoo Finance headlines and SEC 8-K filings) in the background. Reload in a minute.</p>")


def _news_html(items) -> str:
    """The chart page's news block: the last two months of headlines and 8-K filings."""
    if not items:
        return ""
    return ('<div class="newsbox"><h3>Recent news</h3><ul class="news">'
            + "".join(_news_item(i, settings=_tone_settings()) for i in items)
            + '</ul><p class="src" style="margin:6px 0 0;color:var(--muted);font-size:11px">Headlines: Yahoo '
              'Finance (an unofficial source). Filings: SEC EDGAR.</p></div>')


def _add_quotes(conn, rows, fetch=False) -> str:
    """Attach cached market cap and average volume to each row. With fetch, missing or stale
    tickers are fetched on a background thread, and the returned note says so."""
    tickers = list(dict.fromkeys(r["ticker"] for r in rows if r.get("ticker")))
    have = quotes.get_many(conn, tickers)
    for r in rows:
        q = have.get(r.get("ticker")) or {}
        r["market_cap"], r["avg_volume"] = q.get("market_cap"), q.get("avg_volume")
    need = quotes.stale(conn, tickers) if fetch else []
    if not (need and quotes.refresh_in_background(_db_file(conn), need)):
        return ""
    return (f'<p class="note"{_tip("Yahoo Finance is unofficial, so cells it cannot fill stay blank.")}>'
            f"Fetching market cap and volume for {len(need):,} ticker{'s' if len(need) != 1 else ''} "
            "from Yahoo Finance in the background. Reload in a minute to see them.</p>")


def _db_file(conn) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2]


MAX_TABLE_ROWS = 1000  # more than this and a page gets slow to load and scroll; the CSV has everything


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
    market_note = _add_quotes(conn, all_rows, fetch=True) if "market_cap" in view.cols else ""
    if "news" in view.cols or "news_tone" in view.cols:  # the tone is read from the same news
        market_note += _add_news(conn, all_rows, fetch=True)
    if "last_30d" in view.cols:
        market_note += _add_sparks(conn, all_rows, fetch=True)
    if "net_funds" in view.cols:
        market_note = _fund_note(conn) + market_note
    if "quarter_timing" in view.cols:  # earnings dates not fetched yet (or due a refresh)
        need = earnings.missing(conn, [r.get("issuer_cik") for r in all_rows if r.get("issuer_cik")])
        if need and earnings.refresh_in_background(_db_file(conn), need):
            market_note += (f'<p class="note">Fetching earnings dates for {len(need):,} '
                            f'compan{"ies" if len(need) != 1 else "y"} from the SEC in the background.</p>')
    if "flags" in view.cols:  # issuers whose type isn't known yet: look them up in the background
        need = issuers.missing(conn, [r.get("issuer_cik") for r in all_rows if r.get("issuer_cik")])
        if need and issuers.refresh_in_background(_db_file(conn), need):
            market_note += (f'<p class="note">Checking {len(need):,} issuer{"s" if len(need) != 1 else ""} '
                            "with the SEC (company or fund?) in the background.</p>")
    if view.key == "backtest":
        market_note = _backtest_summary(conn, all_rows) + market_note
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
{f'<p class="note">{_esc(view.note)}</p>' if view.note else ''}{_presets(view.key)}{market_note}
{_table(rows[:MAX_TABLE_ROWS], view.cols, sort_href=_sorter("/signals", base), sort_col=sort, sort_dir=direction,
        filter_cells=filter_cells)}
<div class="count"><span>{f"{len(rows):,} of {len(all_rows):,} rows" if (flt or dfl) else f"{len(rows):,} rows"}{f" -- showing the first {MAX_TABLE_ROWS:,}; sort or filter to see others, or download the CSV for all" if len(rows) > MAX_TABLE_ROWS else ""}</span>
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
    # every separate bout of buying in the range, not the whole range as one group
    bands = [{"first": b["first"], "last": b["last"],
              "label": f"Cluster: {b['insiders']} insiders bought {b['first']} to {b['last']}, "
                       f"${b['total_value']:,.0f} in total"}
             for b in signals.cluster_episodes(ins)]

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
{_news_html(news.get(conn, ticker, NEWS_CHART_SINCE()))}
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


TONE_SHOW = {"all": "all headlines", "labeled": "labelled by you", "unlabeled": "not labelled yet",
             "disagree": "where the score disagrees with you", "bad": "scored bad",
             "good": "scored good"}
TONE_ROWS = 300
VERDICT = {1: "good", 0: "neutral", -1: "bad"}


def page_tone(conn, qs, preview=None, form=None, error=None):
    """Tune headline tone: your labels, the agreement they give, the settings, and the headlines."""
    if not tone.available():
        return _page("Tone", "tone", '<p class="empty">Headline tone needs VADER: run '
                                     '<code>pip install -r requirements.txt</code> and restart.</p>')
    try:
        saved = tone.load()
    except tone.SettingsError as exc:
        saved, error = tone.validate({}), error or f"tone.toml couldn't be read, so the built-in settings are shown: {exc}"
    current = preview or saved
    labels = tone.load_labels()
    show = _one(qs, "show", "all")
    show = show if show in TONE_SHOW else "all"
    q = (_one(qs, "q", "") or "").strip()

    def stats_line(a, title):
        if not a["labeled"]:
            return f"<li>{title}: label some headlines below to measure this.</li>"
        pct = lambda x, n: f"{x}/{n}" + (f" ({x / n:.0%})" if n else "")
        return (f"<li><strong>{title}</strong>: agrees with you on {pct(a['agree'], a['labeled'])} &middot; "
                f"bad news caught {pct(a['bad_caught'], a['bad'])} &middot; good news caught "
                f"{pct(a['good_caught'], a['good'])} &middot; called bad when it wasn't: {a['false_bad']} "
                f"&middot; opposite of you: {a['opposite']}</li>")
    a_saved = tone.agreement(labels, saved)
    a_now = tone.agreement(labels, current) if preview else a_saved
    stats = stats_line(a_saved, "Saved settings")
    if preview:
        stats += stats_line(a_now, "This preview (not saved)")

    text = lambda k, default: (form.get(k) or [default])[0] if form else default
    num = lambda k: _esc(text(k, f"{current[k]:g}"))
    banner = ""
    if _one(qs, "detail"):
        banner = f'<p class="note" style="border-bottom:none">{_esc(_one(qs, "detail"))}</p>'
    if error:
        banner += f'<p class="note" style="border-bottom:none"><strong>Not saved:</strong> {_esc(error)}</p>'

    rows = conn.execute(
        "SELECT title, MAX(published) AS published, MIN(ticker) AS ticker FROM news_items "
        "WHERE kind='headline' GROUP BY title ORDER BY published DESC").fetchall()
    freq = tone.frequent_words([r["title"] for r in rows], current)
    freq_html = ("<details class=\"note\"><summary>The scored words that turn up most in your "
                 f"{len(rows):,} headlines</summary><p>"
                 + " &middot; ".join(f"{_esc(w)} <span class=\"nil\">{n}&times; {v:+g}{' (set here)' if mine else ''}</span>"
                                     for w, n, v, mine in freq)
                 + "</p><p class=\"nil\">A company or fund name, a disease, or jargon that VADER reads as emotion "
                   "(VADER gives 'shares' +1.2) skews every headline it appears in: set it to 0 in the word "
                   "weights.</p></details>") if freq else ""
    shown = []
    for r in rows:
        title = r["title"]
        if q and q.lower() not in title.lower():
            continue
        score, drivers = tone.explain(title, current)
        verdict = tone.classify(score, current)
        mine = labels.get(title)
        keep = {"all": True, "labeled": mine is not None, "unlabeled": mine is None,
                "disagree": mine is not None and mine != verdict, "bad": verdict == -1,
                "good": verdict == 1}[show]
        if keep:
            shown.append((r, score, drivers, verdict, mine))
    back = _qs(show=show, q=q) if q else _qs(show=show)
    body_rows = []
    for r, score, drivers, verdict, mine in shown[:TONE_ROWS]:
        why = ", ".join(f"{_esc(n)} {v:+g}" for n, v in drivers[:4]) or '<span class="nil">no scored words</span>'
        buttons = "".join(
            f'<button type="submit" name="label" value="{v}"{ON if VERDICT.get(mine) == v else ""}'
            f'{_tip("Remove your label" if v == "clear" else f"Label this headline {v} news for the stock")}>{v}</button>'
            for v in ("good", "neutral", "bad", "clear") if v != "clear" or mine is not None)
        flag = (' style="font-weight:bold"' if mine is not None and mine != verdict else "")
        body_rows.append(
            f'<tr><td>{_esc((r["published"] or "")[:10])}</td><td>{_cell("ticker", r["ticker"])[0]}</td>'
            f'<td class="txt">{_esc(r["title"])}</td><td class="num"{flag}>{score:+.2f} {VERDICT[verdict]}</td>'
            f'<td class="txt">{why}</td><td>{VERDICT.get(mine, "")}</td>'
            f'<td><form method="post" action="/tone/label" style="margin:0">'
            f'<input type="hidden" name="title" value="{_esc(r["title"])}">'
            f'<input type="hidden" name="back" value="{_esc(back)}">{buttons}</form></td></tr>')
    heads = [("date", "When the headline was published."), ("ticker", "The company it was fetched for."),
             ("headline", "As the publisher wrote it."),
             ("score", "VADER's score with the settings above, -1 to +1, and what it counts as. Bold when it "
                       "disagrees with your label."),
             ("why", "The words and phrases that carried the score, with their weights. Change a weight in "
                     "the settings to change how much it counts."),
             ("yours", "Your label, if you've given one."), ("label", "Say what this headline means for the stock.")]
    table = ('<div class="wrap"><table><thead><tr>'
             + "".join(f"<th{_tip(t)}>{_esc(h)}</th>" for h, t in heads) + "</tr></thead><tbody>"
             + "".join(body_rows) + "</tbody></table></div>") if body_rows else \
        '<p class="empty">No headlines match. News is fetched when the Insider cluster buys tab or a chart is opened.</p>'
    show_links = " &middot; ".join(
        f'<a href="/tone?{_qs(show=k, q=q) if q else _qs(show=k)}"{ON if k == show else ""}>{_esc(v)}</a>'
        for k, v in TONE_SHOW.items())

    body = f"""{banner}
<p class="note">How well the headline scores match your own reading of {len(labels):,} labelled headline{'s' if len(labels) != 1 else ''}.
Bad news is counted separately because it is rare and it is what the news_tone column most needs to get right.
Change the settings, press <em>Preview</em> to see the effect, and <em>Save</em> when it's better.</p>
<ul class="note" style="margin:0;padding:6px 12px 6px 30px">{stats}</ul>
{freq_html}
<form method="post" action="/tone">
  <div class="bar">
    <label{_tip("One headline at or beyond plus/minus this score counts as good or bad news (VADER's own convention is 0.05).")}>headline cut <input type="number" step="any" name="item_cut" value="{num('item_cut')}"></label>
    <label{_tip("A set of headlines whose average is beyond plus/minus this reads positive or negative.")}>tone cut <input type="number" step="any" name="label_at" value="{num('label_at')}"></label>
    <label{_tip("Otherwise the set is 'mixed' if it holds at least one headline this bad and one this good.")}>mixed at <input type="number" step="any" name="mixed_at" value="{num('mixed_at')}"></label>
    <label{_tip("A cluster's tone covers headlines from this many days before its first buy.")}>days before <input type="number" step="1" name="lead_days" value="{num('lead_days')}"></label>
  </div>
  <div class="bar"><label{_tip("One per line: word = weight, on VADER's scale of about -4 (worst news) to +4 (best). 0 makes a word count for nothing, for words that are only bad news outside finance (cancer, crude, gross).")}>Word weights</label></div>
  <textarea name="words" style="height:12em" spellcheck="false">{_esc(text('words', tone.words_text(current)))}</textarea>
  <div class="bar"><label{_tip("One per line: name: pattern = weight. The pattern is a regular expression (case-insensitive) matched across the headline, so it can catch phrases a single word can't: 'cuts guidance', 'misses estimates'.")}>Phrase rules</label></div>
  <textarea name="phrases" style="height:10em" spellcheck="false">{_esc(text('phrases', tone.phrases_text(current)))}</textarea>
  <div class="bar"><label{_tip("8-K filings whose type is bad news by nature, scored -1 to 0. Other 8-Ks aren't scored.")}>Bad 8-K types</label></div>
  <textarea name="bad_8k" style="height:6em" spellcheck="false">{_esc(text('bad_8k', tone.bad_8k_text(current)))}</textarea>
  <div class="bar">
    <button type="submit" name="action" value="preview"{_tip("Re-score everything below with these settings. Nothing is saved.")}>Preview</button>
    <button type="submit" name="action" value="save"{_tip("Write these settings to tone.toml; the news_tone column uses them from now on.")}>Save</button>
    <button type="submit" name="action" value="reset" class="ghost"{_tip("Delete tone.toml and go back to the built-in settings.")}>Reset to built-in</button>
  </div>
</form>
<form class="bar" method="get" action="/tone"><input type="hidden" name="show" value="{_esc(show)}">
  <label{_tip("Only headlines containing this text.")}>search <input type="text" name="q" value="{_esc(q)}"></label>
  <button type="submit">Find</button> <span>{show_links}</span></form>
{table}
<div class="count"><span>{min(len(shown), TONE_ROWS):,} of {len(shown):,} headlines{' (scored with the preview)' if preview else ''}</span></div>"""
    return _page("Tone", "tone", body, meta=f"{len(labels):,} labelled")


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
             "13f": "13F", "funds": "Funds", "history": "History", "backtest": "Backtest", "update": "Update (all + report)"}


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
        _job_form("funds", "Funds", "",
                 "Download the SEC's two newest quarterly 13F data sets (every institution, about 200 MB, "
                 "only when they're new) and rebuild the fund columns of Fund / insider overlap. Takes a "
                 "minute or two; worth running once a quarter."),
        _job_form("history", "History",
                 f'<label{_tip("First year to load. The SEC data goes back to 2006; each year adds about 30,000 purchases.")}>'
                 f'since <input type="number" name="since" value="2015" min="2006"></label>',
                 "Load years of insider purchases from the SEC's quarterly data sets (about 10 MB a "
                 "quarter). Only quarters not loaded yet are fetched, plus the newest one; a few minutes "
                 "the first time, seconds after that. Feeds the backtest and routine-buyer flags."),
        _job_form("backtest", "Backtest", "",
                 "Compute and store the backtest for every cluster in the history: fetches each ticker's "
                 "price history from Yahoo (up to an hour the first time; minutes after that, since only "
                 "clusters still maturing are redone). The Backtest view also starts this itself weekly."),
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
    "stored data": "Everything this app keeps on disk. The database file and the other files are exact "
                   "sizes; the per-table figures inside the database are estimates (the stored values "
                   "added up, without indexes or SQLite's own overhead), so they won't sum to the file.",
    "insider date range": "Earliest and latest trade date in the database. If a view looks "
                          "empty, check the window against this range first.",
}


PROC_COLS = ["source", "status", "n"]
STORAGE_COLS = ["stored", "rows", "size", "what"]
TABLE_PURPOSE = {
    "insider_tx": "Form 4 transactions", "insider_holdings": "Form 4 holdings with no trade",
    "congress_tx": "House and Senate trades", "holdings_13f": "13F positions",
    "processed": "what each ingest has done", "prices": "daily prices for charts and sparklines",
    "price_meta": "when each ticker's prices were fetched", "profiles": "company descriptions",
    "quotes": "market cap and average volume", "news_items": "headlines and 8-K filings",
    "news_meta": "when each ticker's news was fetched", "cusip_ticker": "13F CUSIP to ticker lookups",
    "alert_log": "trades the Discord bot has handled", "alert_groups": "Discord bot cooldowns",
    "fund_flows": "every 13F filer's changes, per stock", "fund_tracked": "your watchlist managers' 13F positions",
    "fund_meta": "which 13F quarters were compared", "earnings": "earnings report dates (SEC 8-K Item 2.02)",
    "earnings_meta": "when each company's earnings dates were fetched", "bt_events": "stored backtest results, one per cluster",
    "bt_meta": "when the backtest history was last updated", "issuer_info": "operating company or fund, per the SEC",
}
ERR_COLS = ["source", "doc_id", "note", "processed_at"]


def _size(n) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


def _dir_size(path) -> int:
    path = Path(path)
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else 0


def _storage_rows(conn) -> tuple:
    """The database file (exact), each table inside it (rows, and an estimated size), and the other
    files the app keeps (exact). Per-table sizes are estimated because this SQLite build has no dbstat."""
    db_file = Path(_db_file(conn))
    files = [db_file] + [db_file.with_name(db_file.name + x) for x in ("-wal", "-shm")]
    total = sum(_dir_size(f) for f in files)
    rows = [{"stored": db_file.name, "rows": None, "size": _size(total),
             "what": "the database: every table below (exact file size, including its write-ahead log)"}]
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    sized = []
    for t in tables:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
        expr = " + ".join(f'COALESCE(LENGTH(CAST("{c}" AS BLOB)), 0)' for c in cols) or "0"
        n, est = conn.execute(f'SELECT COUNT(*), COALESCE(SUM({expr}), 0) FROM "{t}"').fetchone()
        sized.append((est, {"stored": f"  {t}", "rows": n, "size": "about " + _size(est),
                            "what": TABLE_PURPOSE.get(t, "")}))
    rows += [r for _, r in sorted(sized, key=lambda x: -x[0])]
    others = [(funds.DIR, "the SEC's quarterly 13F data sets (re-downloadable; the two newest are kept)"),
              (config.REPORT_DIR, "Markdown and CSV reports"), (config.WATCHLIST_PATH, "your watchlist"),
              (tone.CONFIG_PATH, "your headline-tone settings"), (tone.LABELS_PATH, "your headline labels"),
              (Path(os.environ.get("INSIDER_ALERTS", "alerts.toml")), "Discord alert rules")]
    for path, what in others:
        if Path(path).exists():
            total += _dir_size(path)
            rows.append({"stored": str(path), "rows": None, "size": _size(_dir_size(path)), "what": what})
    rows.append({"stored": "TOTAL", "rows": sum(r["rows"] or 0 for r in rows), "size": _size(total),
                 "what": "everything above: the database file plus the other files (exact)"})
    return rows, total


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
    storage, total = _storage_rows(conn)
    dl += f"<dt{_tip(STATUS_HELP['stored data'])}>stored data</dt><dd>{_size(total)} in total</dd>"
    body = f"""<dl>{dl}</dl>
<p class="note"{_tip(STATUS_HELP['stored data'])}>Stored data: the database file and the other files this app keeps.
Table sizes inside the database are estimates.</p>
{_table(storage, STORAGE_COLS)}
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
    ("Read-only by construction, except four narrow, named exceptions",
     "Signals, Raw tables, SQL, Status and Architecture never write: every "
     "connection they open runs PRAGMA query_only, so even a bug there cannot "
     "touch the database, including from the SQL console. Watchlist, Tone, Jobs and "
     "the price chart are the deliberate exceptions -- Watchlist writes watchlist.json "
     "and Tone writes tone.toml and tone_labels.json (never the database), Jobs starts a background thread that calls the same "
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
    w.writerows(signals.csv_safe(rows))
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
                if "market_cap" in view.cols:
                    _add_quotes(conn, rows)  # what is cached; a download never waits on Yahoo
                if "news" in view.cols or "news_tone" in view.cols:
                    _add_news(conn, rows)
                if "last_30d" in view.cols:
                    _add_sparks(conn, rows)
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
                    news.safe_ensure(self.db_path, t)
                html_out = page_chart(conn, qs, price_meta,
                                      referer_path(self.headers.get("Referer"), self.headers.get("Host")))
            elif route == "/watchlist":
                html_out = page_watchlist(qs)
            elif route == "/tone":
                html_out = page_tone(conn, qs)
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

        if route == "/tone":
            action = (fields.get("action") or ["save"])[0]
            if action == "reset":
                tone.reset()
                location = f"/tone?{_qs(detail='Back to the built-in settings.')}"
            else:
                try:
                    settings = tone.from_form(fields)
                    error = None
                except tone.SettingsError as exc:
                    settings, error = None, str(exc)
                if action == "save" and settings:
                    tone.save(settings)
                    location = f"/tone?{_qs(detail='Saved to tone.toml.')}"
                else:  # a preview, or a mistake to fix: show the page, write nothing
                    conn = _connect(self.db_path, read_only=True)
                    try:
                        body = page_tone(conn, {}, preview=settings, form=fields, error=error)
                    finally:
                        conn.close()
                    self._send(body.encode("utf-8"), "text/html; charset=utf-8")
                    return
        elif route == "/tone/label":
            one = lambda k: (fields.get(k) or [""])[0]
            value = {"good": 1, "neutral": 0, "bad": -1}.get(one("label"))
            if one("title"):
                tone.set_label(one("title"), value)
            back = one("back")
            location = "/tone" + (f"?{back}" if back and "\n" not in back and "\r" not in back else "")
        elif route == "/watchlist":
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
        jobs.load_history(path)  # each source's last finished run, so the Jobs tab survives a restart
    except Exception as exc:
        __import__("logging").getLogger(__name__).warning("could not load past job runs: %s", exc)
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
