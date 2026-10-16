"""Rate-limited HTTP session with retry/backoff."""
import logging
import threading
import time

import requests

log = logging.getLogger(__name__)
RETRY_STATUSES = {429, 500, 502, 503, 504}
# Content types whose .text we actually read. Anything else (zip, pdf) is left alone.
TEXTUAL = ("text/", "xml", "json", "html")


def _prefer_utf8(resp):
    """EDGAR serves documents as text/plain with no charset, so requests falls
    back to ISO-8859-1 (per RFC 2616) and mangles every non-ASCII character:
    a UTF-8 curly quote becomes 'a"', 'Munoz' becomes mojibake. The bodies are
    really UTF-8, so decode them that way when they in fact decode cleanly, and
    keep the original guess otherwise (a few very old EDGAR documents are
    genuinely Latin-1).
    """
    ctype = resp.headers.get("Content-Type", "").lower()
    if "charset=" in ctype or not any(t in ctype for t in TEXTUAL):
        return resp
    try:
        resp.content.decode("utf-8")
    except UnicodeDecodeError:
        return resp
    resp.encoding = "utf-8"
    return resp


class PoliteSession:
    def __init__(self, user_agent: str, min_interval: float, max_retries: int = 5, timeout: int = 30):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self._last = 0.0
        self._lock = threading.Lock()

    def _throttle(self):
        with self._lock:
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def request(self, method: str, url: str, **kw) -> requests.Response:
        kw.setdefault("timeout", self.timeout)
        resp = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.request(method, url, **kw)
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise
                log.warning("network error on %s (%s); retrying", url, exc)
                time.sleep(2 ** attempt)
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                try:
                    delay = float(resp.headers.get("Retry-After", ""))
                except ValueError:
                    delay = 2 ** (attempt + 1)
                log.warning("HTTP %s on %s; backing off %.0fs", resp.status_code, url, delay)
                time.sleep(delay)
                continue
            return _prefer_utf8(resp)
        return _prefer_utf8(resp)

    def get(self, url: str, **kw) -> requests.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> requests.Response:
        return self.request("POST", url, **kw)
