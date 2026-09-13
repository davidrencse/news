"""One polite HTTP client for everything the app fetches from other sites.

Requests to the same site share one limiter: a minimum gap between requests. When a site pushes
back (403/429/503), the gap grows and new requests wait (at least as long as any Retry-After the
site sends). As requests succeed again, the gap shrinks back to normal. Nothing here retries around
a refusal: callers see the error and move on.
"""
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

USER_AGENT = "MediumLibrary/1.0 (personal offline reader)"
BASE_GAP = {"medium.com": 1.0, "freedium-mirror.cfd": 3.0}  # seconds between requests
DEFAULT_GAP = 1.0
MAX_GAP = 600.0
PUSHBACK = {403: 1.5, 429: 3.0, 503: 3.0}  # how much each refusal slows that site down


class HostLimiter:
    def __init__(self, base):
        self.base = self.gap = base
        self.next_at = 0.0
        self.pushbacks = 0
        self.last_pushback = None
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:  # reserve the next slot, then sleep outside the lock
            now = time.time()
            at = max(now, self.next_at)
            self.next_at = at + self.gap
        if at > now:
            time.sleep(at - now)

    def succeeded(self):
        with self._lock:
            self.gap = max(self.base, self.gap * 0.8)

    def pushed_back(self, code, retry_after=None):
        with self._lock:
            self.gap = min(MAX_GAP, self.gap * PUSHBACK.get(code, 2.0))
            self.pushbacks += 1
            self.last_pushback = time.time()
            pause = retry_after if retry_after else (0 if code == 403 else self.gap * 10)
            self.next_at = max(self.next_at, time.time() + pause)


_limiters: dict[str, HostLimiter] = {}
_limiters_lock = threading.Lock()


def site_of(host):
    host = host.lower().split(":")[0]
    return "medium.com" if host == "medium.com" or host.endswith(".medium.com") else host


def limiter(url_or_host):
    site = site_of(urlsplit(url_or_host).netloc if "://" in url_or_host else url_or_host)
    with _limiters_lock:
        if site not in _limiters:
            _limiters[site] = HostLimiter(BASE_GAP.get(site, DEFAULT_GAP))
        return _limiters[site]


def get(url, timeout=40, headers=None):
    """GET a URL through its site's limiter. Returns bytes; raises urllib errors like urlopen."""
    lim = limiter(url)
    lim.wait()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        if e.code in PUSHBACK:
            ra = e.headers.get("Retry-After", "") if e.headers else ""
            lim.pushed_back(e.code, int(ra) if ra.isdigit() else None)
        raise
    lim.succeeded()
    return body


def get_text(url, timeout=40):
    return get(url, timeout).decode("utf-8", "replace")


def status():
    now = time.time()
    return {site: {"gap_s": round(l.gap, 1), "pushbacks": l.pushbacks,
                   "waiting_s": max(0, round(l.next_at - now)),
                   "slowed": l.gap > l.base * 1.5 or l.next_at - now > 5}
            for site, l in _limiters.items()}
