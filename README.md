# insider-tracker

Scrapes, stores, and reports on stock trades by corporate insiders (SEC Form 4),
institutional managers (SEC 13F), and members of Congress (House and Senate
Periodic Transaction Reports). Everything lands in one SQLite file; a daily
command produces a Markdown report plus CSVs.

## Setup

Needs Python 3.11 or newer.

```bash
pip install -r requirements.txt
export SEC_USER_AGENT="Your Name you@example.com"   # required by the SEC
cp watchlist.example.json watchlist.json            # edit tickers / 13F managers
python tests/test_parsers.py                        # offline sanity check
```

## Usage

```bash
python -m insider_tracker update              # daily: all sources + report (watchlist tickers)
python -m insider_tracker update --all        # Form 4 for every issuer, not just the watchlist

python -m insider_tracker form4 --days 7                      # watchlist from watchlist.json
python -m insider_tracker form4 --start 2026-01-01 --all      # backfill everything (slow, see below)
python -m insider_tracker house --year 2026
python -m insider_tracker senate --since 2026-01-01
python -m insider_tracker 13f --managers 1067983 --quarters 4
python -m insider_tracker report --days 30
```

Reports go to `reports/YYYY-MM-DD/report.md` with one CSV per section.

## Exploring interactively

```bash
python -m insider_tracker ui          # http://127.0.0.1:8765, opens your browser
```

A plain HTML UI over the same database.

- **Signals** - the eight report sections, but with their thresholds as form
  controls. Change the window, the minimum purchase size, or how many insiders
  make a cluster, and the page re-queries. Every view exports CSV. Insider cluster buys
  also has date ranges for **first buy** and **last buy** (for example "last buy since
  Oct 1" = still active), separate from the window, which decides which purchases count.
- **Noise filters** (cluster, fund overlap and backtest views, and the Discord bot). About a third
  of raw "clusters" aren't discretionary buying; they're flagged and hidden by default:
  - **fund:** the issuer is a closed-end or interval fund, according to its SEC record. Its
    directors buy routinely.
  - **lockstep:** every insider paid the identical price on the same day: an IPO allocation, a
    placement or a purchase plan.

  - **routine:** every buyer bought in the same month each of the last three years.
  - **reit:** a real estate investment trust. REIT clusters trailed small caps in both halves of the
    2015-2026 backtest.

  Set **hide noise** to 0 to see them with the reason in **flags**. The bot's `skip_funds` and
  `skip_lockstep` settings do the same there.
- **CFO buying** (a preset on the cluster and fund overlap views). In the 2015-2026 backtest,
  clusters with the CFO among the buyers beat IWM by about 2% on average over three and six months,
  in both halves of the period. It was the only pattern of 34 tested that held up. Even so, under
  half of them beat IWM: the edge comes from a minority of big winners, so it only shows up across
  many such clusters. The backtest tracks the screen out of sample from 2026-10-24, when it was
  chosen; that's the real test of whether it holds.
- **CFO mid-quarter** (another preset), plus a **quarter_timing** column. It shows where in the
  company's quarter the buying began, measured from its last earnings report (SEC 8-K, Item 2.02).
  - In 2015-2026, CFO clusters bought 1-2 months after results were the only group where more
    than half beat IWM in both periods (56% and 53%).
  - Buying close to the next report did worse.
  - It rests on a few hundred clusters and was found by looking, so the backtest tracks it from
    2026-10-24.

  Earnings dates are refreshed every two weeks, in the background or by the Backtest job.
- **Insider cluster buys** columns:
  - **roles:** who bought, most senior first (CEO, CFO, ..., Director, 10% owner), read from
    each filer's free-text title. Filterable, so you can keep just the clusters a CEO joined.
  - **max_stake_change:** the biggest growth in one buyer's holding ("new" if they held none
    before), counting all their trusts and direct shares.
  - **last_30d:** a sparkline of the last 30 days of closes, with the cluster's buying
    days shaded and the change over the period, so you can see whether insiders bought
    into a fall or chased a rise. Sorts by the change; Copy PNG includes it.
  - **market_cap** and **avg_volume:** from Yahoo Finance, cached for a day and fetched in the
    background the first time a ticker appears.
  - **news:** click to expand. It lists Yahoo Finance headlines and the company's own SEC 8-K
    filings (results, an executive change, a deal), from a month before the first buy. Items
    from before the buying started are marked "before". The chart page lists the last two
    months of news too.
  - **news_tone:** negative, mixed, neutral or positive, with the average score and how many
    headlines it rests on. It covers the headlines from the month before the first buy. Scored
    by VADER with finance word weights, so "beats estimates" counts as good, while "cancer",
    "crude" or "shares" count for nothing.
- **Insider history** - run **History** on the Jobs tab (or `python -m insider_tracker history
  --since 2015`). It loads every open-market purchase since then from the SEC's quarterly Insider
  Transactions Data Sets: about 375,000 purchases, a few minutes the first time. Implausible dates
  (typos, purchases reported years late) are skipped. History powers:
  - **buyers:** for each insider, whether this purchase was their *first* in three years, a
    *routine* one (same month each year, little information), or *occasional*;
  - the backtest below.
- **Backtest: cluster buys** (a Signals view) - how every cluster has done since it became public.
  - **Entry:** the next open after the filing that made it a cluster. Using trade dates would flatter
    the results.
  - **Returns:** after 5, 10, 20, 60 and 120 trading days and to date, against SPY and IWM.
  - **Summaries:** a table by horizon (median, mean, how often it beat SPY), and one by company size
    and number of insiders. Each figure says how many clusters it rests on and whether that's enough
    to mean anything.

  With history loaded it covers about 17,000 clusters since 2015. The **Backtest** job (Jobs tab,
  also started weekly by the view itself) fetches each ticker's price history once and stores only
  the results; recent clusters are recomputed live, so the view stays current. Its tables show:
  - which kinds of cluster did better, by what was known at entry: first-time vs routine buyers,
    CEO/CFO, prior 30-day move, liquidity, dollars bought;
  - those factors split into 2015-2020 and 2021 onwards, since a factor that only works in one
    period was probably luck. Clusters with no price history (often delisted) drop out and the page
  says how many, since that flatters the results.
- **Fund / insider overlap** (a Signals view) - stocks insiders bought that institutions were also net
  buying, across *every* 13F filer rather than a chosen few. It has the cluster columns plus:
  - how many funds opened, added to, cut or closed a position (moves of 5% or less count as held);
  - net funds buying, total holders, and the change in shares held;
  - what your Watchlist managers did.

  The data comes from the SEC's quarterly Form 13F data sets: run **Funds** on the Jobs tab, or
  `python -m insider_tracker funds`. That downloads the two newest data sets (about 200 MB, kept in
  `data/13f_sets/`) and stores only a per-stock summary. Only managers who filed both quarters are
  compared. 13F positions are quarter-end and published months later, so this shows what funds
  were doing *before* the insiders bought.
- **Tone** - tune that scoring:
  - label real headlines good, neutral or bad, and see how often the scores agree with you,
    with bad news counted on its own;
  - edit the word weights, phrase rules and cut-offs, **Preview** the effect, then **Save**;
  - each headline shows the words that drove its score, and a panel lists the most frequent
    scored words so you can spot names VADER misreads.

  Settings go in `tone.toml` and labels in `tone_labels.json`, both kept out of git.

  Large purchases has the same **stake_change** for each filing.
- **Raw tables** - `insider_tx`, `congress_tx`, `holdings_13f` and `processed`
  with per-column filters (plain text matches as a substring; `>1000000` or
  `=P` compare). These tables are wide, so the columns worth reading are shown
  by default with the rest one click away; a filter on a hidden column still applies.
- **SQL** - a read-only console for anything the other two can't express.
- **Watchlist** - view and edit `watchlist.json` (which tickers Form 4 filters
  to, which managers 13F tracks) without hand-editing a file. Tickers are
  checked against SEC's own ticker list on save, but always saved regardless.
- **Jobs** - trigger `form4`, `house`, `senate`, `13f`, or `update` without
  leaving the browser. Runs on a background thread, same as the CLI, so a long
  Form 4 backfill doesn't block the page; the tab auto-refreshes while
  something is running. Only one instance of a given source runs at a time.
  Update runs Form 4 for all issuers (it doesn't use the watchlist), then House,
  Senate and 13F, and each source's own row shows its progress as Update reaches
  it. Update and the individual sources refuse to start while the other is running.
- **Copy PNG** (top right of the nav bar, on every tab with a table) puts the table on your
  clipboard as an image, exactly as shown -- your sort, filters, wrapping and light/dark theme --
  ready to paste into chat or a document. Pages with two tables copy both. Very long tables
  are cut at roughly 800 rows with a note saying how many were left out.
- **Price charts** - every ticker in every table is a link to its chart: daily closes with insider
  open-market buys (triangles, at the price paid), congressional purchases and sales (dots, on the
  trade date), and a hatched band over each cluster's first-to-last buy. Ranges 3M-5Y. Prices come
  from Yahoo Finance's unofficial chart endpoint, fetched the first time you open a ticker and cached
  in the database; Copy PNG includes the chart. The **back arrow** on a chart returns to the
  exact view you clicked from -- its filters, sort and scroll position -- and keeps doing so
  while you switch ranges. Opened cold (new tab, bookmark), it goes to Signals. A ticker Yahoo doesn't know (bonds, foreign lines,
  delisted names) says so rather than failing. Above the chart is a **company description**: the
  business summary, sector, industry, headquarters, employees and website (Yahoo Finance), cached
  for 30 days; where Yahoo has nothing, just the issuer's industry category from the SEC.
- **Architecture** - a diagram of how data flows from each source through to this
  screen, a one-line responsibility for every module, and the four-table schema
  with live row counts from your own database.

Everything except Watchlist and Jobs is strictly read-only -- every other
connection this UI opens runs `PRAGMA query_only`. Watchlist writes
`watchlist.json` (never the database); Jobs is the only thing here that starts
outbound network requests.

Every column heading is clickable to sort -- numbers descend on the first click,
text ascends, and blanks always sink to the bottom rather than counting as the
largest value. Long free-text columns reflow instead of pushing the table
sideways, so the layout adapts to your window rather than demanding a side scroll.

Every tab, control and column heading explains itself on hover -- what it is,
what it means, and when to use it. That doubles as the data dictionary: hover
`plan_10b5_1` or `acq_disp` on the Raw tables tab rather than going to the
schema. Empty tables still show their headings, so you can learn a source
before you ingest it. A test fails if any column is left undocumented.

State lives in the URL, so any screen can be bookmarked or pasted to someone
else. The server binds to loopback only and refuses any other host: the SQL
console is read-only several ways over (`PRAGMA query_only`, a SELECT-only
check, one statement per query, a 5s abort and a 1,000-row cap), but a
database console on a routable port is still a bad idea.

Cron, weekdays at 7pm Pacific (after EDGAR's daily index is published):

```cron
0 19 * * 1-5 cd /path/to/insider-tracker && SEC_USER_AGENT="Name you@example.com" python -m insider_tracker update >> data/cron.log 2>&1
```

## Discord alerts

Posts Form 4 insider purchases that pass your rules to a Discord channel, one post per alert,
each linking the filing on sec.gov. It's meant to run unattended once each weekday morning on a
server. [DEPLOY.md](DEPLOY.md) covers the setup: a systemd timer, the settings file, how to tune
the rules, security and legal notes.

```bash
cp alerts.example.toml alerts.toml                  # the rules: every setting explained inside
python -m insider_tracker alerts --backtest 20      # what they would have posted, day by day
python -m insider_tracker alerts --dry-run          # what would post right now
python -m insider_tracker alerts --ingest-days 4    # pull new filings, then post (needs DISCORD_WEBHOOK_URL)
```

- **Two rule types.** `cluster_buy` fires when several different insiders at one company buy
  within a window. `large_buy` fires on one officer's or director's big purchase. Either can be
  limited to tickers, roles and dollar amounts, with a cooldown so a daily buyer is one post,
  not five.
- **What never posts:**
  - sales, grants, option exercises and 10b5-1 plan trades;
  - Form 4/A amendments that restate a trade already posted;
  - filings with implausible numbers: a total over $1B, or a price far from what the same
    company's other insiders paid that month, which catches per-share typos;
  - on a first run, anything filed more than 4 days ago.
- **When something breaks** (a failed download, a rejected post, or no new filings for over 3
  weekdays), the job says so in Discord rather than going quiet.
- **Safe to point at a public channel:** @mentions are disabled and markdown escaped in every
  post. The webhook URL never appears in logs or errors.

## How each source works

| Source | Mechanism | Latency | Reliability |
|---|---|---|---|
| Form 4 | EDGAR daily `master.idx` → full-submission `.txt` → `ownershipDocument` XML | ~2 business days after trade | High. Structured XML, stable for years |
| 13F | `data.sec.gov` submissions API → `index.json` → info table XML | Up to 45 days after quarter end | High |
| House PTRs | Clerk's yearly `{year}FD.zip` index → PDF per filing → pdfplumber | Up to 45 days (often later) | Medium. PDF parsing; scanned paper filings flagged for manual review |
| Senate PTRs | efdsearch.senate.gov (agreement form + CSRF) → HTML tables | Up to 45 days | Medium. Site changes break it occasionally; paper filings flagged |

Every document is recorded in the `processed` table, so runs are incremental
and failures retry automatically next run.

## What the report surfaces

1. **Insider cluster buys**: 2+ insiders at one company making open-market
   purchases (code `P`) outside 10b5-1 plans. This is the strongest signal here.
2. **Large officer/director purchases** above $250K.
3. **Congress/insider overlap**: tickers bought by both groups in the window.
4. **Most-traded tickers in Congress**, recent trades, and **late filings**
   (trade-to-filing gap > 45 days, the STOCK Act deadline).
5. **Executive sales outside 10b5-1 plans** over $1M (weak signal).
6. **13F changes**: new, exited, and ±25% positions quarter over quarter.

Thresholds live in `insider_tracker/config.py`.

## Known limitations

- **Unfiltered Form 4 backfill is slow.** The SEC rate limit is 10 req/s and
  a busy day has on the order of a thousand filings. For bulk history, the
  SEC's quarterly "Insider Transactions Data Sets" (Form 3/4/5 flattened
  to TSV) would be a faster loader to add.
- **Form 4/A amendments** are stored alongside originals; signal queries
  dedupe on the economic fields, which handles restatements that don't change
  the numbers.
- **10b5-1 detection** uses the filing-level checkbox (April 2023 onward)
  plus a footnote text search for older filings. Some plan trades will slip through.
- **Form 5 and Form 144** (proposed sales) aren't ingested yet.
- **13F-HR/A amendments** are skipped. Tickers come from an OpenFIGI lookup of each CUSIP,
  then a match of the issuer name against the SEC's ticker list for what it misses (both
  cached). Bonds, funds, and companies no longer listed stay blank.
- **Ticker coverage varies by source.** `parse_quality` says how a row got its
  symbol: `ok` means the filing stated it, `name_ticker` means it was recovered
  from the asset text (the Senate leaves the ticker column empty for most ADRs),
  and `no_ticker` means there is no symbol -- Treasuries, municipal bonds and
  private companies. Check the `no_ticker` rows before trusting ticker-level totals.
- **Congressional amounts are ranges**; reports sum the lower bound as a floor.
- **Legislation may change this data.** The House passed H.R. 7008 (Stop Insider
  Trading Act) in July 2026, which would bar members from buying individual stocks
  and require public notice 7–14 days before sales. If it becomes law, those
  pre-sale notices would be a new source worth adding.

## License, and the data's own terms

The code is MIT-licensed; see [LICENSE](LICENSE). The data it collects comes with its own rules,
which apply to you when you run it:

- **SEC EDGAR:**
  - Set `SEC_USER_AGENT` to your own name and email. The SEC requires that header and uses it to
    contact, or block, whoever runs a tool.
  - Keep the request spacing in `config.py` within its fair-access limit of 10 requests a second.
- **House and Senate disclosures:** 5 U.S.C. §13107 makes it unlawful to use these reports for
  commercial purposes (news media excepted), for credit ratings, or to solicit money.
- **Yahoo Finance:** the price charts and company descriptions come from unofficial endpoints,
  for personal use. Yahoo's terms don't allow redistributing the data. That's why the Discord bot
  posts no charts and the test fixtures for it are invented.

Not investment advice.
