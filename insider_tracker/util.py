import datetime as dt
import re

_MONEY = re.compile(r"\$?\s*([\d,]+(?:\.\d+)?)")


def to_num(s):
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def mdy_to_iso(s):
    """'1/5/2026' or '01/05/2026' -> '2026-01-05'. Returns None if unparseable."""
    if not s:
        return None
    s = s.strip().split(" ")[0]
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_amount_range(text):
    """'$1,001 - $15,000' -> (1001, 15000); 'Over $50,000,000' -> (50000000, None)."""
    if not text:
        return None, None
    nums = [to_num(m) for m in _MONEY.findall(text)]
    nums = [n for n in nums if n is not None]
    if not nums:
        return None, None
    if text.strip().lower().startswith("over") or len(nums) == 1:
        return nums[0], (nums[0] if not text.strip().lower().startswith("over") else None)
    return nums[0], nums[1]


def daterange(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)
