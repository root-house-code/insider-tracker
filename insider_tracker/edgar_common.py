ARCHIVES = "https://www.sec.gov/Archives"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"


def strip_ns(root):
    """Drop XML namespaces so .find() paths stay readable."""
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def tickers_to_ciks(http, tickers) -> dict:
    """Map tickers -> CIK (unpadded string) using the SEC's public ticker file."""
    resp = http.get(TICKER_MAP_URL)
    resp.raise_for_status()
    table = {v["ticker"].upper(): str(v["cik_str"]) for v in resp.json().values()}
    wanted = {t.upper().strip() for t in tickers if t.strip()}
    missing = wanted - table.keys()
    if missing:
        print(f"warning: no CIK found for {sorted(missing)}")
    return {t: table[t] for t in wanted if t in table}
