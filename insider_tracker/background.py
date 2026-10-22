"""One background thread per named cache fill (prices, quotes, news), so a page can show what is
cached right away and leave the rest to fill in while the user reads."""
import logging
import threading

from . import db

log = logging.getLogger(__name__)
_lock = threading.Lock()
_threads = {}


def running(name) -> bool:
    t = _threads.get(name)
    return bool(t and t.is_alive())


def start_db(name, db_path, fn) -> bool:
    """Run fn(conn) on a daemon thread with its own connection, unless `name` is already running.
    True if it is running now (this call's, or an earlier one the next page view will pick up after).
    Errors are logged, never raised: a cache fill must not take the server down."""
    with _lock:
        if running(name):
            return True

        def run():
            try:
                conn = db.connect(db_path)
                try:
                    fn(conn)
                finally:
                    conn.close()
            except Exception as exc:
                log.warning("%s refresh failed: %s", name, exc)

        _threads[name] = threading.Thread(target=run, name=name, daemon=True)
        _threads[name].start()
        return True
