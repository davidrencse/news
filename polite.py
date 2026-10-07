"""One polite HTTP client for everything the app fetches from other sites.

Requests to the same site share one limiter with a minimum gap between requests. Background work
(index crawler, curator, paywall checks) queues in one lane; requests a person is waiting on (opening
an article, Discover, search) use a priority lane so they never queue behind background work.
When a site pushes back (403/429/503), the gap grows and both lanes pause for as long as the site asks
(Retry-After) or a multiple of the gap. As requests succeed again, the gap shrinks back to normal.
Nothing here retries around a refusal: callers see the error and move on.
"""
import threading
import time
import socket
import ssl
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit, urlunsplit

USER_AGENT = "MediumLibrary/1.0 (personal offline reader)"
BASE_GAP = {"medium.com": 1.0, "freedium-mirror.cfd": 3.0, "miro.medium.com": 0.1}  # seconds between requests
DEFAULT_GAP = 1.0
MAX_GAP = 600.0
PUSHBACK = {403: 1.5, 429: 3.0, 503: 3.0}  # how much each refusal slows that site down
RECOVER_S = 60.0  # the extra gap halves after each minute without a pushback


class HostLimiter:
    def __init__(self, base):
        self.base = self.gap = base
        self.next_at = 0.0         # next free slot in the background lane
        self.next_priority = 0.0   # next free slot in the priority lane
        self.pause_until = 0.0     # only set when the site pushes back
        self.pushbacks = 0
        self.last_pushback = None
        self._recovered_at = 0.0
        self.codes = {}            # refusals by HTTP status, for the status panel
        self._lock = threading.Lock()

    def _recover(self, now):
        """Halve the extra gap for every RECOVER_S without a pushback, so a burst of refusals doesn't
        leave the site crawling for an hour (success-only recovery is slowest exactly when the gap is huge)."""
        quiet_since = max(self.last_pushback or 0, self._recovered_at)
        steps = (now - quiet_since) / RECOVER_S
        if self.gap > self.base and self.last_pushback and steps >= 1:
            self.gap = max(self.base, self.base + (self.gap - self.base) * 0.5 ** int(steps))
            self._recovered_at = now
            self.next_at = min(self.next_at, now + self.gap)  # drop slots booked at the old, longer gap
            self.next_priority = min(self.next_priority, now + self.gap / 2)

    def wait(self, priority=False):
        with self._lock:  # reserve a slot once, then sleep outside the lock
            now = time.time()
            self._recover(now)
            if priority:
                at = max(now, self.pause_until, self.next_priority)
                self.next_priority = at + self.gap / 2
            else:
                at = max(now, self.pause_until, self.next_at)
                self.next_at = at + self.gap
        # Sleep until the reserved slot, then recheck the shared cooldown: another request can be pushed
        # back while we sleep, moving pause_until past our slot. Dispatching then would ignore a cooldown
        # the site just asked for, so we wait again. The slot itself is booked only once, above.
        while True:
            now = time.time()
            target = max(at, self.pause_until)  # float read is atomic under the GIL
            if target <= now:
                return
            time.sleep(target - now)

    def succeeded(self):
        with self._lock:
            self.gap = max(self.base, self.gap * 0.8)

    def pushed_back(self, code, retry_after=None):
        with self._lock:
            self.gap = min(MAX_GAP, self.gap * PUSHBACK.get(code, 2.0))
            self.pushbacks += 1
            self.codes[code] = self.codes.get(code, 0) + 1
            self.last_pushback = time.time()
            # A site's own Retry-After is honoured exactly, however long: it is an explicit instruction,
            # and sitting out less than asked invites a longer ban. MAX_GAP caps only the per-request
            # gap and our fallback guess (when no Retry-After is given); it must not truncate a cooldown
            # the site itself requested.
            pause = retry_after if retry_after else (0 if code == 403 else min(MAX_GAP, self.gap * 10))
            self.pause_until = max(self.pause_until, time.time() + pause)


_limiters: dict[str, HostLimiter] = {}
_limiters_lock = threading.Lock()


def site_of(host):
    host = host.lower().split(":")[0]
    if host.startswith(("miro.", "cdn-images")) and host.endswith(".medium.com"):
        return host  # Medium's image CDN: its own limiter, never counted against medium.com
    return "medium.com" if host == "medium.com" or host.endswith(".medium.com") else host


def limiter(url_or_host):
    site = site_of(urlsplit(url_or_host).netloc if "://" in url_or_host else url_or_host)
    with _limiters_lock:
        if site not in _limiters:
            _limiters[site] = HostLimiter(BASE_GAP.get(site, DEFAULT_GAP))
        return _limiters[site]


def get(url, timeout=40, headers=None, priority=False, max_bytes=None):
    """GET a URL through its site's limiter. Returns bytes; raises urllib errors like urlopen.
    priority=True for requests a person is waiting on. max_bytes caps how much is read into memory
    (a hostile or mislabelled image URL can otherwise stream gigabytes); an over-limit body raises
    ValueError instead of being read in full."""
    # HTTP request targets are ASCII. Keep existing escapes and URL delimiters intact.
    parts = urlsplit(url)
    safe = "/%:@!$&'()*+,;=-._~"
    url = urlunsplit((parts.scheme, parts.netloc, quote(parts.path, safe=safe),
                     quote(parts.query, safe=safe + '?'), parts.fragment))
    lim = limiter(url)
    lim.wait(priority)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read() if max_bytes is None else r.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        if e.code in PUSHBACK:
            ra = e.headers.get("Retry-After", "") if e.headers else ""
            lim.pushed_back(e.code, int(ra) if ra.isdigit() else None)
        raise
    lim.succeeded()
    if max_bytes is not None and len(body) > max_bytes:
        raise ValueError(f"response larger than {max_bytes} bytes")
    return body


def describe_error(exc):
    """Concise source diagnostics without exposing a URL, credentials, or response body."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, socket.gaierror):
        return "DNS lookup failed"
    if isinstance(reason, ssl.SSLError):
        return "TLS connection failed"
    if isinstance(reason, TimeoutError):
        return "request timed out"
    if isinstance(reason, OSError):
        return f"connection failed ({type(reason).__name__})"
    if isinstance(reason, ValueError):
        return "article extraction failed"
    return type(reason).__name__


def get_text(url, timeout=40, priority=False):
    return get(url, timeout, priority=priority).decode("utf-8", "replace")


def status():
    now = time.time()
    for l in list(_limiters.values()):
        with l._lock:
            l._recover(now)
    return {site: {"gap_s": round(l.gap, 1), "pushbacks": l.pushbacks, "codes": dict(l.codes),
                   "waiting_s": max(0, round(l.pause_until - now)),   # the site asked us to wait
                   "queued_s": max(0, round(l.next_at - now)),        # ordinary background queue
                   "slowed": l.gap > l.base * 1.5 or l.pause_until - now > 5}
            for site, l in _limiters.items()}
