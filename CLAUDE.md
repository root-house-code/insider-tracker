# insider-tracker

Python CLI that scrapes and reports on stock trades by corporate insiders
(SEC Form 4), institutional managers (SEC 13F), and members of Congress
(House/Senate STOCK Act Periodic Transaction Reports). Storage is a single
SQLite file; output is a Markdown report + CSVs per section.

## Commands
- Tests (offline, fixture-based): `python tests/test_parsers.py` or `python -m pytest tests/`
- Run: `python -m insider_tracker {form4,house,senate,13f,update,report,ui,alerts} --help`
- Requires env var `SEC_USER_AGENT="Name email@example.com"` for any network command.
- Python >= 3.11 (the Discord job targets Debian 12 / Raspberry Pi OS servers). That means `tomllib`
  is fine, but there are no PEP 701 f-strings. Inside `{...}` there can be no backslashes and no
  string in the f-string's own quote, and a one-line f-string's expression can't break across lines.
  Hoist the value to a variable or constant instead (see `ui.ON`). `test_source_stays_python_311_compatible`
  enforces this from any 3.12+ interpreter.

## Layout
- `insider_tracker/net.py` rate-limited session (SEC max 10 req/s; we use 0.12s spacing; congress sites 1s)
- `edgar_form4.py` daily master.idx -> full-submission .txt -> ownershipDocument XML -> `insider_tx`
- `edgar_13f.py` data.sec.gov submissions API -> index.json -> info table XML -> `holdings_13f`
- `house.py` Clerk yearly {year}FD.zip -> PTR PDFs -> pdfplumber text -> anchor-regex parser
- `senate.py` efdsearch.senate.gov: accept agreement (CSRF) -> POST /search/report/data/ -> HTML tables
- `signals.py` SQL for report sections; `report.py` Markdown/CSV rendering; `cli.py` argparse
- `ui.py` localhost web UI over the same signals: stdlib http.server, server-rendered
  HTML, state in the query string. View thresholds travel under a `p_` prefix so they
  can't collide with the global `days` window. Read-only, loopback-only by construction.
  All hover help lives in the GLOSSARY block at the top of `ui.py`; rendered as `title`
  attributes (so it survives JS being off) and upgraded by `TOOLTIP_JS`. Adding a column
  to a view or table means adding it to `COLUMN_HELP` -- `test_ui_tooltip_coverage` fails
  otherwise. `NAV_TABS` is the single source of truth for which tabs exist; `_page()`
  and that test both read it, so a tab can't be added to one and forgotten in the other.
  The Architecture tab (`DIAGRAM`, `MODULES`, `DESIGN_NOTES`) is a hand-built ASCII
  diagram, not generated from the code -- update it by hand if the pipeline changes,
  and rerun `test_ui_diagram_columns_align`, which checks its column math structurally.
  Every other tab is strictly read-only; Watchlist (writes `watchlist.json`) and Jobs
  (starts an ingest) are the two named exceptions, both via `do_POST` with a 303
  redirect back to a GET so a page refresh can't resubmit the form.
  The nav bar's Copy PNG button (`COPY_PNG_JS`) is client-side: it asks the BROWSER where every
  word of every `<table>` sits (Range rects) and redraws them on a canvas -- don't re-derive line
  wrapping yourself, that drifted visibly from the page. No library (html2canvas would be a CDN
  dependency). Needs a secure context (`localhost`/`127.0.0.1` count) and a browser with
  `ClipboardItem`. The Python tests can only check the button and `node --check` the JS; to verify
  drawing, load a real page in headless Edge/Chrome with a stubbed `navigator.clipboard` and
  screenshot the PNG it receives (headless needs Start-Process redirection on Windows, and its
  virtual clock won't decode images -- show the blob in an `<img>` instead).
- `prices.py` + `chart.py` ticker click -> `/chart`. Yahoo's chart endpoint is unofficial and keyless: expect
  it to break someday, so every failure degrades to "no chart" (`price_meta.status` ok/none/error with
  retry windows) and `tests/fixtures/yahoo_chart_example.json` is the response structure the parser must
  keep honouring. Bars are keyed by Yahoo symbol, not our ticker string (`prices.candidates` maps `BRK.B`,
  `LEN/B`, `HEI, HEI.A`, `N O G` to symbols). `/chart` is the one GET that writes (it tops up the cache on
  its own connection via `safe_ensure`; the page's own connection stays read-only). Yahoo `close` is
  split-adjusted, so a reported trade price far from that day's close is drawn at the close and says so
  -- this also absorbs bad filings (an SLBT row put $2.27M in the price field). The chart is inline SVG
  built from CSS variables; Copy PNG swaps in real colours before rasterising it.
  `profiles.py` (company description, `profiles` table keyed by the clicked ticker string): Yahoo
  quoteSummary needs a cookie (`fc.yahoo.com`, answers 404 on purpose) then a crumb, both per fetch --
  expect that handshake to be the first thing Yahoo changes. SEC `submissions` (CIK from `insider_tx`) is
  the fallback and only ever has an industry category; an SEC-sourced profile is re-tried against Yahoo
  daily, a Yahoo one lives 30 days. `website` comes from a third party, so `_about_html` only links
  http(s) and escapes everything. Fixtures: `tests/fixtures/sec_submissions_len.json` (real) and
  `yahoo_profile_example.json`.
  - **Yahoo fixtures are invented on purpose.** The repo is public and Yahoo's terms forbid
    redistributing its data. Both `yahoo_*_example.json` files copy a real capture's exact
    structure (keys, types, list lengths, real bar timestamps) with a fictional company, "Example
    Homes Corporation" (EXH), and made-up numbers.
  - **Real captures stay local**, in git-ignored `tests/fixtures/local/`. If Yahoo changes format,
    capture a fresh response there and rebuild the example file to the same structure. Never
    commit a real Yahoo response.
  The chart's back arrow (`BACK_JS`, `_safe_back`, `referer_path`): `history.back()` when this tab got here
  from one of our pages (keeps scroll and form state, which a link can't), else a plain link built from
  `?back=` / the Referer. Range tabs `location.replace` so they never add history. `?back=` is only ever
  a relative same-site path -- it's rendered into an href, so `//host` and absolute URLs are rejected.
  History/scroll behaviour can't be unit-tested; drive headless Edge over the DevTools protocol (needs
  `--remote-allow-origins=*` and a throwaway `websocket-client`, which should not be left installed).
- `jobs.py` in-memory job runner behind the Jobs tab: one background thread per
  source, `_jobs` dict guarded by `_lock`. `start()` flips status to `running`
  *synchronously* before spawning the thread -- that's what makes the one-job-per-source
  lock correct, but it also means status can read `running` slightly before the
  runner itself has been entered (it still has to open its own `db.connect()` first).
  A test that waits on the status flip rather than on the runner actually being
  called will race; wait on an effect of the runner, not the status dict, if you
  add one. No job state is persisted -- a process restart forgets everything mid-run.
  Update owns the rows of the sources it runs (`UPDATE_SOURCES`): `start("update")`
  marks them `queued`, `_run_update` moves each to running/ok/skipped/error, and
  `_run`'s finally releases any still queued if Update dies early. `start()` treats
  `queued` as busy and makes Update and the individual sources mutually exclusive.
  The UI's Update runs Form 4 with all issuers; the CLI's `update` still uses the
  watchlist unless given `--all`.
- `alerts.py` + `alerts.example.toml` + `deploy/` + `DEPLOY.md`: the unattended Discord job
  (`alerts --ingest-days 4` from a systemd timer, Tue-Sat 06:00 America/New_York). It doesn't use
  `signals.py`: it reads `insider_tx` with its own query (P, non-derivative, non-plan, ticker
  present). It never runs from the UI.
  - **Identity.** A trade is `fingerprint()` = ticker|insider|trans_date|shares|price, so a 4/A
    restatement matches its original. The insider is `_who()`, the first CIK on the filing, so
    joint filings count once.
  - **Memory.** `alert_log` (rule, fp) records what was handled, posted or held back.
    `alert_groups` (rule, grp) records when a group last posted, for `cooldown_days`. The group
    is the issuer for clusters, issuer|insider for large_buy. Both are keyed by rule *name*, so
    renaming a rule resets its memory. A trade is recorded only after Discord accepts the post,
    so failures retry next run.
  - **Freshness.** A trade is eligible only if filed within `max_age_days`, so a first run or a
    long gap can't dump history. `follow_up` ("(update)", 🆕 on new insiders) means this rule
    already posted part of the cluster. It doesn't mean "some members are old", which wrongly
    labelled first posts.
  - **Implausible filings, both from real data.** `max_filing_value` caps a filing's total (SLBT
    reported "$10T"). `_price_outliers` drops a filing whose price is outside 0.2-5x the median
    of the same issuer's other P/S trades within 45 days, given at least 2 peers (UUU reported
    $5,134/share against ~$5).
  - **Discord safety.** Every payload sets `allowed_mentions: {"parse": []}` and `md()`-escapes
    filer text. The webhook URL is a secret, and `requests` exceptions embed it, so `post()`
    reports only the exception type. Keep it that way.
  - **Stale feed.** `edgar_form4.list_form4_filings` treats a 403/404 index as "not published",
    which is also how the SEC answers a blocked client. `stale_feed()` warns the admin webhook
    after more than 3 quiet weekdays instead.
  - **Testing.** Tests use `_FakeDiscord` (fake HTTP) and `_tmpdb()`. For an end-to-end check,
    run the real CLI against a local `http.server` posing as the webhook. Nobody has booted the
    systemd units yet: on a real box, run `systemd-analyze verify` and
    `systemd-analyze calendar` (DEPLOY.md step 8). `deploy/run.sh` is always invoked through
    `sh` (a copy from Windows loses the exec bit), strips CRs from `.env`, and `.gitattributes`
    pins `deploy/**` to LF.
- `funds.py` the Fund / insider overlap view's data: the SEC's quarterly Form 13F data sets (one zip per
  three months of filing dates, so roughly one quarter; ~100 MB, INFOTABLE ~400 MB unzipped). Positions
  are staged in a throwaway SQLite file and only the per-CUSIP summary (`fund_flows`) and watchlist
  managers' rows (`fund_tracked`) are kept.
  - **Comparison rules.** Only managers who filed both quarters are compared. The quarter is read from
    the data, not the file name (zips also hold late filings for old periods). The latest
    original-or-restatement filing is the position, and NEW HOLDINGS amendments add to it. SH only:
    no PRN or options. A change of 5% or less counts as held.
  - **Issuer names** are filer-typed, so the most common spelling per CUSIP is used ("QNITY
    ELECTRONICS" was typed against H.B. Fuller's CUSIP).
  - **Tickers.** The cusip_ticker cache first, then SEC-list name matching. Then
    `fill_insider_tickers` takes stocks insiders bought that are still unmatched, compares their Form 4
    name with 13F names, and confirms with OpenFIGI. It accepts an unconfirmed match only when
    OpenFIGI knows nothing and exactly one CUSIP has that name. 72% of insider-bought tickers match;
    the rest are mostly funds, fresh IPOs and OTC micro-caps.
- `form345.py` loads history from the SEC Insider Transactions Data Sets: one zip per filing
  quarter, about 10 MB, purchases only. Rows land in `insider_tx` in the live parser's shape.
  - **Overlap with the live ingest:** accessions already present are skipped, so there are no
    duplicates.
  - **Prices are rounded to the cent** in the data sets (the live parser keeps 4 decimals), which is
    why `lockstep` compares to the cent.
  - **Dates:** about 1% of rows had impossible or 2+-years-late trade dates and are dropped
    (`_plausible`). Older files lack AFF10B5ONE, so use `.get` for optional columns.
- `signals.buyer_type` adapts Cohen, Malloy and Pomorski (2012) to purchases: routine (same
  month in each of the 3 prior years), first buy, occasional, or unknown (data too short). Each
  insider is judged per issuer by first CIK. A cluster where every buyer is routine gets the
  "routine" noise flag.
- Exploration (2026-10-24, about 10,000 priced clusters at 2+ insiders). Of 34 entry-time traits,
  only a few held in both 2015-2020 and 2021+:
  - **CFO among the buyers:** about +2% average excess over IWM at 60/120 days, in both periods.
    That's the CFO preset, and `backtest.TRACK_FROM` marks where its out-of-sample record starts.
  - **REITs:** worse in both periods, so they get the "reit" noise flag.
  - **Penny stocks looked best,** but they're 45% unpriced (delisted), so don't trust that.
  - Re-test before adding any new screen, and judge it only on clusters after it was chosen.
- `earnings.py` reads earnings report dates from 8-Ks with Item 2.02, including the older
  "files" pages, back to 2014, and stores them in the `earnings` table. `days_since_results`
  measures from the last report strictly before the first buy, and needs 4 or more reports (fewer
  usually means the company doesn't file 2.02). `quarter_timing` buckets the result.
  - CFO + 1-2 months after results: 278 clusters, avg +3.7% / median +0.9% vs IWM, 54% beat.
    It's tracked out of sample from TRACK_FROM.
  - The backtest group tables show a trimmed average and a median. They differ a lot (skew), so
    always quote both.
- `backtest.py` at history scale (about 17,000 clusters since 2015, about 5,000 tickers).
  `refresh()` (the Backtest job) fetches each ticker's Yahoo history once and stores result rows as
  JSON in `bt_events`, with `final=1` once ret_120d exists (or no prices after a year). The view
  merges those with `live_events` for the last 200 days.
  - **No hindsight:** levels 2/3/4 are dated separately. Factors use only trades filed by the signal
    date. Size is dollar volume at entry; today's market cap would be look-ahead.
  - **Reused tickers:** a price history starting more than 10 days after the signal counts as "no
    prices" (often a recycled symbol).
  - Tables over 1,000 rows show the first 1,000 (`MAX_TABLE_ROWS`); the CSV has everything.
- Noise filters (`issuers.py`, `signals.lockstep`). In the October 2026 data, 25 of 67 clusters
  were noise: 11 fund issuers and 16 lockstep groups, which were employee purchase plans,
  dividend-reinvestment plans, discounted placements and IPO allocations.
  - **Fund vs company.** A fund has no SIC code and files N-CSR/N-2. A company has a SIC and files
    10-K/20-F. Name rules would misfile REITs ("Trust"). BDCs (no SIC but 10-K filers) count as
    companies, deliberately.
  - **Lockstep** means 2+ insiders, one price, one day. It can catch two directors who genuinely
    bought together, so it's a hide-by-default flag, never a delete.
  - **Tests** opt out with hide_noise=0 / skip_lockstep=False, because `_trade` defaults give
    everyone $100 on the same day.
- `db.py` schema; `processed` table makes every source incremental (errors retry next run)
- `cusip_map.py` 13F gives only CUSIPs. Two sources, labelled in `cusip_ticker.source`: OpenFIGI by
  CUSIP (`figi`; free, 25 req/min x 10, but its US coverage has holes -- real listed companies like
  Exxon come back empty), then the issuer NAME matched against the SEC's `company_tickers.json`
  (`sec_name`; 99.7% agreement with OpenFIGI on CUSIPs both could answer, but only ~59% coverage).
  The name match refuses rather than guesses: bonds (`share_type='PRN'`), fund/trust names,
  ambiguous names (two tickers) and names that normalise to nothing stay blank (`none`).
  Failed requests are never cached; `none` is, so nothing is retried forever. Runs at the end of
  every `edgar_13f.ingest`; never raises. `cusip_ticker` isn't a browsable table (query it in SQL).
  Known gaps: SEC's list lacks taken-private/acquired companies and some renamed ones (it calls
  Exxon "ExxonMobil Holdings"), ~100 held positions are bonds, and share classes arrive as `LEN/B`
  not `LEN.B`. When changing the matcher, re-measure agreement against the `figi` rows first.

## Conventions and gotchas
- Dates stored as ISO `YYYY-MM-DD` strings.
- Insider signals use only open-market codes `P`/`S`, non-derivative, `plan_10b5_1=0`.
  `SELECT DISTINCT` on economic fields dedupes Form 4/A amendments; keep that when adding queries.
- 13F `<value>` is dollars for filings on/after 2023-01-03, thousands before.
- 13F `put_call` uses '' not NULL (it's in the primary key).
- House PDF parsing is best-effort. `parse_quality` is `ok` (ticker stated),
  `name_ticker` (recovered from the asset text, Senate ADRs mostly) or `no_ticker`
  (Treasuries, munis, private companies). Scanned/paper filings are `needs_manual`.
- Clerk PDFs pad the row-terminating labels with NUL bytes, so a marker reads
  `F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00: New`. `house.CONTROL` strips control characters before
  parsing; without it `STOP` never fires and the marker text lands in the asset
  name. It only ever corrupted TICKERLESS rows -- a tickered row gets sliced at
  the ticker anyway -- which is what made the first regression test vacuous.
- Senate eFD leaves the Ticker column as `--` for many securities while naming the
  symbol in the asset text; `senate.ticker_from_name` recovers those. Roughly a
  third of Senate rows depend on it.
- Senate site is the most fragile piece; breakage is usually form field names in `search_ptrs`.
- EDGAR serves documents as `text/plain` with no charset, so `requests` guesses
  ISO-8859-1 and mangles UTF-8. `net._prefer_utf8` re-decodes textual bodies as
  UTF-8 when they decode cleanly; don't bypass `PoliteSession` for EDGAR reads.
  `report.py` pins its `.md`/`.csv` writes to UTF-8 for the same reason -- the
  locale default (cp1252 on Windows) raises on any name outside that codepage.
- Form 4, House and Senate are validated against live data and have real filings
  in `tests/fixtures/` with `MANIFEST.json` recording the source URL. 13F is still
  fixture-only: run it live and fix mismatches, adding real samples the same way.
- A fixture only earns its place if the test fails when the fix is reverted. Check
  that explicitly when adding one.
- Known-loose, not yet a problem in practice: `ingest()` filters the watchlist on
  every CIK on the master.idx line, which includes reporting owners, so a
  watchlist company appearing as an *owner* on someone else's Form 4 would be
  ingested. No occurrences in the validated window.

## Roadmap
1. Validate against live data (Form 4, House, Senate done; 13F remains).
2. Bulk backfill from SEC quarterly "Insider Transactions Data Sets" (Form 3/4/5 TSVs).
3. Price data to measure post-signal returns (e.g. 30/90-day forward returns for cluster buys).
4. Form 144 (proposed sales), Form 5.
5. If H.R. 7008 becomes law: ingest congressional pre-sale notices.
