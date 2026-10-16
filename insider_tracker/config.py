"""Runtime configuration. Everything is overridable via environment variables."""
import json
import os
from pathlib import Path

DB_PATH = Path(os.environ.get("INSIDER_DB", "data/insider_tracker.db"))
REPORT_DIR = Path(os.environ.get("INSIDER_REPORTS", "reports"))
WATCHLIST_PATH = Path(os.environ.get("INSIDER_WATCHLIST", "watchlist.json"))

# SEC fair-access policy: max 10 requests/second. Stay comfortably under it.
SEC_MIN_INTERVAL = 0.12
# House/Senate disclosure sites are small government servers. Be gentle.
CONGRESS_MIN_INTERVAL = 1.0

# Thresholds used by the report
LARGE_BUY_USD = 250_000
EXEC_SALE_USD = 1_000_000
LATE_FILING_DAYS = 45  # STOCK Act deadline is 45 days from the transaction


def sec_user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua or "@" not in ua:
        raise SystemExit(
            "Set SEC_USER_AGENT to something like 'Jane Doe jane@example.com'. "
            "The SEC blocks requests without a descriptive User-Agent."
        )
    return ua


def load_watchlist() -> dict:
    """Optional watchlist.json: {"tickers": [...], "managers_13f": {"cik": "name"}}."""
    if WATCHLIST_PATH.exists():
        return json.loads(WATCHLIST_PATH.read_text())
    return {"tickers": [], "managers_13f": {}}


def save_watchlist(data: dict) -> None:
    WATCHLIST_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
