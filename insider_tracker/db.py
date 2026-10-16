"""SQLite storage. One file, no server, easy to query from anything."""
import sqlite3
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
  source TEXT NOT NULL, doc_id TEXT NOT NULL, status TEXT NOT NULL, note TEXT,
  processed_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (source, doc_id)
);

CREATE TABLE IF NOT EXISTS insider_tx (
  accession TEXT NOT NULL, seq INTEGER NOT NULL, form_type TEXT,
  filed_date TEXT, trans_date TEXT,
  issuer_cik TEXT, issuer_name TEXT, ticker TEXT,
  owner_cik TEXT, owner_name TEXT, relationship TEXT,
  is_director INTEGER, is_officer INTEGER, is_ten_pct INTEGER,
  security TEXT, is_derivative INTEGER,
  code TEXT,          -- P=open-market buy, S=open-market sale, A=grant, M=exercise, F=tax withhold, G=gift...
  acq_disp TEXT,      -- A=acquired, D=disposed
  shares REAL, price REAL, value REAL, shares_after REAL,
  direct_indirect TEXT,
  plan_10b5_1 INTEGER, -- 1 if flagged as a pre-scheduled 10b5-1 plan trade
  url TEXT,
  PRIMARY KEY (accession, seq)
);
CREATE INDEX IF NOT EXISTS ix_insider_date ON insider_tx(trans_date);
CREATE INDEX IF NOT EXISTS ix_insider_ticker ON insider_tx(ticker);
CREATE INDEX IF NOT EXISTS ix_insider_code ON insider_tx(code);

CREATE TABLE IF NOT EXISTS congress_tx (
  chamber TEXT NOT NULL, doc_id TEXT NOT NULL, seq INTEGER NOT NULL,
  member TEXT, state_district TEXT, filed_date TEXT, trans_date TEXT,
  owner TEXT,          -- Self / Spouse / Joint / Dependent
  ticker TEXT, asset TEXT, asset_type TEXT,
  tx_type TEXT,        -- P / S / E
  amount_text TEXT, amount_low REAL, amount_high REAL,
  parse_quality TEXT,  -- ok / no_ticker
  url TEXT,
  PRIMARY KEY (chamber, doc_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_congress_filed ON congress_tx(filed_date);
CREATE INDEX IF NOT EXISTS ix_congress_ticker ON congress_tx(ticker);

CREATE TABLE IF NOT EXISTS holdings_13f (
  accession TEXT NOT NULL, manager_cik TEXT, manager_name TEXT,
  period TEXT, filed_date TEXT,
  cusip TEXT NOT NULL, issuer TEXT, title_class TEXT,
  put_call TEXT NOT NULL DEFAULT '',
  value_usd REAL, shares REAL, share_type TEXT,
  PRIMARY KEY (accession, cusip, put_call)
);
CREATE INDEX IF NOT EXISTS ix_13f_mgr ON holdings_13f(manager_cik, period);

-- Daily bars for the chart page, keyed by Yahoo symbol; price_meta remembers what happened the
-- last time we asked about a ticker, so a delisted or malformed one isn't re-fetched on every click.
CREATE TABLE IF NOT EXISTS prices (
  symbol TEXT NOT NULL, date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, adj_close REAL, volume REAL,
  PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS price_meta (
  ticker TEXT PRIMARY KEY, symbol TEXT, status TEXT NOT NULL,  -- ok / none / error
  note TEXT, fetched_at TEXT NOT NULL
);

-- Which trades each Discord alert rule has already posted (fp = alerts.fingerprint), so a
-- re-run or a Form 4/A restating the same trade never posts twice.
CREATE TABLE IF NOT EXISTS alert_log (
  rule TEXT NOT NULL, fp TEXT NOT NULL, posted_at TEXT NOT NULL,
  PRIMARY KEY (rule, fp)
);
-- When each rule last posted about a company (or company+insider): drives cooldown_days.
CREATE TABLE IF NOT EXISTS alert_groups (
  rule TEXT NOT NULL, grp TEXT NOT NULL, last_posted TEXT NOT NULL,
  PRIMARY KEY (rule, grp)
);

-- What a company does, for the chart page. Keyed by the ticker string as clicked, like price_meta.
CREATE TABLE IF NOT EXISTS profiles (
  ticker TEXT PRIMARY KEY, status TEXT NOT NULL,  -- ok / none / error
  source TEXT,                                    -- yahoo (full summary) / sec (category only)
  description TEXT, sector TEXT, industry TEXT, hq TEXT, employees INTEGER, website TEXT,
  note TEXT, fetched_at TEXT NOT NULL
);

-- 13F identifies securities by CUSIP only; this caches OpenFIGI's answer.
-- ticker '' means "looked up, none exists" (munis, foreign lines, etc.).
CREATE TABLE IF NOT EXISTS cusip_ticker (
  cusip TEXT PRIMARY KEY, ticker TEXT NOT NULL DEFAULT '', name TEXT, security_type TEXT,
  checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
  source TEXT  -- figi (looked up by CUSIP), sec_name (matched on issuer name), none (nothing found)
);
"""


def connect(path=None) -> sqlite3.Connection:
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cusip_ticker)")}
    if "source" not in cols:  # database predates the column
        conn.execute("ALTER TABLE cusip_ticker ADD COLUMN source TEXT")
        conn.execute("UPDATE cusip_ticker SET source='figi' WHERE ticker<>''")
    return conn


def insert_rows(conn, table: str, rows) -> int:
    rows = list(rows)
    if not rows:
        return 0
    cols = list(rows[0].keys())
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    return len(rows)


def processed_ids(conn, source: str) -> set:
    return {r[0] for r in conn.execute(
        "SELECT doc_id FROM processed WHERE source=? AND status IN ('ok','no_transactions','needs_manual')",
        (source,))}


def mark(conn, source: str, doc_id: str, status: str, note: str = None):
    conn.execute(
        "INSERT OR REPLACE INTO processed (source, doc_id, status, note) VALUES (?,?,?,?)",
        (source, doc_id, status, note))
