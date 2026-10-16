"""Offline tests for the parsers using fixtures shaped like real filings.
Run: python -m pytest tests/  (or python tests/test_parsers.py)"""
import datetime as dt
import json
import re
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from insider_tracker import alerts, chart, config, cusip_map, db, edgar_13f, edgar_form4, house, jobs, net, prices, profiles, report, senate, signals, ui  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
LDQUO, RDQUO = chr(0x201C), chr(0x201D)   # the curly quotes in UUU's footnote

FORM4 = """<ownershipDocument>
<schemaVersion>X0508</schemaVersion><documentType>4</documentType>
<periodOfReport>2026-09-18</periodOfReport>
<aff10b5One>0</aff10b5One>
<issuer><issuerCik>0000320193</issuerCik><issuerName>Acme Corp</issuerName>
<issuerTradingSymbol>acme</issuerTradingSymbol></issuer>
<reportingOwner>
 <reportingOwnerId><rptOwnerCik>0001111111</rptOwnerCik><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
 <reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>1</isOfficer>
  <officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship>
</reportingOwner>
<nonDerivativeTable>
 <nonDerivativeTransaction>
  <securityTitle><value>Common Stock</value></securityTitle>
  <transactionDate><value>2026-09-18</value></transactionDate>
  <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>P</transactionCode></transactionCoding>
  <transactionAmounts><transactionShares><value>10,000</value></transactionShares>
   <transactionPricePerShare><value>42.50</value></transactionPricePerShare>
   <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
  <postTransactionAmounts><sharesOwnedFollowingTransaction><value>250000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
  <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
 </nonDerivativeTransaction>
 <nonDerivativeTransaction>
  <securityTitle><value>Common Stock</value></securityTitle>
  <transactionDate><value>2026-09-19</value></transactionDate>
  <transactionCoding><transactionCode>S</transactionCode><footnoteId id="F1"/></transactionCoding>
  <transactionAmounts><transactionShares><value>500</value></transactionShares>
   <transactionPricePerShare><value>43</value></transactionPricePerShare>
   <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
  <postTransactionAmounts><sharesOwnedFollowingTransaction><value>249500</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
  <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
 </nonDerivativeTransaction>
</nonDerivativeTable>
<footnotes><footnote id="F1">Sold pursuant to a Rule 10b5-1 trading plan adopted May 1, 2026.</footnote></footnotes>
</ownershipDocument>"""

HOUSE_TEXT = """Filing ID #20031234
Name: Hon. John Q. Public
ID Owner Asset Transaction Type Date Notification Date Amount Cap. Gains > $200?
SP Acme Corp - Common Stock P 09/02/2026 09/10/2026 $15,001 -
(ACME) [ST] $50,000
F S: New
S O: Brokerage Account
JT NVIDIA Corporation - Common Stock S (partial) 07/01/2026 09/12/2026 $1,001 - $15,000
(NVDA) [ST]
F S: New
Some Private Fund LP E 08/15/2026 08/20/2026 Over $1,000,000
D: Exchange of fund interests
* For the complete list of asset type abbreviations, please visit ..."""

SENATE_HTML = """<html><body><table class="table">
<thead><tr><th>#</th><th>Transaction Date</th><th>Owner</th><th>Ticker</th><th>Asset Name</th>
<th>Asset Type</th><th>Type</th><th>Amount</th><th>Comment</th></tr></thead>
<tbody>
<tr><td>1</td><td>09/03/2026</td><td>Spouse</td><td><a href="#">ACME</a></td><td>Acme Corp</td>
<td>Stock</td><td>Purchase</td><td>$50,001 - $100,000</td><td>--</td></tr>
<tr><td>2</td><td>09/04/2026</td><td>Self</td><td>--</td><td>US Treasury Bill</td>
<td>Other Securities</td><td>Sale (Full)</td><td>$100,001 - $250,000</td><td>--</td></tr>
</tbody></table></body></html>"""

INFOTABLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
<infoTable><nameOfIssuer>ACME CORP</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>000000AA1</cusip>
<value>1000000</value><shrsOrPrnAmt><sshPrnamt>20000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
<investmentDiscretion>SOLE</investmentDiscretion></infoTable>
<infoTable><nameOfIssuer>ACME CORP</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>000000AA1</cusip>
<value>500000</value><shrsOrPrnAmt><sshPrnamt>10000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
<investmentDiscretion>DFND</investmentDiscretion></infoTable>
<infoTable><nameOfIssuer>BETA INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>000000BB2</cusip>
<value>300</value><shrsOrPrnAmt><sshPrnamt>5000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
<putCall>Put</putCall></infoTable>
</informationTable>"""


def test_form4():
    rows = edgar_form4.parse_form4(FORM4, "0001234567-26-000001", "4", "2026-09-20")
    assert len(rows) == 2
    buy, sale = rows
    assert buy["ticker"] == "ACME" and buy["code"] == "P" and buy["value"] == 425000.0
    assert buy["relationship"] == "Director, Chief Executive Officer"
    assert buy["plan_10b5_1"] == 0
    assert sale["plan_10b5_1"] == 1, "10b5-1 footnote should flag the sale"
    return rows


def test_house():
    rows = house.parse_ptr_text(HOUSE_TEXT)
    assert len(rows) == 3, rows
    a, b, c = rows
    assert (a["owner"], a["ticker"], a["tx_type"], a["amount_low"], a["amount_high"]) == \
        ("Spouse", "ACME", "P", 15001, 50000), a
    assert (b["owner"], b["ticker"], b["tx_type"], b["trans_date"]) == ("Joint", "NVDA", "S", "2026-07-01"), b
    assert c["ticker"] == "" and c["parse_quality"] == "no_ticker" and c["amount_low"] == 1_000_000
    return rows


def test_senate():
    rows = senate.parse_ptr_html(SENATE_HTML)
    assert rows[0]["ticker"] == "ACME" and rows[0]["tx_type"] == "P" and rows[0]["owner"] == "Spouse"
    assert rows[1]["ticker"] == "" and rows[1]["tx_type"] == "S"
    return rows


def test_13f():
    rows = edgar_13f.parse_infotable(INFOTABLE, values_in_thousands=False)
    acme = next(r for r in rows if r["cusip"] == "000000AA1")
    assert acme["shares"] == 30000 and acme["value_usd"] == 1_500_000
    old = edgar_13f.parse_infotable(INFOTABLE, values_in_thousands=True)
    assert next(r for r in old if r["cusip"] == "000000BB2")["value_usd"] == 300_000
    return rows


def test_pipeline():
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        f4 = test_form4()
        # second insider buying the same ticker -> cluster
        f4b = [dict(r, accession="0001234567-26-000002", owner_name="Roe Rick", owner_cik="2")
               for r in f4 if r["code"] == "P"]
        db.insert_rows(conn, "insider_tx", f4 + f4b)
        # amendment duplicate should not double count
        db.insert_rows(conn, "insider_tx", [dict(f4[0], accession="0001234567-26-000003", form_type="4/A")])
        cong = [{**r, "chamber": "House", "doc_id": "20031234", "seq": i, "member": "Hon. John Q. Public",
                 "state_district": "XX01", "filed_date": "2026-09-12", "url": "u"}
                for i, r in enumerate(test_house())]
        db.insert_rows(conn, "congress_tx", cong)
        for acc, period, filed, scale in (("a1", "2026-03-31", "2026-05-14", 1), ("a2", "2026-06-30", "2026-08-14", 2)):
            hs = edgar_13f.parse_infotable(INFOTABLE, False)
            db.insert_rows(conn, "holdings_13f", [{**h, "shares": h["shares"] * scale, "accession": acc,
                           "manager_cik": "1", "manager_name": "Test Capital", "period": period,
                           "filed_date": filed} for h in hs])
        conn.commit()
        since = (dt.date.today() - dt.timedelta(days=60)).isoformat()
        cb = signals.cluster_buys(conn, since)
        assert cb and cb[0]["ticker"] == "ACME" and cb[0]["insiders"] == 2
        assert cb[0]["total_value"] == 850000.0, "4/A duplicate must not double-count"
        assert signals.overlap(conn, since)[0]["ticker"] == "ACME"
        late = signals.congress_late(conn, since)
        assert late and late[0]["ticker"] == "NVDA" and late[0]["lag_days"] == 73
        assert all(r["change"] == "ADD" for r in signals.thirteenf_changes(conn))
        path = report.build(conn, 60, Path(tmp) / "reports")
        text = path.read_text()
        assert "ACME" in text and "Late congressional" in text
        print(text[:2500])
        conn.close()  # Windows won't delete the temp dir while the db is open


# ---------- real filings captured from live EDGAR ----------
# Full submission .txt files saved verbatim (raw bytes) from www.sec.gov/Archives.
# See fixtures/MANIFEST.json for accession numbers and source URLs.

def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _parse_fixture(name: str):
    """Exercise the same path as ingest(): extract the XML, then parse it."""
    meta = {m["file"]: m for m in json.loads(_fixture("MANIFEST.json").decode())}[name]
    xml = edgar_form4.OWNERSHIP_RE.search(_fixture(name).decode("utf-8")).group(0)
    return edgar_form4.parse_form4(xml, meta["accession"], meta["form_type"], meta["filed_date"])


def test_fixture_real_purchase():
    """GSHD: single open-market buy by a 10% owner; booleans spelled true/false."""
    rows = _parse_fixture("form4_gshd_purchase.txt")
    assert len(rows) == 1
    r = rows[0]
    assert (r["ticker"], r["issuer_name"]) == ("GSHD", "Goosehead Insurance, Inc.")
    assert r["owner_name"] == "Durable Capital Partners LP"
    assert (r["code"], r["acq_disp"], r["is_derivative"]) == ("P", "A", 0)
    assert (r["shares"], r["price"], r["value"]) == (150000.0, 47.32, 7098000.0)
    assert r["trans_date"] == "2026-09-22" and r["shares_after"] == 2720260.0
    # <isTenPercentOwner>true</isTenPercentOwner> must read as a flag, not a string
    assert (r["is_ten_pct"], r["is_director"], r["is_officer"]) == (1, 0, 0)
    assert r["relationship"] == "10% Owner" and r["direct_indirect"] == "I"
    # the only footnote is a beneficial-ownership disclaimer, not a trading plan
    assert r["plan_10b5_1"] == 0
    assert r["url"] == ("https://www.sec.gov/Archives/edgar/data/1726978/"
                        "000119312526401141/0001193125-26-401141-index.htm")


def test_fixture_real_plan_sale():
    """AAPL: officer sale with the filing-level 10b5-1 checkbox set (<aff10b5One>1)."""
    rows = _parse_fixture("form4_aapl_10b5-1_sale.txt")
    assert len(rows) == 1
    r = rows[0]
    assert (r["ticker"], r["issuer_cik"]) == ("AAPL", "320193")
    assert (r["code"], r["acq_disp"]) == ("S", "D")
    assert (r["shares"], r["price"], r["value"]) == (2399.0, 340.06, 815803.94)
    assert r["is_officer"] == 1 and r["relationship"] == "SVP, GC and Government Affairs"
    assert r["plan_10b5_1"] == 1, "filing-level 10b5-1 checkbox must flag the sale"


def _response(body: bytes, content_type: str = "text/plain") -> requests.Response:
    """A response shaped like EDGAR's: text/plain with no charset parameter."""
    r = requests.Response()
    r.status_code = 200
    r._content = body
    r.headers["Content-Type"] = content_type
    r.encoding = requests.utils.get_encoding_from_headers(r.headers)
    return r


def test_fixture_utf8_not_mangled():
    """EDGAR omits the charset, so requests guesses ISO-8859-1 and mangles UTF-8.

    UUU's Form 4 footnote uses curly quotes; decoded as Latin-1 they turn into
    mojibake that lands in issuer/owner names and footnote text in the database.
    """
    raw = _fixture("form4_uuu_utf8.txt")
    assert LDQUO.encode("utf-8") in raw, "fixture must contain a real UTF-8 character"

    naive = _response(raw)
    assert naive.encoding == "ISO-8859-1"
    assert LDQUO not in naive.text, "precondition: the naive decode is wrong"

    fixed = net._prefer_utf8(_response(raw))
    assert fixed.encoding == "utf-8"
    assert LDQUO + "Ault Lending" + RDQUO in fixed.text
    assert not any(0x80 <= ord(c) <= 0x9F for c in fixed.text), "no mojibake control chars"

    rows = _parse_fixture("form4_uuu_utf8.txt")
    assert len(rows) == 1
    r = rows[0]
    assert (r["ticker"], r["code"], r["shares"], r["price"]) == ("UUU", "P", 100.0, 5.01)
    assert r["relationship"] == "Director, 10% Owner"


def test_prefer_utf8_leaves_other_bodies_alone():
    latin1 = ("r" + chr(0xE9) + "sum" + chr(0xE9)).encode("latin-1")   # not valid UTF-8
    assert net._prefer_utf8(_response(latin1)).encoding == "ISO-8859-1"
    declared = _response(b"hi", "text/html; charset=iso-8859-1")
    assert net._prefer_utf8(declared).encoding == "iso-8859-1"
    zipped = _response(bytes([0x50, 0x4B, 3, 4, 0xFF, 0xFE]), "application/zip")
    assert net._prefer_utf8(zipped) is zipped           # binary bodies untouched


def test_ui_tooltip_coverage():
    """Every tab, control and column heading the UI renders must be explained.

    The glossary is the whole point of the hover help; a column that slips in
    undocumented is the failure mode worth catching automatically.
    """
    gaps = []
    for v in ui.VIEWS:
        if not v.tip:
            gaps.append(f"view {v.key}: no tab tooltip")
        for c in v.cols:
            if c not in ui.COLUMN_HELP:
                gaps.append(f"view {v.key}: undocumented column {c}")
        for prm in v.params:
            if not prm.tip:
                gaps.append(f"view {v.key}: undocumented control {prm.name}")
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        for t in ui.RAW_TABLES:
            if t not in ui.TABLE_HELP:
                gaps.append(f"raw table {t}: no tab tooltip")
            for r in conn.execute(f'PRAGMA table_info("{t}")'):
                if r["name"] not in ui.COLUMN_HELP:
                    gaps.append(f"raw {t}: undocumented column {r['name']}")
        conn.close()
    for key, _ in ui.NAV_TABS:
        if key not in ui.NAV_HELP:
            gaps.append(f"nav {key}: no tooltip")
    sep = chr(10) + "  "
    assert not gaps, "undocumented UI elements:" + sep + sep.join(gaps)


def _manifest(name):
    return {m["file"]: m for m in json.loads(_fixture("MANIFEST.json").decode())}[name]


def test_fixture_house_ptr():
    """A real House PTR as pdfplumber extracts it -- NUL bytes and all.

    The Clerk's PDFs pad the row-terminating labels with NUL, so a marker arrives
    as 'F.....S.....: New' with NULs where the dots are. Regex whitespace classes
    do not match NUL, so STOP missed them and the filing-status and subholding
    text was absorbed into the asset name.

    The corruption only ever showed on TICKERLESS rows: when a row has a ticker,
    the parser slices the asset name at the ticker and discards the tail anyway.
    This fixture deliberately carries both kinds, so the test can actually fail.
    """
    raw = _fixture("house_ptr_20035203.txt").decode("utf-8")
    assert chr(0) in raw, "fixture must keep the NUL artifact that caused the bug"

    rows = house.parse_ptr_text(raw)
    assert len(rows) == 5, rows
    assert [r["ticker"] for r in rows] == ["ABT", "", "", "PEP", "DIS"]
    assert all(r["owner"] == "Spouse" for r in rows), "SP owner code"

    abt = rows[0]
    assert abt["asset"] == "Abbott Laboratories Common Stock"
    assert (abt["tx_type"], abt["trans_date"]) == ("S", "2026-07-17")
    assert (abt["amount_low"], abt["amount_high"]) == (1001.0, 15000.0)
    assert abt["asset_type"] == "ST" and abt["parse_quality"] == "ok"

    # the two municipal bonds are the rows the bug corrupted: no ticker to slice
    # at, so every trailing marker used to end up in the asset name
    muni = rows[1]
    assert muni["parse_quality"] == "no_ticker"
    assert muni["asset"] == "California Cmnty Choice Fing & Clean Ener 5% due 10/1/2034%"
    assert (muni["tx_type"], muni["trans_date"]) == ("P", "2025-07-17")
    assert (muni["amount_low"], muni["amount_high"]) == (50001.0, 100000.0), \
        "wrapped amount must be stitched across lines"
    assert rows[2]["asset"] == "Florida St Hsg Fin Corp Rev 3% due 7/1/52"

    for r in rows:
        assert ": New" not in r["asset"] and "Trust ICA" not in r["asset"], \
            "row-terminating marker leaked into the asset name: " + repr(r["asset"])
        assert not any(ord(c) < 32 for c in r["asset"]), "control chars survived parsing"


def test_house_control_chars_do_not_defeat_row_terminators():
    """Without stripping, the NUL-padded marker runs straight into the asset text."""
    # no ticker, so nothing slices the tail off: the markers below must terminate
    # the row on their own, or they become part of the asset name
    padded = ("Some Muni Bond 5% due 2040 [GS] P 09/02/2026 09/10/2026 $1,001 - $15,000\n"
              + "F" + chr(0) * 5 + " S" + chr(0) * 5 + ": New\n"
              + "D" + chr(0) * 9 + ": a long description that must not become the asset name")
    rows = house.parse_ptr_text(padded)
    assert len(rows) == 1
    assert rows[0]["asset"] == "Some Muni Bond 5% due 2040", rows[0]["asset"]
    assert rows[0]["ticker"] == ""
    # the raw marker is invisible to STOP until the control characters are gone
    assert not house.STOP.match("F" + chr(0) * 5 + " S" + chr(0) * 5 + ": New")
    assert house.STOP.match("F S: New")
    assert house.STOP.match("F      S     : New"), "runs of spaces must terminate a row too"


def test_fixture_senate_ptr():
    """A real Senate PTR. eFD leaves Ticker as '--' for ADRs and similar, naming
    the symbol only in the asset text; those are recovered and flagged."""
    rows = senate.parse_ptr_html(_fixture("senate_ptr_b999bc0e.html").decode("utf-8"))
    assert len(rows) == 4, rows
    by_q = {}
    for r in rows:
        by_q.setdefault(r["parse_quality"], []).append(r)
    assert len(by_q["ok"]) == 2 and len(by_q["name_ticker"]) == 2, by_q.keys()

    stated = by_q["ok"][0]
    assert stated["ticker"] == "WMB" and stated["asset_type"] == "Stock Option"
    assert (stated["owner"], stated["tx_type"]) == ("Joint", "P")
    assert (stated["trans_date"], stated["amount_low"]) == ("2026-08-20", 15001.0)

    recovered = {r["ticker"] for r in by_q["name_ticker"]}
    assert recovered == {"EA", "AVB"}, recovered
    # "(The)" sits in the WMB asset name and must never be taken for a symbol
    assert all(r["ticker"] != "THE" for r in rows)


def test_senate_ticker_from_name():
    """The fallback must be greedy about real symbols and shy about everything else."""
    assert senate.ticker_from_name("Electronic Arts Inc. (EA)") == "EA"
    assert senate.ticker_from_name("Zurich Insurance Group Ltd Sponsored ADR (ZURVY)") == "ZURVY"
    assert senate.ticker_from_name("EA - Electronic Arts Inc") == "EA"
    assert senate.ticker_from_name("Berkshire Hathaway (BRK.B)") == "BRK.B"
    # mixed case is a word, not a symbol
    assert senate.ticker_from_name("Williams Companies, Inc. (The) Common Stock") == ""
    assert senate.ticker_from_name("AvalonBay (Exchanged)") == ""
    # structural abbreviations are not symbols either
    assert senate.ticker_from_name("Some Fund (ETF)") == ""
    assert senate.ticker_from_name("Holdings (LLC)") == ""
    # nothing to find
    assert senate.ticker_from_name("US Treasury Bill") == ""
    assert senate.ticker_from_name("") == ""
    # a stated ticker still wins over the name
    rows = senate.parse_ptr_html(_fixture("senate_ptr_b999bc0e.html").decode("utf-8"))
    assert all(r["parse_quality"] != "name_ticker" for r in rows if r["ticker"] == "WMB")


def test_ui_diagram_columns_align():
    """The architecture diagram is hand-built ASCII; a vertical '|' must sit
    directly under the label it points at in every stage, or the picture lies
    about the wiring. Checked structurally rather than by eyeballing it."""
    rows = ui.DIAGRAM.splitlines()

    def col(*labels):
        """Column each label's own center sits at, on its own line."""
        out = []
        for label in labels:
            i = next(n for n, r in enumerate(rows) if label in r)
            start = rows[i].index(label)
            out.append(start + len(label) // 2)
        return out

    # each source's own vertical bar sits under that source's own name
    src_centers = col("SEC EDGAR", "House Clerk", "Senate eFD")
    pipe_row = next(r for r in rows if r.count("|") == 3)
    pipe_cols = [i for i, c in enumerate(pipe_row) if c == "|"]
    for center, pipe in zip(src_centers, pipe_cols):
        assert abs(center - pipe) <= 1, (center, pipe)

    # same check one stage down: each ingest module under its own pipe
    mod_centers = col("edgar_form4.py", "edgar_13f.py", "house.py", "senate.py")
    mod_pipe_row = next(r for r in rows if r.count("|") == 4)
    mod_pipe_cols = [i for i, c in enumerate(mod_pipe_row) if c == "|"]
    for center, pipe in zip(mod_centers, mod_pipe_cols):
        assert abs(center - pipe) <= 1, (center, pipe)

    # the pipeline stages the diagram actually draws boxes for (config.py, cli.py
    # and edgar_common.py are real modules but supporting plumbing, not a stage)
    pictured = {"edgar_form4.py", "edgar_13f.py", "house.py", "senate.py",
                "db.py", "signals.py", "report.py", "alerts.py", "ui.py"}
    documented = {name for name, _ in ui.MODULES}
    assert pictured <= documented, pictured - documented
    for name in pictured:
        assert name in ui.DIAGRAM, f"{name} is pictured in MODULES but missing from the diagram"
    assert "net.PoliteSession" in ui.DIAGRAM


def test_ui_architecture_page_reflects_live_database():
    """The schema table on the Architecture tab must show real counts, not the
    documentation text alone -- otherwise it silently drifts from the database
    the moment someone changes what has been ingested."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        db.insert_rows(conn, "insider_tx", test_form4())
        conn.commit()
        body = ui.page_architecture(conn, {})
        conn.close()
    assert "insider_tx" in body and "congress_tx" in body
    # the two rows test_form4() inserts must show up as a live count, not 0
    i = body.index(">insider_tx<")
    row = body[i:body.index("</tr>", i)]
    assert re.search(r'class="num">2<', row), "row count should reflect what was just inserted"
    # every module and every design-note label from the source data is present
    for name, _ in ui.MODULES:
        assert ui._esc(name) in body
    for label, _ in ui.DESIGN_NOTES:
        assert ui._esc(label) in body


def test_config_save_and_load_watchlist():
    with tempfile.TemporaryDirectory() as tmp:
        old_path = config.WATCHLIST_PATH
        config.WATCHLIST_PATH = Path(tmp) / "watchlist.json"
        try:
            assert config.load_watchlist() == {"tickers": [], "managers_13f": {}}
            data = {"tickers": ["NVDA", "AAPL"], "managers_13f": {"1067983": "Berkshire Hathaway"}}
            config.save_watchlist(data)
            assert config.load_watchlist() == data
        finally:
            config.WATCHLIST_PATH = old_path


def test_ui_watchlist_save_parses_dedupes_and_validates():
    """Tickers dedupe/uppercase/sort; managers parse 'CIK name'; SEC validation
    runs but never blocks the save -- not even when validation itself fails."""
    old_path, old_tfc, old_ua = config.WATCHLIST_PATH, ui.tickers_to_ciks, config.sec_user_agent
    config.sec_user_agent = lambda: "Test test@example.com"  # no real network needed to reach it
    with tempfile.TemporaryDirectory() as tmp:
        config.WATCHLIST_PATH = Path(tmp) / "watchlist.json"
        try:
            ui.tickers_to_ciks = lambda http, tickers: {"NVDA": "1045810"}  # FAKE deliberately unmatched
            detail = ui.save_watchlist_from_form({
                "tickers": ["nvda, nvda\nFAKE"],
                "managers_13f": ["1067983 Berkshire Hathaway\n1336528"],
            })
            saved = config.load_watchlist()
            assert saved["tickers"] == ["FAKE", "NVDA"], "dedupe + uppercase + sort"
            assert saved["managers_13f"] == {"1067983": "Berkshire Hathaway", "1336528": ""}
            assert "2 ticker(s), 2 manager(s)" in detail
            assert "FAKE" in detail, "unmatched ticker should be called out, not silently dropped"

            def boom(http, tickers):
                raise RuntimeError("no network")
            ui.tickers_to_ciks = boom
            detail2 = ui.save_watchlist_from_form({"tickers": ["NVDA"], "managers_13f": [""]})
            assert config.load_watchlist()["tickers"] == ["NVDA"], "must save even if validation crashes"
            assert "Could not validate" in detail2
        finally:
            config.WATCHLIST_PATH, ui.tickers_to_ciks = old_path, old_tfc
            config.sec_user_agent = old_ua


def _wait_job_threads(timeout=5):
    """A job flips its status to done BEFORE its thread closes the database file. On Windows,
    deleting the temp folder in that gap fails ('file in use'), so tests wait for the thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any("(_run)" in t.name for t in threading.enumerate()):
            return
        time.sleep(0.01)


def test_jobs_prevents_concurrent_runs_of_the_same_source():
    """The one concurrency guarantee the Jobs tab depends on: a second click on
    a still-running source must be refused, not queued or double-started."""
    gate = threading.Event()
    calls = []

    def blocking_runner(conn, params):
        calls.append(params)
        gate.wait(timeout=5)
        return "done"

    def wait_until(pred, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            time.sleep(0.02)
        return False

    old_runner = jobs._RUNNERS.get("form4")
    jobs._RUNNERS["form4"] = blocking_runner
    with jobs._lock:
        jobs._jobs.pop("form4", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "t.db"
            assert jobs.start("form4", db_path, days="7") is True
            # status flips to "running" synchronously inside start(), before the
            # background thread has necessarily reached the runner -- wait for
            # the runner itself, the real proof the job is actually executing.
            assert wait_until(lambda: len(calls) == 1)
            assert jobs.status("form4")["status"] == "running"
            assert jobs.any_running()
            assert jobs.start("form4", db_path, days="7") is False, \
                "a second start while running must be refused"
            assert len(calls) == 1, "the refused start must not have run the job again"

            gate.set()
            assert wait_until(lambda: jobs.status("form4")["status"] != "running")
            final = jobs.status("form4")
            assert final["status"] == "ok" and final["message"] == "done"
            assert final["started_at"] and final["finished_at"]
            assert not jobs.any_running()

            # now that it's idle, starting again must be allowed
            gate.clear()
            assert jobs.start("form4", db_path, days="7") is True
            assert wait_until(lambda: len(calls) == 2)
            gate.set()
            wait_until(lambda: jobs.status("form4")["status"] != "running")
            _wait_job_threads()
    finally:
        if old_runner is not None:
            jobs._RUNNERS["form4"] = old_runner
        else:
            jobs._RUNNERS.pop("form4", None)
        with jobs._lock:
            jobs._jobs.pop("form4", None)


def test_jobs_records_errors_without_crashing():
    """Both a runner's own SystemExit (e.g. 'no watchlist configured') and an
    unexpected exception must land as status='error' with a message, not
    propagate and kill the background thread silently."""

    def wait_until(pred, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            time.sleep(0.02)
        return False

    old = dict(jobs._RUNNERS)
    jobs._RUNNERS["13f"] = lambda conn, params: (_ for _ in ()).throw(SystemExit("no managers"))
    jobs._RUNNERS["senate"] = lambda conn, params: (_ for _ in ()).throw(RuntimeError("boom"))
    with jobs._lock:
        jobs._jobs.pop("13f", None)
        jobs._jobs.pop("senate", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "t.db"
            # create the schema first: two jobs racing to create it on a brand-new
            # file can legitimately hit "database is locked" (covered separately below)
            db.connect(db_path).close()
            jobs.start("13f", db_path)
            jobs.start("senate", db_path)
            assert wait_until(lambda: jobs.status("13f")["status"] == "error")
            assert wait_until(lambda: jobs.status("senate")["status"] == "error")
            assert "no managers" in jobs.status("13f")["error"]
            assert "boom" in jobs.status("senate")["error"]
            _wait_job_threads()
    finally:
        jobs._RUNNERS.clear()
        jobs._RUNNERS.update(old)
        with jobs._lock:
            jobs._jobs.pop("13f", None)
            jobs._jobs.pop("senate", None)


def test_jobs_update_reports_each_source_on_its_own_row():
    """Update runs Form 4 for all issuers, and each source's own row moves
    queued -> running -> ok/skipped/error with its own times and result."""
    gates = {s: threading.Event() for s in jobs.UPDATE_SOURCES}
    seen = {}

    def fake(name, outcome):
        def run(conn, params):
            seen[name] = params
            gates[name].wait(timeout=5)
            if outcome == "skip":
                raise SystemExit("nothing to do")
            if outcome == "fail":
                raise RuntimeError("boom")
            return f"{name} done"
        return run

    def wait_until(pred, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            time.sleep(0.02)
        return False

    st = lambda s: jobs.status(s)["status"]
    old = dict(jobs.__dict__)
    jobs._run_form4, jobs._run_house = fake("form4", "ok"), fake("house", "fail")
    jobs._run_senate, jobs._run_13f = fake("senate", "ok"), fake("13f", "skip")
    old_build = jobs.report.build
    jobs.report.build = lambda conn, days, out: "report.md"
    with jobs._lock:
        jobs._jobs.clear()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "t.db"
            db.connect(db_path).close()
            assert jobs.start("update", db_path) is True
            assert all(st(s) in ("queued", "running") for s in jobs.UPDATE_SOURCES)
            assert wait_until(lambda: st("form4") == "running")
            assert jobs.status("form4")["started_at"]
            assert st("house") == "queued" and jobs.status("house")["started_at"] is None
            # no individual source may start while Update owns them, and vice versa
            assert jobs.start("form4", db_path) is False
            assert jobs.start("senate", db_path) is False
            assert jobs.start("update", db_path) is False

            for s in jobs.UPDATE_SOURCES:
                gates[s].set()
            assert wait_until(lambda: st("update") != "running")
            assert seen["form4"].get("all") is True, "Update must run Form 4 for all issuers"
            f4, ho, se, f13 = (jobs.status(s) for s in jobs.UPDATE_SOURCES)
            assert (f4["status"], f4["message"]) == ("ok", "form4 done")
            assert ho["status"] == "error" and "boom" in ho["error"]
            assert se["status"] == "ok", "one source failing must not stop the rest"
            assert (f13["status"], f13["message"]) == ("skipped", "nothing to do")
            assert all(jobs.status(s)["finished_at"] for s in jobs.UPDATE_SOURCES)
            assert st("update") == "ok" and not jobs.any_running()
            assert jobs.start("form4", db_path) is True  # free again once Update is done
            wait_until(lambda: st("form4") != "running")
            _wait_job_threads()
    finally:
        for k in ("_run_form4", "_run_house", "_run_senate", "_run_13f"):
            setattr(jobs, k, old[k])
        jobs.report.build = old_build
        with jobs._lock:
            jobs._jobs.clear()


def test_jobs_update_that_dies_early_releases_queued_rows():
    """If Update can't even open the database, its sources must not stay
    'queued' -- that would block each of them until the server restarts."""
    old_connect = jobs.db.connect
    jobs.db.connect = lambda path=None: (_ for _ in ()).throw(sqlite3.OperationalError("locked"))
    with jobs._lock:
        jobs._jobs.clear()
    try:
        assert jobs.start("update", "x.db") is True
        deadline = time.monotonic() + 5
        while jobs.any_running() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert jobs.status("update")["status"] == "error"
        assert all(jobs.status(s)["status"] == "skipped" for s in jobs.UPDATE_SOURCES)
    finally:
        jobs.db.connect = old_connect
        with jobs._lock:
            jobs._jobs.clear()


def test_jobs_failed_connect_does_not_stick_at_running():
    """If opening the database fails (e.g. 'database is locked' while another job
    writes), the job must end as 'error' and the source must be startable again --
    not read 'running' forever until the server restarts."""
    old_connect = jobs.db.connect

    def locked(path=None):
        raise sqlite3.OperationalError("database is locked")

    jobs.db.connect = locked
    with jobs._lock:
        jobs._jobs.pop("house", None)
    try:
        assert jobs.start("house", "irrelevant.db") is True
        deadline = time.monotonic() + 5
        while jobs.status("house")["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        st = jobs.status("house")
        assert st["status"] == "error" and "locked" in st["error"], st
        assert st["finished_at"]
        jobs.db.connect = old_connect  # a later attempt is not blocked by the failed one
        jobs._RUNNERS_saved = dict(jobs._RUNNERS)
        jobs._RUNNERS["house"] = lambda conn, params: "ok now"
        with tempfile.TemporaryDirectory() as tmp:
            assert jobs.start("house", Path(tmp) / "t.db") is True
            deadline = time.monotonic() + 5
            while jobs.status("house")["status"] == "running" and time.monotonic() < deadline:
                time.sleep(0.02)
            assert jobs.status("house")["message"] == "ok now"
            _wait_job_threads()
    finally:
        jobs.db.connect = old_connect
        if hasattr(jobs, "_RUNNERS_saved"):
            jobs._RUNNERS.clear()
            jobs._RUNNERS.update(jobs._RUNNERS_saved)
            del jobs._RUNNERS_saved
        with jobs._lock:
            jobs._jobs.pop("house", None)


# ---------- Discord alerts ----------

import contextlib  # noqa: E402


@contextlib.contextmanager
def _tmpdb():
    """A throwaway database that is closed even when an assertion fails -- otherwise Windows
    refuses to delete the temp folder and the real failure is buried under a PermissionError."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        try:
            yield conn
        finally:
            conn.close()

ALERT_DAY = dt.date(2026, 10, 15)


def _trade(acc, ticker="ACME", who="Ann", cik="0000000001", day="2026-10-13", filed="2026-10-14",
           shares=1000.0, price=100.0, issuer_cik="777", seq=0, **kw):
    row = dict(test_form4()[0], accession=acc, seq=seq, ticker=ticker, issuer_name=f"{ticker} Inc",
               issuer_cik=issuer_cik, owner_name=who, owner_cik=cik, trans_date=day, filed_date=filed,
               shares=shares, price=price, value=shares * price, shares_after=shares * 3,
               code="P", is_derivative=0, plan_10b5_1=0, is_officer=1, is_director=0, is_ten_pct=0,
               relationship="Chief Executive Officer", form_type="4", url=f"https://sec.example/{acc}")
    row.update(kw)
    return row


def _alert_cfg(**rule_over):
    cfg = {"settings": dict(alerts.SETTINGS_DEFAULTS), "rules": []}
    for rtype, name in (("cluster_buy", "Cluster"), ("large_buy", "Large")):
        r = {**alerts.RULE_DEFAULTS[rtype], "name": name, "type": rtype, "tickers": [], "exclude_tickers": [],
             "webhook_env": "DISCORD_WEBHOOK_URL"}
        r.update(rule_over.get(rtype, {}))
        cfg["rules"].append(r)
    return cfg


class _FakeDiscord:
    def __init__(self, replies=None):
        self.replies, self.posts = list(replies or []), []
    def post(self, url, params=None, json=None, timeout=None):
        self.posts.append(json)
        code = self.replies.pop(0) if self.replies else 204
        if isinstance(code, Exception):
            raise code
        r = _Resp(code, {"retry_after": 0.5} if code == 429 else {})
        r.text = ""
        return r


def test_alerts_config_validation():
    cfg = alerts.load_config(Path(__file__).resolve().parents[1] / "alerts.example.toml")
    assert [r["type"] for r in cfg["rules"]] == ["cluster_buy", "large_buy"], "the shipped example must load"
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "a.toml"
        bad.write_text("""
[settings]
max_age_days = -1
colour = "red"
[[rule]]
name = "X"
type = "cluster_buy"
min_insider = 3
[[rule]]
name = "X"
type = "large_buy"
roles = ["ceo"]
[[rule]]
type = "teleport"
""", encoding="utf-8")
        try:
            alerts.load_config(bad)
            raise AssertionError("bad config accepted")
        except alerts.ConfigError as exc:
            msg = str(exc)
        for expected in ("unknown key 'colour'", "unknown key 'min_insider'", "used twice", "roles must be",
                         "type must be one of", "max_age_days must be a non-negative number"):
            assert expected in msg, (expected, msg)
        try:
            alerts.load_config(Path(tmp) / "missing.toml")
            raise AssertionError("missing file accepted")
        except alerts.ConfigError as exc:
            assert "copy alerts.example.toml" in str(exc)


def test_alerts_rules_pick_the_right_trades():
    with _tmpdb() as conn:
        rows = [
            _trade("c1", who="Ann", cik="0000000001", shares=3000, price=100.0),            # $300k
            _trade("c2", who="Bob", cik="0000000002", shares=500, price=100.0, is_officer=0, is_director=1,
                   relationship="Director"),                                                  # $50k
            *[_trade(f"e{i}", who=f"Staff {i}", cik=f"10{i}", shares=30, price=100.0) for i in range(6)],  # $3k each
            # one person, solo filing and joint filing: must not make a "cluster" of two
            _trade("j1", ticker="JNT", issuer_cik="888", who="AULT MILTON", cik="0000000009", shares=5000, price=50.0),
            _trade("j2", ticker="JNT", issuer_cik="888", who="AULT MILTON; Ault & Co", cik="0000000009; 0000000010",
                   shares=5000, price=50.0),
            # never alerts: plan trade, derivative, a sale
            _trade("p1", ticker="PLN", issuer_cik="999", who="P1", cik="21", plan_10b5_1=1, shares=9e4),
            _trade("p2", ticker="PLN", issuer_cik="999", who="P2", cik="22", is_derivative=1, shares=9e4),
            _trade("p3", ticker="PLN", issuer_cik="999", who="P3", cik="23", code="S", acq_disp="D", shares=9e4),
            # UUU's real typo: about $5 all month, then "$5,134" a share
            *[_trade(f"u{i}", ticker="UUU", issuer_cik="555", who="Ault", cik="31", shares=1000, price=5.0,
                     day=f"2026-10-0{i + 1}", filed=f"2026-10-0{i + 2}") for i in range(3)],
            _trade("u9", ticker="UUU", issuer_cik="555", who="Ault", cik="31", shares=18000, price=5134.0),
            # SLBT's: a total far beyond any plausible insider buy, nothing to compare it with
            _trade("s1", ticker="SLBT", issuer_cik="444", who="Wang", cik="41", shares=4545306, price=2272653.0),
            # a 10% owner's huge buy: not an officer or director
            _trade("t1", ticker="FUND", issuer_cik="333", who="Big Fund LP", cik="51", shares=1e5, price=50.0,
                   is_officer=0, is_ten_pct=1, relationship="10% Owner"),
        ]
        db.insert_rows(conn, "insider_tx", rows)
        conn.commit()
        found, skipped = alerts.evaluate(conn, _alert_cfg(), ALERT_DAY, lambda r, fp: False)
        got = {(a["rule"], a["ticker"]) for a in found}
        assert ("Cluster", "ACME") in got
        acme = next(a for a in found if a["rule"] == "Cluster")
        assert [i["name"] for i in acme["insiders"]] == ["Ann", "Bob"], "the $3k staff buys don't count"
        assert acme["total"] == 350_000
        assert ("Cluster", "JNT") not in got, "a joint filing is still one person"
        assert not any(t == "PLN" for _, t in got), "plan trades, derivatives and sales never alert"
        assert "u9" in skipped and "5,134" in skipped["u9"][1], "the per-share typo is caught by its peers"
        assert "s1" in skipped and "one filing" in skipped["s1"][1], "the absurd total is caught by the cap"
        assert ("Large", "ACME") not in got, "Ann's $300k is under the $500k large-buy bar"
        assert ("Large", "FUND") not in got, "10% owners are excluded by the default roles"
        found, _ = alerts.evaluate(conn, _alert_cfg(large_buy={"roles": ["ten_pct"]}), ALERT_DAY, lambda r, fp: False)
        assert ("Large", "FUND") in {(a["rule"], a["ticker"]) for a in found}
        found, _ = alerts.evaluate(conn, _alert_cfg(cluster_buy={"exclude_tickers": ["ACME"]}), ALERT_DAY,
                                   lambda r, fp: False)
        assert not any(a["ticker"] == "ACME" for a in found if a["rule"] == "Cluster")
        found, _ = alerts.evaluate(conn, _alert_cfg(cluster_buy={"tickers": ["OTHER"]}), ALERT_DAY,
                                   lambda r, fp: False)
        assert not [a for a in found if a["rule"] == "Cluster"]


def test_alerts_post_once_and_follow_up_only_on_new_trades():
    env = {"DISCORD_WEBHOOK_URL": "https://discord.example/api/webhooks/1/SECRET"}
    with _tmpdb() as conn:
        db.insert_rows(conn, "insider_tx", [
            # an older member, filed too long ago to post: must not make the first post an "update"
            _trade("a0", who="Dee", cik="4", shares=3000, day="2026-10-05", filed="2026-10-06"),
            _trade("a1", who="Ann", cik="1", shares=3000),
            _trade("a2", who="Bob", cik="2", shares=3000, is_officer=0, is_director=1, relationship="Director"),
            _trade("old", ticker="OLD", issuer_cik="1234", who="Zed", cik="9", shares=9000, filed="2026-10-01",
                   day="2026-10-01")])
        conn.commit()
        cfg = _alert_cfg(large_buy={"min_value": 10**9})
        d = _FakeDiscord()
        r = alerts.run(conn, cfg, today=ALERT_DAY, env=env, http=d, sleep=lambda s: None)
        assert r["posted"] == 1 and len(d.posts) == 1
        assert "OLD" not in json.dumps(d.posts), "filed 2 weeks ago: too old to post on a first run"
        first = d.posts[0]["embeds"][0]
        assert "(update)" not in first["title"] and "🆕" not in first["description"],             "a first post is not an update"

        alerts.run(conn, cfg, today=ALERT_DAY, env=env, http=d, sleep=lambda s: None)
        assert len(d.posts) == 1, "nothing new: nothing posted"

        db.insert_rows(conn, "insider_tx", [_trade("a1-amend", who="Ann", cik="1", shares=3000, form_type="4/A")])
        conn.commit()
        alerts.run(conn, cfg, today=ALERT_DAY, env=env, http=d, sleep=lambda s: None)
        assert len(d.posts) == 1, "a 4/A restating the same trade is not news"

        db.insert_rows(conn, "insider_tx", [_trade("a3", who="Cy", cik="3", shares=2000, filed="2026-10-15")])
        conn.commit()
        alerts.run(conn, cfg, today=ALERT_DAY, env=env, http=d, sleep=lambda s: None)
        assert len(d.posts) == 2, "a new insider joining the cluster is news"
        update = d.posts[1]["embeds"][0]
        assert update["title"].endswith("(update)")
        new_lines = [l for l in update["description"].splitlines() if "🆕" in l]
        assert len(new_lines) == 1 and "Cy" in new_lines[0], "only the new insider is marked new"
        assert "4 insiders" in update["description"]


def test_alerts_cooldown_holds_back_repeat_buyers():
    env = {"DISCORD_WEBHOOK_URL": "https://discord.example/api/webhooks/1/SECRET"}
    with _tmpdb() as conn:
        cfg = _alert_cfg(cluster_buy={"min_insiders": 99})
        d = _FakeDiscord()
        day = lambda n: (ALERT_DAY + dt.timedelta(days=n)).isoformat()
        for n in range(0, 9):  # BORR's director: a big buy every day
            db.insert_rows(conn, "insider_tx", [_trade(f"b{n}", ticker="BORR", who="Troim", cik="7", shares=10000,
                                                       price=600.0 + n, day=day(n), filed=day(n))])
            conn.commit()
            alerts.run(conn, cfg, today=ALERT_DAY + dt.timedelta(days=n), env=env, http=d, sleep=lambda s: None)
        posted_days = [p["embeds"][0]["description"] for p in d.posts]
        assert len(d.posts) == 2, f"7-day cooldown: day 0 and day 7 only, got {len(d.posts)}"
        # held-back filings are recorded, so they can't surface later as a stale post
        assert conn.execute("select count(*) from alert_log where rule='Large'").fetchone()[0] == 9


def test_alerts_discord_payload_is_safe_and_within_limits():
    evil = "**Evil** @everyone [click](http://phish.example) # heading"
    a = {"rule": "Cluster", "type": "cluster_buy", "ticker": "ACME", "issuer": evil, "total": 1e6,
         "first": "2026-10-01", "last": "2026-10-02", "follow_up": False, "new_links": ["https://sec.example/1"],
         "insiders": [{"name": evil, "role": "CEO", "shares": 10.0, "value": 1e6, "avg_price": 1e5, "is_new": True,
                       "trades": [{"trans_date": "2026-10-01", "shares_after": 30.0}]}] * 60, "fps": []}
    body = alerts.payload(a, dict(alerts.SETTINGS_DEFAULTS))
    assert body["allowed_mentions"] == {"parse": []}, "no @everyone / role / user pings, whatever filers write"
    emb = body["embeds"][0]
    assert "[click](http" not in emb["description"] and "\\[click\\]" in emb["description"]
    assert "**Evil**" not in emb["description"] and "\\*\\*Evil\\*\\*" in emb["description"]
    assert len(emb["description"]) <= 4096 and len(emb["title"]) <= 256
    assert "+45 more insiders" in emb["description"], "long clusters are cut, not truncated mid-line"
    assert "Not investment advice." in emb["footer"]["text"]
    text = alerts.as_text(a)
    assert "**" not in text.replace("**Evil**", "") and "https://sec.example/1" in text


def test_alerts_posting_survives_rate_limits_and_never_leaks_the_webhook():
    url = "https://discord.example/api/webhooks/123/TOPSECRET"
    waits = []
    d = _FakeDiscord([429, 204])
    alerts.post(url, {"content": "x"}, http=d, sleep=waits.append)
    assert len(d.posts) == 2 and waits == [0.5], "waits as long as Discord says, then retries"

    for reply, hint in ((404, "deleted"), (401, "token")):
        try:
            alerts.post(url, {}, http=_FakeDiscord([reply]), sleep=lambda s: None)
            raise AssertionError("error swallowed")
        except alerts.DiscordError as exc:
            assert hint in str(exc) and "TOPSECRET" not in str(exc)
    leaky = requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {url}")
    try:
        alerts.post(url, {}, http=_FakeDiscord([leaky]), sleep=lambda s: None)
        raise AssertionError("error swallowed")
    except alerts.DiscordError as exc:
        assert "TOPSECRET" not in str(exc), "requests puts the URL in its errors; ours must not"

    # a failed post is not recorded, so the next run tries again
    with _tmpdb() as conn:
        db.insert_rows(conn, "insider_tx", [_trade("x1", who="Ann", cik="1", shares=9000)])
        conn.commit()
        env = {"DISCORD_WEBHOOK_URL": url}
        cfg = _alert_cfg(cluster_buy={"min_insiders": 99})
        r = alerts.run(conn, cfg, today=ALERT_DAY, env=env, http=_FakeDiscord([500, 500, 500, 500]),
                       sleep=lambda s: None)
        assert r["posted"] == 0 and r["failed"] and conn.execute("select count(*) from alert_log").fetchone()[0] == 0
        d = _FakeDiscord()
        assert alerts.run(conn, cfg, today=ALERT_DAY, env=env, http=d, sleep=lambda s: None)["posted"] == 1
        try:
            alerts.run(conn, cfg, today=ALERT_DAY + dt.timedelta(days=30), env={}, http=d)
        except alerts.DiscordError:
            raise AssertionError("nothing to post, so a missing webhook shouldn't matter yet")
        db.insert_rows(conn, "insider_tx", [_trade("x2", who="Bo", cik="2", shares=9000)])
        conn.commit()
        try:
            alerts.run(conn, cfg, today=ALERT_DAY, env={}, http=d)
            raise AssertionError("posting with no webhook configured")
        except alerts.DiscordError as exc:
            assert "DISCORD_WEBHOOK_URL is not set" in str(exc)


def test_alerts_backtest_and_cli_dry_run_change_nothing():
    from insider_tracker import cli
    with tempfile.TemporaryDirectory() as tmp:
        dbp = Path(tmp) / "t.db"
        conn = db.connect(dbp)
        today = dt.date.today()
        recent = lambda n: (today - dt.timedelta(days=n)).isoformat()
        db.insert_rows(conn, "insider_tx", [
            _trade("d1", who="Ann", cik="1", shares=3000, day=recent(3), filed=recent(2)),
            _trade("d2", who="Bob", cik="2", shares=3000, day=recent(2), filed=recent(1))])
        conn.commit()
        hits = alerts.backtest(conn, _alert_cfg(), 10, today=today)
        assert [a["rule"] for _, a in hits if not a["suppressed"]].count("Cluster") >= 1
        assert conn.execute("select count(*) from alert_log").fetchone()[0] == 0
        conn.close()

        cfgp = Path(tmp) / "alerts.toml"
        cfgp.write_text((Path(__file__).resolve().parents[1] / "alerts.example.toml").read_text(encoding="utf-8"),
                        encoding="utf-8")
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["--db", str(dbp), "alerts", "--config", str(cfgp), "--dry-run"])
        out = buf.getvalue()
        assert "ACME" in out and "would post 1" in out and "Ann" in out, out
        conn = db.connect(dbp)
        assert conn.execute("select count(*) from alert_log").fetchone()[0] == 0, "a dry run records nothing"
        conn.close()


def test_alerts_warn_when_filings_stop_arriving():
    """The ingest reads a 403 daily index as "not published yet", which is also how the SEC turns
    away a blocked client -- so the nightly run must notice a feed that has gone quiet."""
    import io
    from insider_tracker import cli
    day = dt.date.fromisoformat
    with _tmpdb() as conn:
        assert "no Form 4 filings at all" in alerts.stale_feed(conn, day("2026-10-13"))
        db.insert_rows(conn, "insider_tx", [_trade("q1", filed="2026-10-09")])  # a Friday
        conn.commit()
        assert alerts.stale_feed(conn, day("2026-10-13")) is None, "Columbus Day Monday, then Tuesday's morning run"
        assert alerts.stale_feed(conn, day("2026-10-14")) is None, "three quiet weekdays: still a plausible holiday run"
        warning = alerts.stale_feed(conn, day("2026-10-16"))
        assert warning and "since 2026-10-09 (5 weekdays)" in warning and "SEC_USER_AGENT" in warning

    # wired into the nightly command: the admin channel hears about it, and about a failed ingest
    with tempfile.TemporaryDirectory() as tmp:
        dbp, cfgp = Path(tmp) / "t.db", Path(tmp) / "alerts.toml"
        cfgp.write_text((Path(__file__).resolve().parents[1] / "alerts.example.toml").read_text(encoding="utf-8"),
                        encoding="utf-8")
        conn = db.connect(dbp)
        old = (dt.date.today() - dt.timedelta(days=30)).isoformat()
        db.insert_rows(conn, "insider_tx", [_trade("q1", day=old, filed=old)])
        conn.commit()
        conn.close()
        told = []
        saved = cli._sec, cli.edgar_form4.ingest, cli.alerts.notify_error
        try:
            cli._sec = lambda: None
            cli.edgar_form4.ingest = lambda *a: 0
            cli.alerts.notify_error = lambda cfg, msg: told.append(msg)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                cli.main(["--db", str(dbp), "alerts", "--config", str(cfgp), "--ingest-days", "3"])
            assert len(told) == 1 and f"since {old}" in told[0], told

            def no_identity():
                raise SystemExit("Set SEC_USER_AGENT to something like 'Jane Doe jane@example.com'.")
            cli._sec = no_identity
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                cli.main(["--db", str(dbp), "alerts", "--config", str(cfgp), "--ingest-days", "3"])
            assert len(told) == 2 and told[1].startswith("Form 4 ingest failed: Set SEC_USER_AGENT"), told
        finally:
            cli._sec, cli.edgar_form4.ingest, cli.alerts.notify_error = saved


def test_source_stays_python_311_compatible():
    """Home servers often run Debian 12 / Raspberry Pi OS, which ship Python 3.11. Python 3.12
    relaxed f-strings, so code written on a newer Python can silently stop running there. Only
    a 3.12+ tokenizer can see inside f-strings, so this check runs there (on 3.11 itself, any
    such line would already have failed to import)."""
    import io, tokenize
    if not hasattr(tokenize, "FSTRING_START"):
        return

    def delim(tokstr, prefixes):
        body = tokstr.lstrip(prefixes)
        return body[:3] if body[:3] in ('"""', "'''") else body[:1]

    root = Path(__file__).resolve().parents[1]
    problems = []
    for f in sorted(root.glob("insider_tracker/*.py")) + [Path(__file__)]:
        stack = []  # per open f-string: [delimiter, brace depth]
        for tok in tokenize.generate_tokens(io.StringIO(f.read_text(encoding="utf-8")).readline):
            where = f"{f.name}:{tok.start[0]}"
            if tok.type == tokenize.FSTRING_START:
                if stack and stack[-1][1] > 0:
                    q, outer = delim(tok.string, "fFrRbB"), stack[-1][0]
                    if q == outer or (len(outer) == 1 and q[0] == outer):
                        problems.append(f"{where} nested f-string reuses the outer quote")
                stack.append([delim(tok.string, "fFrRbB"), 0])
            elif tok.type == tokenize.FSTRING_END:
                stack.pop()
            elif stack:
                if tok.type == tokenize.OP and tok.string in "{}":
                    stack[-1][1] += 1 if tok.string == "{" else -1
                elif stack[-1][1] > 0 and tok.type in (tokenize.NL, tokenize.NEWLINE) and len(stack[-1][0]) == 1:
                    problems.append(f"{where} expression in a one-line f-string spans lines")
                elif stack[-1][1] > 0 and tok.type == tokenize.STRING:
                    q, outer = delim(tok.string, "rRbBuU"), stack[-1][0]
                    if chr(92) in tok.string:
                        problems.append(f"{where} backslash inside an f-string expression")
                    if q == outer or (len(outer) == 1 and q[0] == outer):
                        problems.append(f"{where} string inside reuses the f-string's quote")
    assert not problems, "needs Python 3.12+:\n  " + "\n  ".join(sorted(set(problems)))


def test_ui_sorting():
    """Column sorting: numeric order, text order, and blanks always last."""
    rows = [{"t": "b", "v": 5.0}, {"t": "a", "v": 100.0}, {"t": "c", "v": None},
            {"t": "A", "v": 20.0}]
    assert [r["v"] for r in ui._sort_rows(rows, "v", "asc")] == [5.0, 20.0, 100.0, None]
    assert [r["v"] for r in ui._sort_rows(rows, "v", "desc")] == [100.0, 20.0, 5.0, None]
    # numeric, not lexicographic: 100 sorts above 20
    assert ui._sort_rows(rows, "v", "desc")[0]["v"] == 100.0
    # text sorts case-insensitively
    assert [r["t"] for r in ui._sort_rows(rows, "t", "asc")] == ["a", "A", "b", "c"]
    # blanks stay at the bottom in BOTH directions, never treated as largest
    assert ui._sort_rows(rows, "v", "asc")[-1]["v"] is None
    assert ui._sort_rows(rows, "v", "desc")[-1]["v"] is None
    # unknown column is a no-op rather than an error
    assert ui._sort_rows(rows, "nope", "asc") == rows


def test_ui_wraps_long_text_columns():
    """Long free-text columns reflow; short and numeric ones stay on one line."""
    rows = [{"ticker": "BBD", "who": "Name One, " * 12, "value": 1234567.0, "seq": 1}]
    wrap = ui._wrap_cols(rows, ["ticker", "who", "value", "seq"])
    assert wrap == {"who"}, wrap
    # a long number is still a number: right-aligned and never wrapped
    assert "value" not in ui._wrap_cols([{"value": 123456789012.0}], ["value"])
    # and the markup caps it in a block, since max-width is ignored on a raw cell
    body = ui._table(rows, ["ticker", "who", "value"])
    assert 'class="txt"><span>' in body
    assert "td.txt>span{display:block;max-width:" in ui.CSS


def test_ui_comma_lists_get_breathing_room():
    """GROUP_CONCAT joins with a bare comma; names carry their own ', '. The two
    must not be confused -- the real case is 'Ault & Company, Inc.'."""
    raw = "AULT MILTON C III,AULT MILTON C III; Ault & Company, Inc.,Doe Jane"
    assert ui._split_list(raw) == ["AULT MILTON C III",
                                   "AULT MILTON C III; Ault & Company, Inc.", "Doe Jane"]
    txt, _ = ui._cell("who", raw)
    assert txt == ("AULT MILTON C III &middot; AULT MILTON C III; Ault &amp; Company, Inc."
                   " &middot; Doe Jane")


def test_congress_most_traded_splits_bought_and_sold():
    """Dollar floors split by direction, ratio = bought/sold, blank when nothing
    was sold, and exchanges count toward the total but neither side."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        base = {"chamber": "House", "doc_id": "d", "filed_date": "2026-09-01", "url": "u",
                "state_district": "", "owner": "Self", "asset": "", "asset_type": "",
                "amount_text": "", "amount_high": None, "parse_quality": "ok", "trans_date": "2026-08-01"}
        trades = [("AAA", "m1", "P", 15001), ("AAA", "m2", "S", 5001), ("AAA", "m3", "E", 1001),
                  ("BBB", "m1", "P", 1001)]
        db.insert_rows(conn, "congress_tx", [dict(base, seq=i, ticker=t, member=m, tx_type=x, amount_low=a)
                                             for i, (t, m, x, a) in enumerate(trades)])
        conn.commit()
        rows = {r["ticker"]: r for r in signals.congress_most_traded(conn, "2026-01-01")}
        page = ui.page_signals(conn, {"view": ["congress_most_traded"], "days": ["3650"]})
        conn.close()
    a, b = rows["AAA"], rows["BBB"]
    assert (a["min_bought_usd"], a["min_sold_usd"], a["min_total_usd"]) == (15001, 5001, 21003)
    assert a["bought_sold_ratio"] == 3.0
    assert b["bought_sold_ratio"] is None, "buys-only has no finite ratio"
    assert "3.00" in page and "bought_sold_ratio" in page


def test_cusip_map_caches_and_never_caches_failures():
    """Each CUSIP is looked up once ever; 'no ticker' is a cached answer, but a failed
    request is not -- an outage must not become a permanent 'no ticker'."""
    class Resp:
        def __init__(self, body, status=200):
            self.body, self.status_code = body, status
        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(str(self.status_code))
        def json(self):
            return self.body

    class FakeSec:
        def __init__(self, companies):
            self.companies, self.calls = companies, 0
        def get(self, url, **kw):
            self.calls += 1
            return Resp(self.companies)

    sec = FakeSec({})

    class FakeFigi:
        def __init__(self, plan):
            self.plan, self.sent = list(plan), []
        def post(self, url, json=None, **kw):
            self.sent.append([j["idValue"] for j in json])
            return self.plan.pop(0)(json)

    ok = {"data": [{"ticker": "AAPL", "name": "APPLE INC", "securityType": "Common Stock"}]}
    none = {"warning": "No identifier found."}
    bad = {"error": "Invalid idValue format."}

    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        holds = [{"accession": "a", "cusip": c, "put_call": "", "issuer": "X"}
                 for c in ("037833100", "G0750C108", "ZZZZZZZZZ", "")]
        db.insert_rows(conn, "holdings_13f", holds)
        conn.commit()
        assert cusip_map.unmapped(conn) == ["037833100", "G0750C108", "ZZZZZZZZZ"], "blank CUSIPs are skipped"

        down = FakeFigi([lambda j: Resp({}, 429)])
        assert cusip_map.resolve(conn, down, sec) == 0
        assert cusip_map.unmapped(conn), "a failed request must leave the CUSIPs unmapped"

        wrong_len = FakeFigi([lambda j: Resp([ok])])
        assert cusip_map.resolve(conn, wrong_len, sec) == 0, "a mismatched response is not trusted"

        good = FakeFigi([lambda j: Resp([ok, none, bad])])
        assert cusip_map.resolve(conn, good, sec) == 3
        got = {r[0]: r[1] for r in conn.execute("select cusip, ticker from cusip_ticker")}
        assert got == {"037833100": "AAPL", "G0750C108": "", "ZZZZZZZZZ": ""}
        assert cusip_map.unmapped(conn) == []

        again = FakeFigi([])
        assert cusip_map.resolve(conn, again, sec) == 0 and again.sent == [], "cached: no second lookup"

        # batches of at most BATCH, and the ticker reaches the 13F signal
        many = [{"accession": "a", "cusip": f"{i:09d}", "put_call": "", "issuer": "X"} for i in range(25)]
        db.insert_rows(conn, "holdings_13f", many); conn.commit()
        big = FakeFigi([lambda j: Resp([none] * len(j))] * 3)
        assert cusip_map.resolve(conn, big, sec) == 25
        assert [len(b) for b in big.sent] == [10, 10, 5]

        for acc, period, shares in (("p", "2026-03-31", 10), ("q", "2026-06-30", 40)):
            db.insert_rows(conn, "holdings_13f", [{
                "accession": acc, "manager_cik": "1", "manager_name": "M", "period": period,
                "filed_date": period, "cusip": "037833100", "issuer": "APPLE INC", "title_class": "COM",
                "put_call": "", "value_usd": 1.0, "shares": shares, "share_type": "SH"}])
        conn.commit()
        rows = signals.thirteenf_changes(conn)
        assert [(r["ticker"], r["change"]) for r in rows] == [("AAPL", "ADD")]
        conn.close()


def test_cusip_map_name_fallback_is_careful():
    """OpenFIGI misses real listed companies; the issuer name, matched against the SEC's
    own list, fills some in -- but only where it can be sure."""
    class Resp:
        status_code = 200
        def __init__(self, body): self.body = body
        def raise_for_status(self): pass
        def json(self): return self.body

    class Figi:  # knows nothing, like OpenFIGI does for these
        def post(self, url, json=None, **kw):
            return Resp([{"warning": "No identifier found."}] * len(json))

    class Sec:
        def __init__(self, ok=True): self.ok, self.calls = ok, 0
        def get(self, url, **kw):
            self.calls += 1
            if not self.ok:
                raise requests.ConnectionError("down")
            t = lambda tk, title: {"ticker": tk, "title": title}
            return Resp({str(i): v for i, v in enumerate([
                t("STX", "Seagate Technology Holdings plc"), t("WDC", "Western Digital Corp."),
                t("XOM", "ExxonMobil Holdings Corp"), t("ASML", "ASML HOLDING NV"), t("ASMLF", "ASML HOLDING NV"),
                t("VFC", "V F Corp"), t("CNS", "Cohen & Steers, Inc."), t("WT", "WisdomTree, Inc."),
                t("CHRT", "Chart Industries, Inc.")])})

    held = [("G7997R103", "SEAGATE TECHNOLOGY HLDNGS PL", "SH"),   # truncated mid-word
            ("958102AT2", "WESTERN DIGITAL CORP", "PRN"),            # a bond: not the stock
            ("30231G102", "EXXON MOBIL CORP", "SH"),                 # SEC lists it under another name
            ("N07059210", "ASML HOLDING N V", "SH"),                 # two tickers share the name
            ("918204108", "F N B CORP", "SH"),                       # normalises to nothing
            ("19247A100", "COHEN & STEERS INFRASTRUCTUR", "SH"),     # meaningful word cut off
            ("97717W000", "WISDOMTREE TR", "SH"),                    # trust: name can't pick the fund
            ("16115Q308", "CHART INDS INC", "SH")]                   # abbreviation
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        db.insert_rows(conn, "holdings_13f", [
            {"accession": "a", "cusip": c, "put_call": "", "issuer": n, "share_type": st} for c, n, st in held])
        conn.commit()

        down = Sec(ok=False)
        cusip_map.resolve(conn, Figi(), down)
        assert {r[0] for r in conn.execute("select source from cusip_ticker")} == {None},             "an unreachable SEC list must not mark anything as 'nothing found'"

        sec = Sec()
        cusip_map.resolve(conn, Figi(), sec)
        got = {r[0]: (r[1], r[2]) for r in conn.execute("select cusip, ticker, source from cusip_ticker")}
        assert got["G7997R103"] == ("STX", "sec_name")
        assert got["16115Q308"] == ("CHRT", "sec_name")
        for cusip in ("958102AT2", "30231G102", "N07059210", "918204108", "19247A100", "97717W000"):
            assert got[cusip] == ("", "none"), (cusip, got[cusip])

        before = sec.calls
        cusip_map.resolve(conn, Figi(), sec)
        assert sec.calls == before, "settled CUSIPs are not retried or re-downloaded"
        conn.close()

    # a cache row written before the source column existed still gets its second chance
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        db.insert_rows(conn, "holdings_13f", [{"accession": "a", "cusip": "G7997R103", "put_call": "",
                                              "issuer": "SEAGATE TECHNOLOGY HLDNGS PL", "share_type": "SH"}])
        conn.execute("INSERT INTO cusip_ticker (cusip, ticker) VALUES ('G7997R103', '')")  # source NULL
        conn.commit()
        cusip_map.resolve(conn, Figi(), Sec())
        assert conn.execute("select ticker, source from cusip_ticker").fetchone()[:] == ("STX", "sec_name")
        conn.close()


def test_cluster_buys_filter_by_first_and_last_buy():
    """Date ranges on the finished clusters, for first_buy and last_buy independently.
    LONG started early and is still buying; SHORT started later and stopped first."""
    def buys(ticker, who_dates):
        return [dict(test_form4()[0], accession=f"{ticker}-{i}", seq=0, ticker=ticker, issuer_name=ticker,
                     owner_name=who, trans_date=d) for i, (who, d) in enumerate(who_dates)]

    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        db.insert_rows(conn, "insider_tx",
                       buys("LONG", [("Ann", "2026-09-01"), ("Bob", "2026-09-25")]) +
                       buys("SHORT", [("Cy", "2026-09-15"), ("Di", "2026-09-18")]))
        conn.commit()

        def page(**q):
            qs = {"view": ["cluster_buys"], "days": ["3650"], **{k: [v] for k, v in q.items()}}
            return ui.page_signals(conn, qs)
        tickers = lambda html: sorted(set(re.findall(r'href="/chart\?ticker=(LONG|SHORT)"', html)))

        assert tickers(page()) == ["LONG", "SHORT"]
        # last buy: who is still active?
        assert tickers(page(df_last_buy_from="2026-09-20")) == ["LONG"]
        assert tickers(page(df_last_buy_to="2026-09-20")) == ["SHORT"]
        # first buy: who started recently?
        assert tickers(page(df_first_buy_from="2026-09-10")) == ["SHORT"]
        assert tickers(page(df_first_buy_to="2026-09-10")) == ["LONG"]
        # both at once combine (AND); inclusive at both ends
        assert tickers(page(df_first_buy_from="2026-09-01", df_last_buy_from="2026-09-25")) == ["LONG"]
        assert tickers(page(df_first_buy_from="2026-09-15", df_last_buy_to="2026-09-18")) == ["SHORT"]
        assert tickers(page(df_first_buy_from="2026-09-16", df_last_buy_to="2026-09-17")) == []
        # junk and blanks degrade to "no filter" instead of an error
        assert tickers(page(df_last_buy_from="not-a-date", df_first_buy_to="")) == ["LONG", "SHORT"]

        html = page(df_last_buy_from="2026-09-20")
        assert "1 of 2 rows" in html
        assert 'name="df_last_buy_from" value="2026-09-20"' in html, "the form keeps what you chose"
        assert 'type="date"' in html and "last buy" in html and "first buy" in html
        assert "df_last_buy_from=2026-09-20" in html, "sort links and CSV must carry the range"
        # the column dropdowns only offer values from clusters still in range
        who = re.search(r'name="sf_ticker".*?</select>', html, re.S).group(0)
        assert "LONG" in who and "SHORT" not in who
        # other views have no date controls
        assert "df_" not in ui.page_signals(conn, {"view": ["large_buys"], "days": ["3650"]})
        conn.close()


def _yahoo_fixture():
    return json.loads(_fixture("yahoo_chart_example.json").decode("utf-8"))


def test_prices_parse_yahoo_response_and_clean_symbols():
    bars = prices.parse_chart(_yahoo_fixture())
    assert len(bars) == 22 and bars[0]["date"] == "2026-09-15" and bars[-1]["date"] == "2026-10-14"
    # the date comes from the exchange's own offset: bars are stamped at the open, in UTC
    assert all(b["close"] and b["adj_close"] and b["high"] >= b["low"] for b in bars)
    assert [b["date"] for b in bars] == sorted({b["date"] for b in bars}), "one bar per day, ascending"
    assert prices.parse_chart({"chart": {"result": None, "error": {"code": "Not Found"}}}) == []
    holey = _yahoo_fixture()
    holey["chart"]["result"][0]["indicators"]["quote"][0]["close"][3] = None
    assert len(prices.parse_chart(holey)) == 21, "a day with no close is skipped, not zero"

    c = prices.candidates
    assert c("LEN") == ["LEN"] and c("BRK.B") == ["BRK-B"] and c("LEN/B") == ["LEN-B"]
    assert c("HEI, HEI.A") == ["HEI", "HEI-A"] and c("N O G") == ["NOG"]
    assert c("") == [] and c("NOT A TICKER AT ALL") == [] and c("WAYTOOLONGSYMBOL") == []


class _Resp:
    def __init__(self, code=200, body=None):
        self.status_code, self.body = code, body
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))
    def json(self):
        return self.body


class _Yahoo:
    """Answers per symbol: a payload, an int status, or an exception to raise."""
    def __init__(self, plan):
        self.plan, self.calls = plan, []
    def get(self, url, params=None, **kw):
        sym = url.rsplit("/", 1)[-1]
        self.calls.append((sym, dict(params or {})))
        got = self.plan[sym]
        if isinstance(got, Exception):
            raise got
        return _Resp(got, None) if isinstance(got, int) else _Resp(200, got)


def test_prices_cache_freshness_and_failures():
    payload = _yahoo_fixture()
    t0 = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")

        # first open: five years of history, stored under the Yahoo symbol
        y = _Yahoo({"EXH": payload})
        meta = prices.ensure(conn, "EXH", y, now=t0)
        assert (meta["status"], meta["symbol"]) == ("ok", "EXH")
        assert y.calls == [("EXH", {"interval": "1d", "range": "5y"})]
        assert conn.execute("select count(*) from prices where symbol='EXH'").fetchone()[0] == 22

        # reopened within the freshness window: no network at all
        y2 = _Yahoo({})
        prices.ensure(conn, "EXH", y2, now=t0 + dt.timedelta(hours=1))
        assert y2.calls == []

        # once stale: a top-up from a week before the newest bar, not a full re-download;
        # restated bars overwrite rather than duplicate
        y3 = _Yahoo({"EXH": payload})
        prices.ensure(conn, "EXH", y3, now=t0 + dt.timedelta(hours=7))
        sym, params = y3.calls[0]
        assert "range" not in params and params["period1"] < params["period2"]
        assert dt.datetime.fromtimestamp(params["period1"], dt.timezone.utc).date().isoformat() == "2026-10-07"
        assert conn.execute("select count(*) from prices where symbol='EXH'").fetchone()[0] == 22

        # an unknown symbol is remembered, so clicking it again doesn't hit Yahoo
        y4 = _Yahoo({"ZZZZ": 404})
        m = prices.ensure(conn, "ZZZZ", y4, now=t0)
        assert m["status"] == "none" and "ZZZZ" in m["note"]
        prices.ensure(conn, "ZZZZ", y4, now=t0 + dt.timedelta(days=3))
        assert len(y4.calls) == 1
        prices.ensure(conn, "ZZZZ", y4, now=t0 + dt.timedelta(days=8))
        assert len(y4.calls) == 2, "but not forever"

        # unreachable is different from nonexistent: retried soon, and bars already cached survive
        y5 = _Yahoo({"EXH": requests.ConnectionError("down")})
        m = prices.ensure(conn, "EXH", y5, now=t0 + dt.timedelta(days=2))
        assert m["status"] == "error" and "down" in m["note"]
        assert conn.execute("select count(*) from prices where symbol='EXH'").fetchone()[0] == 22
        y6 = _Yahoo({"EXH": payload})
        prices.ensure(conn, "EXH", y6, now=t0 + dt.timedelta(days=2, minutes=11))
        assert prices.get_meta(conn, "EXH")["status"] == "ok"

        # 'HEI, HEI.A': the first spelling Yahoo doesn't know falls through to the next
        y7 = _Yahoo({"HEI": 404, "HEI-A": payload})
        m = prices.ensure(conn, "HEI, HEI.A", y7, now=t0)
        assert (m["status"], m["symbol"]) == ("ok", "HEI-A") and [c[0] for c in y7.calls] == ["HEI", "HEI-A"]
        # not a symbol at all: say so without asking Yahoo anything
        y8 = _Yahoo({})
        assert prices.ensure(conn, "not a ticker", y8, now=t0)["status"] == "none" and y8.calls == []

        # and the wrapper for read-only callers turns a locked/unwritable database into a status
        from insider_tracker import db as _db
        real = _db.connect
        _db.connect = lambda path=None: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
        try:
            got = prices.safe_ensure("irrelevant.db", "EXH", _Yahoo({}))
        finally:
            _db.connect = real
        assert got["status"] == "error" and "locked" in got["note"], got
        conn.close()


def test_chart_markers_and_svg():
    dates = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06"]   # 10-03/04 is a weekend
    closes = [100.0, 102.0, 104.0, 103.0]
    assert chart.close_on(dates, closes, "2026-10-04") == 102.0, "weekend trade uses Friday's close"
    assert chart.close_on(dates, closes, "2026-09-30") is None

    buys = chart.insider_markers([
        {"trans_date": "2026-10-02", "owner_name": "Ann", "shares": 100, "price": 100.0, "value": 10000.0},
        {"trans_date": "2026-10-02", "owner_name": "Bob", "shares": 300, "price": 104.0, "value": 31200.0},
        {"trans_date": "2026-10-05", "owner_name": "Cy", "shares": 5, "price": 99999.0, "value": 499995.0},
        {"trans_date": "2026-10-06", "owner_name": "Di", "shares": 9, "price": 20.0, "value": 180.0}],
        dates, closes)
    by = {m["date"]: m for m in buys}
    assert abs(by["2026-10-02"]["y"] - 103.0) < 1e-9, "several buys one day: one marker at the volume-weighted price"
    assert "2 insiders bought 400 shares" in by["2026-10-02"]["label"]
    assert by["2026-10-05"]["y"] == 104.0 and "far from that day's close" in by["2026-10-05"]["label"], \
        "a typo'd price must not stretch the chart"
    assert by["2026-10-06"]["y"] == 103.0, "a split-sized gap is drawn at the close too"

    cong = chart.congress_markers([
        {"trans_date": "2026-10-02", "tx_type": "P", "member": "A", "amount_text": "$1,001 - $15,000"},
        {"trans_date": "2026-10-02", "tx_type": "P", "member": "B", "amount_text": "$1,001 - $15,000"},
        {"trans_date": "2026-10-02", "tx_type": "S", "member": "C", "amount_text": ""},
        {"trans_date": "2026-10-02", "tx_type": "E", "member": "D", "amount_text": ""}], dates, closes)
    assert sorted((m["kind"], m["label"].count("congressional")) for m in cong) == [("P", 1), ("S", 1)]
    assert "2 congressional purchases" in [m for m in cong if m["kind"] == "P"][0]["label"]

    bars = [{"date": d, "close": c} for d, c in zip(dates, closes)]
    svg = chart.build_svg(bars, buys, cong, [{"first": "2026-10-02", "last": "2026-10-05", "label": "Cluster: 2"}],
                          title="LEN \u00b7 Lennar")
    assert svg.startswith("<svg") and svg.endswith("</svg>") and "LEN \u00b7 Lennar" in svg
    assert svg.count("<polyline") == 1 and svg.count("<circle") == 2 + 2, "two congress markers + the legend's dot and ring"
    assert "url(#hatch)" in svg and "Cluster: 2" in svg, "the cluster window is drawn and explains itself"
    assert "var(--fg)" in svg and "#000" not in svg, "colours are theme variables, so dark mode follows"
    assert chart.build_svg(bars[:1], [], [], []) == "", "one bar is not a chart"
    assert chart.nice_ticks(70.9, 81.1) == [72.5, 75.0, 77.5, 80.0]


def test_ui_chart_page():
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        bars = prices.parse_chart(_yahoo_fixture())
        db.insert_rows(conn, "prices", [dict(b, symbol="EXH") for b in bars])
        db.insert_rows(conn, "insider_tx", [dict(test_form4()[0], accession=f"A{i}", seq=0, ticker="EXH",
                                                 issuer_name="Example Homes Corp", owner_name=w, trans_date="2026-10-02",
                                                 price=78.0, shares=100.0, value=7800.0)
                                            for i, w in enumerate(("Ann", "Bob"))])
        db.insert_rows(conn, "congress_tx", [{
            "chamber": "House", "doc_id": "d", "seq": 0, "member": "Hon. A", "ticker": "EXH", "tx_type": "P",
            "trans_date": "2026-09-30", "amount_text": "$1,001 - $15,000", "filed_date": "2026-10-10",
            "asset": "", "asset_type": "", "owner": "Self", "state_district": "", "amount_low": 1001.0,
            "amount_high": 15000.0, "parse_quality": "ok", "url": "u"}])
        conn.commit()
        ok = {"symbol": "EXH", "status": "ok", "note": None, "fetched_at": "2026-10-15T02:52:14+00:00"}

        def page(meta, **q):
            return ui.page_chart(conn, {k: [v] for k, v in {"ticker": "EXH", "range": "1y", **q}.items()}, meta)
        html = page(ok)
        assert '<svg class="chart"' in html and "Example Homes Corp" in html
        assert "url(#hatch)" in html, "two insiders buying -> a cluster window"
        assert "Cluster: 2 insiders" in html and "1 congressional trades" in html
        assert ">insider buy<" in html and ">congress buy<" in html, "the trades are also listed in a table"
        assert 'class="on"' in html and "range=3m" in html, "range tabs, current one marked"
        assert page(ok, range="bogus").count('class="on"') >= 1, "an unknown range falls back"
        # a ticker spelled differently in another table still finds its trades
        assert "Cluster: 2 insiders" in page(ok, ticker="EXH, EXH.B"), "multi-class spelling matches"

        none = page({"symbol": None, "status": "none", "note": "Yahoo has no price history for XYZ"}, ticker="XYZ")
        assert '<svg class="chart"' not in none and "No price chart for XYZ" in none
        err = page({"symbol": None, "status": "error", "note": "ConnectionError: down"})
        assert "Couldn" in err and "ConnectionError: down" in err
        assert "Click any ticker" in ui.page_chart(conn, {}, None)

        # every ticker in a table is a link to its chart, including awkward spellings
        cell, _ = ui._cell("ticker", "HEI, HEI.A")
        assert cell.startswith('<a class="tk" href="/chart?ticker=HEI%2C+HEI.A"')
        assert ui._cell("ticker", "") == ("", "nil")
        assert 'href="/chart?ticker=EXH"' in ui.page_signals(conn, {"view": ["cluster_buys"], "days": ["3650"]})
        # the chart is part of what Copy PNG draws, and the button is enabled on this page
        assert 'id="copypng"' in html and "svg.chart" in ui.COPY_PNG_JS and "disabled" not in re.search(
            r"<button[^>]*id=\"copypng\"[^>]*>", html).group(0)
        conn.close()


def test_profiles_parse_responses():
    y = profiles.parse_yahoo(json.loads(_fixture("yahoo_profile_example.json").decode("utf-8")))
    assert y["source"] == "yahoo" and y["sector"] == "Consumer Cyclical"
    assert y["industry"] == "Residential Construction" and y["hq"] == "Anytown, FL, United States"
    assert y["employees"] == 4321 and y["website"] == "https://www.example-homes.example"
    assert y["description"].startswith("Example Homes Corporation") and len(y["description"]) > 500
    sec = profiles.parse_sec(json.loads(_fixture("sec_submissions_len.json").decode("utf-8")))
    assert sec["source"] == "sec" and sec["industry"] == "General Bldg Contractors - Residential Bldgs"
    assert sec["description"] == "", "the SEC gives a category, never a summary"
    # nothing worth showing is None, not an empty card
    assert profiles.parse_yahoo({"quoteSummary": {"result": None, "error": {"code": "Not Found"}}}) is None
    assert profiles.parse_yahoo({"quoteSummary": {"result": [{"assetProfile": {}}]}}) is None
    assert profiles.parse_sec({"name": "X", "sicDescription": ""}) is None


class _ProfileHttp:
    """Stands in for Yahoo (cookie, crumb, quoteSummary) or the SEC, by URL substring."""
    def __init__(self, summary=None, crumb="abc123", crumb_status=200, summary_status=200, sec=None, down=False):
        self.summary, self.crumb, self.crumb_status = summary, crumb, crumb_status
        self.summary_status, self.sec, self.down, self.calls = summary_status, sec, down, []
    def get(self, url, params=None, **kw):
        self.calls.append((url, dict(params or {})))
        if self.down:
            raise requests.ConnectionError("down")
        r = _Resp(404, "")
        if "fc.yahoo.com" in url:
            return _Resp(404, None)
        if "getcrumb" in url:
            r = _Resp(self.crumb_status, None); r.text = self.crumb; return r
        if "quoteSummary" in url:
            return _Resp(self.summary_status, self.summary)
        if "submissions" in url:
            return _Resp(200 if self.sec else 404, self.sec)
        return r


def test_profiles_cache_fallback_and_failures():
    yahoo_doc = json.loads(_fixture("yahoo_profile_example.json").decode("utf-8"))
    sec_doc = json.loads(_fixture("sec_submissions_len.json").decode("utf-8"))
    t0 = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")

        # the handshake: cookie, then crumb, then the summary, carrying that crumb
        h = _ProfileHttp(summary=yahoo_doc)
        got = profiles.ensure(conn, "EXH", "EXH", 920760, yahoo_http=h, now=t0)
        assert got["status"] == "ok" and got["source"] == "yahoo" and got["employees"] == 4321
        urls = [c[0] for c in h.calls]
        assert "fc.yahoo.com" in urls[0] and "getcrumb" in urls[1] and "quoteSummary" in urls[2]
        assert h.calls[2][1] == {"modules": "assetProfile", "crumb": "abc123"}

        # fresh for a month: reopening costs no requests; after that it refreshes
        h2 = _ProfileHttp(summary=yahoo_doc)
        profiles.ensure(conn, "EXH", "EXH", 920760, yahoo_http=h2, now=t0 + dt.timedelta(days=29))
        assert h2.calls == []
        profiles.ensure(conn, "EXH", "EXH", 920760, yahoo_http=h2, now=t0 + dt.timedelta(days=31))
        assert h2.calls

        # Yahoo knows nothing (delisted) but the SEC does: a category-only profile, retried daily
        h3, s3 = _ProfileHttp(summary_status=404), _ProfileHttp(sec=sec_doc)
        got = profiles.ensure(conn, "OLD", "OLD", 920760, yahoo_http=h3, sec_http=s3, now=t0)
        assert got["source"] == "sec" and got["industry"].startswith("General Bldg") and not got["description"]
        h4 = _ProfileHttp(summary=yahoo_doc)
        profiles.ensure(conn, "OLD", "OLD", 920760, yahoo_http=h4, sec_http=s3, now=t0 + dt.timedelta(hours=2))
        assert h4.calls == [], "not yet"
        got = profiles.ensure(conn, "OLD", "OLD", 920760, yahoo_http=h4, sec_http=s3, now=t0 + dt.timedelta(days=2))
        assert got["source"] == "yahoo", "the thin SEC profile is upgraded once Yahoo has it"

        # nowhere has anything: remembered for a week, not re-asked on every click
        h5, s5 = _ProfileHttp(summary_status=404), _ProfileHttp(sec=None)
        assert profiles.ensure(conn, "NADA", "NADA", 1, yahoo_http=h5, sec_http=s5, now=t0)["status"] == "none"
        n = len(h5.calls)
        profiles.ensure(conn, "NADA", "NADA", 1, yahoo_http=h5, sec_http=s5, now=t0 + dt.timedelta(days=3))
        assert len(h5.calls) == n
        profiles.ensure(conn, "NADA", "NADA", 1, yahoo_http=h5, sec_http=s5, now=t0 + dt.timedelta(days=8))
        assert len(h5.calls) > n

        # unreachable is an error (retried in minutes), and a bad crumb is too -- neither raises
        got = profiles.ensure(conn, "DOWN", "DOWN", None, yahoo_http=_ProfileHttp(down=True), now=t0)
        assert got["status"] == "error" and "down" in got["note"]
        assert profiles.ensure(conn, "DOWN", "DOWN", None, yahoo_http=_ProfileHttp(summary=yahoo_doc),
                               now=t0 + dt.timedelta(minutes=11))["status"] == "ok"
        got = profiles.ensure(conn, "NOCRUMB", "NOCRUMB", None,
                              yahoo_http=_ProfileHttp(crumb="<html>", summary=yahoo_doc), now=t0)
        assert got["status"] == "error" and "crumb" in got["note"]

        # no Yahoo symbol and no CIK: nothing to ask, and nothing breaks
        assert profiles.ensure(conn, "??", None, None, now=t0)["status"] == "none"
        conn.close()


def test_ui_chart_page_shows_company_description():
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        y = profiles.parse_yahoo(json.loads(_fixture("yahoo_profile_example.json").decode("utf-8")))
        profiles._store(conn, "EXH", "ok", dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc), y)
        meta = {"symbol": None, "status": "none", "note": "n"}
        page = lambda t: ui.page_chart(conn, {"ticker": [t]}, meta)

        html = page("EXH")
        about = re.search(r'<div class="about">(.*?)</div>', html, re.S).group(1)
        assert "Consumer Cyclical" in about and "Residential Construction" in about
        assert "Anytown, FL, United States" in about and "4,321 employees" in about
        assert 'href="https://www.example-homes.example"' in about and ">example-homes.example<" in about
        assert "Example Homes Corporation, together with its subsidiaries" in about
        assert "<details>" in about and "read more" in about, "long summaries are folded after the first sentences"
        lead = re.search(r"</p><p>(.*?)</p>", about, re.S).group(1)
        assert 100 < len(lead) < 700 and lead.endswith("."), "folds at a sentence boundary"
        assert "Yahoo Finance" in about

        # SEC-only: just the category, and it says why there is no paragraph
        profiles._store(conn, "OLD", "ok", dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc),
                        profiles.parse_sec(json.loads(_fixture("sec_submissions_len.json").decode("utf-8"))))
        sec_html = re.search(r'<div class="about">(.*?)</div>', page("OLD"), re.S).group(1)
        assert "General Bldg Contractors" in sec_html and "no business summary" in sec_html
        assert "<details>" not in sec_html

        # nothing known, or a failed lookup: no empty card, no error
        profiles._store(conn, "NADA", "none", dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc))
        assert 'class="about"' not in page("NADA") and 'class="about"' not in page("NEVERFETCHED")

        # whatever a source puts in the website field, only web links become links, and text is escaped
        evil = dict(y, website="javascript:alert(1)", description="<script>alert(1)</script> Real text.")
        profiles._store(conn, "EVIL", "ok", dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc), evil)
        evil_html = page("EVIL")
        assert "javascript:" not in evil_html and "<script>alert(1)</script>" not in evil_html
        assert "&lt;script&gt;" in evil_html
        conn.close()


def test_ui_chart_back_arrow():
    """The arrow returns to the page the ticker was clicked from, and can't be turned into a
    link to anywhere else. (The history/scroll behaviour needs a browser; it was driven
    through headless Edge, not here.)"""
    ok = ui._safe_back
    assert ok("/signals?view=cluster_buys&days=30&sf_who=Ann") == "/signals?view=cluster_buys&days=30&sf_who=Ann"
    assert ok("/raw?table=insider_tx&f_ticker=LEN") == "/raw?table=insider_tx&f_ticker=LEN"
    BS, CR, LF = chr(92), chr(13), chr(10)
    for bad in ("//evil.example/x", "https://evil.example", "http://127.0.0.1:8765/signals", "javascript:alert(1)",
                "/" + BS + "evil.example", "/chart?ticker=LEN", "/chart", "", None,
                "/x" + CR + LF + "Set-Cookie: a=b", "signals"):
        assert ok(bad) is None, bad

    host = "127.0.0.1:8765"
    assert ui.referer_path(f"http://{host}/signals?view=large_buys&sf_ticker=LEN", host) ==         "/signals?view=large_buys&sf_ticker=LEN"
    assert ui.referer_path("http://evil.example/signals", host) is None, "another site is not 'back'"
    assert ui.referer_path(f"http://{host}/chart?ticker=LEN&range=3m", host) is None, "never back to a chart"
    assert ui.referer_path(None, host) is None

    assert ui._back_label("/signals?view=large_buys") == "Large officer / director purchases"
    assert ui._back_label("/signals") == ui.VIEWS[0].title and ui._back_label("/raw?table=x") == "Raw tables"
    assert ui._back_label("/odd") == "the previous page"

    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        ok_meta = {"symbol": None, "status": "none", "note": "n"}
        def page(qs, referer=None):
            return ui.page_chart(conn, {k: [v] for k, v in {"ticker": "LEN", **qs}.items()}, ok_meta, referer)
        arrow = lambda html: re.search(r'<a id="back" class="back" href="([^"]*)"', html).group(1)

        assert arrow(page({})) == "/signals", "opened cold: Signals"
        ref = "/signals?view=large_buys&days=120&sort=value&dir=desc"
        assert arrow(page({}, referer=ref)) == ui._esc(ref), "comes from the page the click was on"
        assert arrow(page({"back": "/raw?table=insider_tx"}, referer=ref)) == "/raw?table=insider_tx",             "an explicit ?back= (carried by the range tabs) beats the Referer"
        assert arrow(page({"back": "//evil.example"}, referer=ref)) == ui._esc(ref), "a hostile ?back= is ignored"
        assert arrow(page({"back": "https://evil.example"})) == "/signals"

        html = page({}, referer=ref)
        assert "Back to Large officer / director purchases" in html
        # range tabs keep the destination, so the arrow survives changing the range
        tabs = re.findall(r'<a href="(/chart\?[^"]*)" data-range="(\w+)"', html)
        assert len(tabs) == 4 and all("back=" in href and "large_buys" in urllib.parse.unquote(href) for href, _ in tabs)
        assert "history.back()" in html and "location.replace" in html, "the script that makes Back mean 'where I was'"
        conn.close()


def test_ui_copy_png_button():
    """The Copy PNG button lives in the navbar, pushed to the right edge, and is disabled
    on pages with nothing to copy. (The drawing itself runs in the browser; it was checked
    in headless Edge against real pages, not here.)"""
    with_table = ui._page("t", "signals", "<table><tr><td>x</td></tr></table>")
    nav = re.search(r"<nav>(.*?)</nav>", with_table, re.S).group(1)
    assert 'id="copypng"' in nav, "the button belongs inside the navbar"
    assert nav.rindex('id="copypng"') > nav.rindex("</a>"), "after the tab links, so it sits at the end"
    assert re.search(r"nav \.navbtn\{margin-left:auto", ui.CSS), "auto left margin pushes it to the right edge"
    btn = re.search(r"<button[^>]*id=\"copypng\"[^>]*>", with_table).group(0)
    assert "disabled" not in btn and "title=" in btn, "enabled, and explains itself on hover"
    assert "clipboard.write" in with_table and "ClipboardItem" in with_table

    # a page with no table has nothing to copy: disabled, and says why
    empty = ui._page("t", "watchlist", "<form>no tables here</form>")
    btn = re.search(r"<button[^>]*id=\"copypng\"[^>]*>", empty).group(0)
    assert "disabled" in btn and "no table" in btn.lower()

    # the real pages: every tab with a table gets an enabled button, the form tabs a disabled one
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        for page, enabled in ((ui.page_signals(conn, {}), True),
                              (ui.page_raw(conn, {"table": ["processed"]}), True),
                              (ui.page_status(conn, {}), True),
                              (ui.page_architecture(conn, {}), True),
                              (ui.page_watchlist({}), False)):
            tag = re.search(r"<button[^>]*id=\"copypng\"[^>]*>", page).group(0)
            assert ("disabled" not in tag) == enabled, tag
        conn.close()

    # the script has to be valid JavaScript; a typo would silently kill the button
    import shutil, subprocess
    node = shutil.which("node")
    if node:
        f = Path(tempfile.mkdtemp()) / "copy.js"
        f.write_text(ui.COPY_PNG_JS, encoding="utf-8")
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_ui_filter_out_empty_values():
    """Hiding empty cells, on both tabs. 'Empty' means NULL or '' -- the ingesters
    store a missing ticker as '' but a missing price as NULL."""
    rows = [{"t": "A", "v": 1.0}, {"t": "B", "v": None}, {"t": "C", "v": ""}, {"t": "D", "v": 0.0}]
    keep = ui._apply_filters(rows, {"v": ui.NOT_BLANK})
    assert [r["t"] for r in keep] == ["A", "D"], "zero is a value, not a blank"
    assert [r["t"] for r in ui._apply_filters(rows, {"v": ui.BLANK})] == ["B", "C"]

    sel = ui._filter_select("v", rows, None, "f")
    assert sel.index("(not blank)") < sel.index("(blank)") < sel.index(">1<"), \
        "blank handling sits at the top of the list"
    # a column with too many values to list still offers (not blank)
    many = [{"asset": f"a{i}"} for i in range(ui.MAX_FILTER_OPTIONS + 1)] + [{"asset": None}]
    sel = ui._filter_select("asset", many, None, "f")
    assert "(not blank)" in sel and 'value="a1"' not in sel
    # ...but with no blanks there is nothing to hide, so it stays an ellipsis
    assert "<select" not in ui._filter_select("asset", many[:-1], None, "f")

    # Raw tables: !null / null through real SQL, against NULL and '' alike
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        f4 = test_form4()
        db.insert_rows(conn, "insider_tx", [
            dict(f4[0], seq=0), dict(f4[0], seq=1, price=None, value=None),
            dict(f4[0], seq=2, ticker=""), dict(f4[0], seq=3, ticker="", price=None)])
        conn.commit()
        conn.row_factory = sqlite3.Row

        def count(**filters):
            where, params = [], []
            for c, expr in filters.items():
                clause, vals = ui._filter_clause(c, expr)
                where.append(clause)
                params.extend(vals)
            return conn.execute(f"SELECT COUNT(*) FROM insider_tx WHERE {' AND '.join(where)}",
                                params).fetchone()[0]
        assert count(price="!null") == 2 and count(price="null") == 2
        assert count(ticker="!null") == 2 and count(ticker="null") == 2
        assert count(price="!null", ticker="!null") == 1
        assert count(ticker="NULL") == 2, "keyword is case-insensitive"
        page = ui.page_raw(conn, {"table": ["insider_tx"], "f_price": ["!null"], "cols": ["all"]})
        conn.close()
    assert re.search(r">2 rows matching<", page)


def test_ui_signal_filter_dropdowns():
    """Excel-style autofilter on the Signals tab: exact match, one name inside a
    list column, blanks selectable, options cascading from the other filters."""
    rows = [
        {"ticker": "AAA", "who": "Doe Jane,Roe Rick", "relationship": "Director", "value": 10.0},
        {"ticker": "BBB", "who": "Roe Rick,Acme Co, Inc.", "relationship": "", "value": 20.0},
        {"ticker": "CCC", "who": "Poe Pat", "relationship": "Director", "value": None},
    ]
    assert [r["ticker"] for r in ui._apply_filters(rows, {"relationship": "Director"})] == ["AAA", "CCC"]
    assert [r["ticker"] for r in ui._apply_filters(rows, {"relationship": ui.BLANK})] == ["BBB"]
    # a list column matches one member, not the whole joined string
    assert [r["ticker"] for r in ui._apply_filters(rows, {"who": "Roe Rick"})] == ["AAA", "BBB"]
    assert [r["ticker"] for r in ui._apply_filters(rows, {"who": "Acme Co, Inc."})] == ["BBB"]
    # numbers match on their raw value
    assert [r["ticker"] for r in ui._apply_filters(rows, {"value": "20.0"})] == ["BBB"]
    # skip= is how each dropdown ignores its own filter so its options cascade
    flt = {"relationship": "Director", "ticker": "AAA"}
    assert [r["ticker"] for r in ui._apply_filters(rows, flt, skip="ticker")] == ["AAA", "CCC"]

    sel = ui._filter_select("who", rows, "Roe Rick", "f")
    assert 'value="Acme Co, Inc."' in sel and 'value="Inc."' not in sel
    assert '<option value="Roe Rick" selected>' in sel and 'class="on"' in sel
    assert 'form="f"' in sel
    assert "(blank)" in ui._filter_select("relationship", rows, None, "f")
    assert ui._filter_select("url", rows, None, "f") == "", "a link column has nothing to filter"
    many = [{"asset": f"a{i}"} for i in range(ui.MAX_FILTER_OPTIONS + 1)]
    assert "<select" not in ui._filter_select("asset", many, None, "f")

    # end to end through the real page: filtered count, and the filter survives
    # into the sort links
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        f4 = test_form4()
        f4b = [dict(r, accession="0001234567-26-000002", owner_name="Roe Rick", owner_cik="2")
               for r in f4 if r["code"] == "P"]
        db.insert_rows(conn, "insider_tx", f4 + f4b)
        conn.commit()
        page = ui.page_signals(conn, {"view": ["large_buys"], "days": ["3650"],
                                      "p_min_value": ["0"], "sf_owner_name": ["Roe Rick"]})
        conn.close()
    assert "1 of 2 rows" in page
    assert 'id="sigform"' in page and 'name="sf_owner_name"' in page
    assert "sf_owner_name=Roe+Rick" in page, "sort links must carry the active filter"


def test_ui_escapes_tooltips():
    """Help text is interpolated into a title attribute, so it must be escaped."""
    assert ui._tip(chr(34) + "x" + chr(34) + " <b> & co") == (
        ' title="&quot;x&quot; &lt;b&gt; &amp; co"')
    assert ui._tip("") == ""


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
