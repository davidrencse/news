"""On-demand news briefing: 60 topical feeds -> cluster & rank -> 10 candidates -> 5 reading picks.

The pipeline has four stages, each kept independently testable:
  scan      fetch_topic / parse_feed    one bounded feed per topic, retried once on a transient error
  select    select_briefing             pure, deterministic: merge, interest boost, cluster, diversify
  resolve   resolve_article_url         best-effort Google News -> publisher URL, only for the shortlist
  save      fetch_article / news_document   extract a readable copy for the library (app.py writes it)
Only scan and resolve touch the network; select is pure so the unit tests need no stubs there.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import base64
import binascii
import copy
import html
import math
import re
import threading
import time
from urllib.parse import urlencode, urljoin, urlsplit
import uuid
import xml.etree.ElementTree as ET

import polite


TOPIC_GROUPS = {
    "AI": ("artificial intelligence", "AI models", "AI regulation", "AI chips", "AI research", "robotics"),
    "Software": ("open source software", "software engineering", "cloud computing", "programming languages", "developer tools", "data centers"),
    "Security": ("cybersecurity", "ransomware", "data breach", "software vulnerability", "digital privacy", "critical infrastructure security"),
    "Devices": ("semiconductors", "smartphones", "personal computers", "consumer technology", "wearable technology", "quantum computing"),
    "Business": ("technology earnings", "technology startups", "antitrust technology", "global economy", "interest rates", "global trade"),
    "Science": ("scientific discovery", "physics research", "biotechnology", "medical research", "materials science", "ocean research"),
    "Energy": ("renewable energy", "battery technology", "electric vehicles", "climate science", "nuclear energy", "power grid"),
    "World": ("world news", "international diplomacy", "elections", "geopolitics", "humanitarian aid", "global supply chains"),
    "Space": ("space exploration", "NASA", "satellite technology", "astronomy", "rocket launch", "lunar mission"),
    "Society": ("public health", "education technology", "internet policy", "future of work", "transportation technology", "digital media"),
}
TOPICS = tuple((group, query) for group, queries in TOPIC_GROUPS.items() for query in queries)
STOP_WORDS = {"the", "and", "for", "with", "from", "that", "this", "into", "over", "after", "news", "new", "how", "what", "its", "are", "has"}


def words(text):
    return set(re.findall(r"[a-z0-9]{2,}", text.lower())) - STOP_WORDS


def jaccard(a, b):
    return len(a & b) / max(1, len(a | b))


def interest_tokens(topics):
    """A vocabulary of what this library already follows: topic/subtopic names and Discover tags.

    Picks whose headline overlaps these get a modest boost, so the briefing leans toward the
    reader's own subjects without ever excluding general news. Deterministic and offline."""
    toks = set()
    for topic in topics or []:
        toks |= words(topic.get("name", ""))
        for subtopic in topic.get("subtopics", []):
            toks |= words(subtopic.get("name", ""))
            for tag in subtopic.get("tags", []):
                toks |= words(tag.replace("-", " "))
    return frozenset(toks)


def feed_url(query, start, end):
    # Request calendar bounds, then enforce the exact click-time window on every item.
    search = f'{query} after:{start.date()} before:{(end + timedelta(days=1)).date()}'
    return "https://news.google.com/rss/search?" + urlencode({
        "q": search, "hl": "en-US", "gl": "US", "ceid": "US:en"})


def fetch_topic(group, query, start, end):
    raw = polite.get(feed_url(query, start, end), timeout=15)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("News feed exceeded 4 MiB")
    return parse_feed(raw, group, query, start, end)


def parse_feed(raw, group, query, start, end):
    root = ET.fromstring(raw)
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("The news source did not return an RSS feed")
    items = []
    for position, node in enumerate(root.findall("./channel/item")[:40]):
        title = (node.findtext("title") or "").strip()
        url = (node.findtext("link") or "").strip()
        source = (node.findtext("source") or "Unknown publisher").strip()
        try:
            published = parsedate_to_datetime(node.findtext("pubDate") or "")
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            published = published.astimezone(timezone.utc)
            parsed = urlsplit(url)
            if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
                continue
        except (ValueError, TypeError, OverflowError):
            continue
        if not title or not start <= published <= end:
            continue
        if title.endswith(" - " + source):
            title = title[:-(len(source) + 3)].strip()
        if not title:
            continue
        age_hours = (end - published).total_seconds() / 3600
        match = len(words(title) & words(query)) / max(1, len(words(query)))
        score = 4 * math.exp(-age_hours / 36) + 2 * match + 1 / (1 + position / 5)
        items.append({"title": title[:500], "url": url, "source": source[:200],
                      "published": published.isoformat(), "category": group,
                      "topics": [query], "score": score})
    return items


CLUSTER_SIMILARITY = 0.55   # headlines this close describe the same event


def select_briefing(candidates, interests=frozenset()):
    """Deterministic, explainable ranking: merge duplicates, boost the reader's interests,
    cluster retellings of one event, then diversify across topics and publishers.

    Pure and offline, so the unit tests exercise it directly. `interests` is a token set from
    interest_tokens(); an empty set reproduces the plain freshness-and-relevance ranking."""
    merged, titles = {}, {}
    for candidate in candidates:
        item = dict(candidate, topics=list(candidate["topics"]))
        title_key = " ".join(sorted(words(item["title"])))
        key = item["url"] if item["url"] in merged else titles.get(title_key, item["url"])
        if key in merged:
            existing = merged[key]
            topics = sorted(set(existing["topics"] + item["topics"]))
            if item["score"] > existing["score"]:
                merged[key] = item
            merged[key]["topics"] = topics
        else:
            merged[key] = item
        titles[title_key] = key

    for item in merged.values():
        hits = len(words(item["title"]) & interests)
        item["interest_hits"] = hits
        item["base_score"] = item["score"]
        item["score"] = item["score"] + 1.6 * min(1.0, hits / 2)  # capped, never dominates freshness
    pool = sorted(merged.values(), key=lambda x: (-x["score"], x["url"]))

    # Greedy single-link clustering over the score-ordered pool: each headline joins the first
    # cluster whose representative it closely matches, otherwise it starts one. Selection then
    # takes at most one story per cluster, so one big event cannot crowd out the rest.
    clusters = []  # [(representative_tokens, [items])]
    for item in pool:
        tokens = words(item["title"])
        for cid, (rep, members) in enumerate(clusters):
            if jaccard(tokens, rep) >= CLUSTER_SIMILARITY:
                members.append(item)
                item["cluster"] = cid
                break
        else:
            item["cluster"] = len(clusters)
            clusters.append((tokens, [item]))
    for rep, members in clusters:
        for item in members:
            item["related"] = len(members) - 1

    def pick(rows, limit):
        remaining, chosen, used_clusters = list(rows), [], set()
        while remaining and len(chosen) < limit:
            ranked = []
            for item in remaining:
                if item["cluster"] in used_clusters:
                    continue
                category_count = sum(x["category"] == item["category"] for x in chosen)
                source_count = sum(x["source"].casefold() == item["source"].casefold() for x in chosen)
                ranked.append((item["score"] - 1.8 * category_count - 1.4 * source_count, item))
            if not ranked:
                break
            best = max(ranked, key=lambda x: x[0])[1]
            remaining.remove(best)
            chosen.append(best)
            used_clusters.add(best["cluster"])
        return chosen

    shortlist = pick(pool, 10)
    picks = pick(shortlist, 5)
    for item in shortlist:
        item["score"] = round(item["score"], 3)
        extra = " Related coverage was grouped so one story does not crowd out the rest." if item.get("related") else ""
        focus = "topics you follow" if item.get("interest_hits") else "a mix of topics and publishers"
        item["reason"] = f"Recent coverage of {item['topics'][0]}; selected for freshness, relevance, and {focus}.{extra}"
    return {"candidate_count": len(pool), "shortlist": shortlist, "articles": picks}


# ---------------------------------------------------------------- resolve stage

def _external(url):
    """A link to a real publisher, or None for Google's own / asset hosts."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return None
    if host and not host.endswith("google.com") and not host.endswith("gstatic.com") and not host.endswith("ggpht.com"):
        return url
    return None


def _decode_google_news(url):
    """Older Google News links base64-encode the publisher URL into the path; pull it back out."""
    match = re.search(r"/(?:articles|read)/([A-Za-z0-9_\-]+)", urlsplit(url).path)
    if not match:
        return None
    seg = match.group(1)
    for padded in (seg, seg + "=" * (-len(seg) % 4)):
        try:
            blob = base64.urlsafe_b64decode(padded).decode("latin-1", "ignore")
        except (binascii.Error, ValueError):
            continue
        for found in re.findall(r"https?://[^\x00-\x20\"'\\]+", blob):
            if _external(found):
                return found
    return None


def resolve_article_url(url, fetch=polite.get):
    """Best effort: turn a Google News redirect into the publisher's own URL.

    Tries to decode it offline first, then falls back to one fetch and scraping the landing page.
    Never raises and never changes a non-Google URL; the original is always a safe return value."""
    if not _external(url):  # already a publisher link, or not http(s)
        if (urlsplit(url).hostname or "").lower().endswith("news.google.com"):
            decoded = _decode_google_news(url)
            if decoded:
                return decoded
            try:
                page = fetch(url, timeout=15, priority=True).decode("utf-8", "replace")
            except Exception:
                return url
            for href in re.findall(r'(?:href|data-n-au)="(https?://[^"]+)"', page):
                link = _external(html.unescape(href))
                if link:
                    return link
    return url


# ---------------------------------------------------------------- save stage (readable copy)

_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "blockquote", "pre", "li"}
_SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form",
              "figure", "figcaption", "button", "svg", "iframe", "template", "select", "label"}
_MAX_IMAGES = 12
_MIN_ARTICLE_CHARS = 400


class _Readable(HTMLParser):
    """A tiny readability pass over arbitrary news HTML, stdlib only (no new dependencies).

    Collects text from block elements and <img> sources, skipping chrome. Blocks found inside
    <article>/<main> win when they carry enough text, so sidebars and related-link rails drop out."""
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.skip = 0
        self.article = 0
        self.tag = None
        self.buf = []
        self.blocks = []  # (tag, value, inside_article)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("article", "main") or a.get("role") == "main":
            self.article += 1
        if tag in _SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "img":
            src = (a.get("src") or a.get("data-src") or a.get("data-lazy-src") or "").strip()
            if src and not src.startswith("data:"):
                self.blocks.append(("img", urljoin(self.base, html.unescape(src)), self.article > 0))
            return
        if tag == "br" and self.tag:
            self.buf.append(" ")
            return
        if tag in _BLOCK_TAGS:
            self._flush()
            self.tag = "h2" if tag in ("h1", "h2", "h3", "h4") else tag
            self.buf = []

    def handle_endtag(self, tag):
        if tag in ("article", "main") and self.article:
            self.article -= 1
        if tag in _SKIP_TAGS and self.skip:
            self.skip -= 1
            return
        if self.skip:
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if not self.skip and self.tag:
            self.buf.append(data)

    def _flush(self):
        if self.tag:
            text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
            if text:
                self.blocks.append((self.tag, text, self.article > 0))
            self.tag, self.buf = None, []


def _meta(page, prop):
    for pattern in (rf'<meta[^>]+property=["\']{prop}["\'][^>]+content=["\']([^"\']+)["\']',
                    rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{prop}["\']'):
        m = re.search(pattern, page, re.I)
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


def extract_readable(raw, base_url):
    """Return {"body": html_fragment, "text_len": int}. Groups bare <li> runs under <ul>."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
    reader = _Readable(base_url)
    try:
        reader.feed(text)
    except Exception:
        pass  # a truncated or malformed page still yields whatever parsed before the break
    reader._flush()
    inside = [(t, v) for t, v, ia in reader.blocks if ia]
    body_chars = sum(len(v) for t, v in inside if t != "img")
    chosen = inside if body_chars > _MIN_ARTICLE_CHARS else [(t, v) for t, v, _ in reader.blocks]

    parts, text_len, images, in_list = [], 0, 0, False
    for tag, value in chosen:
        if tag != "li" and in_list:
            parts.append("</ul>")
            in_list = False
        if tag == "img":
            if images < _MAX_IMAGES:
                parts.append(f'<img src="{html.escape(value, quote=True)}">')
                images += 1
            continue
        safe = html.escape(value)
        text_len += len(value)
        if tag == "h2":
            parts.append(f"<h2>{safe}</h2>")
        elif tag == "li":
            if not in_list:
                parts.append("<ul>")
                in_list = True
            parts.append(f"<li>{safe}</li>")
        elif tag == "blockquote":
            parts.append(f"<blockquote>{safe}</blockquote>")
        elif tag == "pre":
            parts.append(f"<pre>{safe}</pre>")
        else:
            parts.append(f"<p>{safe}</p>")
    if in_list:
        parts.append("</ul>")
    return {"body": "\n".join(parts), "text_len": text_len}


def fetch_article(url, fetch=polite.get):
    """Download a news page and return {title, image, published, body}. Raises ValueError when
    there is not enough readable text, so the caller can keep a link-only record instead."""
    raw = fetch(url, timeout=20, priority=True)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Article page is too large to read in the library")
    page = raw.decode("utf-8", "replace")
    extracted = extract_readable(raw, url)
    if extracted["text_len"] < _MIN_ARTICLE_CHARS:
        raise ValueError("Could not find readable article text on this page")
    title = _meta(page, "og:title") or _meta(page, "twitter:title")
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
        title = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip() if m else ""
    image = _meta(page, "og:image")
    return {"title": title[:500], "image": urljoin(url, image) if image else None,
            "published": _meta(page, "article:published_time"), "body": extracted["body"]}


def news_document(article, meta):
    """A content.html fragment (header + body) for the in-app reader, matching the reader's shell."""
    title = html.escape((meta.get("title") or article.get("title") or "Untitled").strip())
    source = html.escape((article.get("source") or urlsplit(article["url"]).hostname or "").strip())
    url = html.escape(article["url"], quote=True)
    date = html.escape((meta.get("published") or article.get("published") or "").strip())
    byline = " · ".join(x for x in (source, date) if x)
    header = (f'<header class="doc-head"><h1>{title}</h1>'
              f'{f"<p class=doc-byline>{byline}</p>" if byline else ""}'
              f'<p class="doc-source"><a href="{url}" target="_blank" rel="noopener noreferrer">'
              f'Open the original article</a></p></header>')
    return header + "\n" + meta["body"]


# ---------------------------------------------------------------- scan orchestration

ANTITHRASH_S = 90   # a fresh identical edition is reused instead of hammering the news source
RETRY_PAUSE_S = 0.15  # brief wait before a lone feed retry; small so a full outage still finishes fast


class BriefingService:
    """One bounded, shared scan at a time; no crawling until the user asks."""
    def __init__(self, fetcher=fetch_topic, resolver=None):
        self.fetcher = fetcher
        self.resolver = resolver  # None keeps shortlist URLs as-is (unit tests stay offline)
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.job = None
        self._done_at = 0.0
        self._done_days = None

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.job) if self.job else {"state": "idle"}

    def start(self, days=3, now=None, interests=frozenset(), force=False):
        if days not in (1, 3, 7):
            raise ValueError("Choose 1, 3, or 7 days")
        with self.lock:
            if self.job and self.job["state"] == "running":
                return self.snapshot()  # repeat clicks join the existing scan
            # A fresh edition over the same window is reused rather than re-scanned, so an accidental
            # double click (or a reopen) does not hit the news source again. `now` is set only by tests
            # and `force` by the explicit Generate button, and both bypass the reuse.
            if (now is None and not force and self.job and self.job["state"] == "complete"
                    and self._done_days == days and time.time() - self._done_at < ANTITHRASH_S):
                return self.snapshot()
            if self.stopped.is_set():
                raise RuntimeError("Briefing service is shutting down")
            end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            start = end - timedelta(days=days)
            self.job = {"id": uuid.uuid4().hex, "state": "running", "days": days,
                        "started_at": end.isoformat(), "window_start": start.isoformat(),
                        "window_end": end.isoformat(), "topics_total": len(TOPICS),
                        "topics_scanned": 0, "topics_succeeded": 0, "topics_failed": 0,
                        "topics": [q for _, q in TOPICS], "errors": [], "shortlist": [],
                        "articles": [], "candidate_count": 0, "message": "Scanning recent news"}
            threading.Thread(target=self._run, args=(start, end, frozenset(interests)),
                             daemon=True, name="morning-briefing").start()
            return self.snapshot()

    def stop(self):
        self.stopped.set()

    def _fetch_topic(self, group, query, start, end):
        """One feed, retried once: Google News occasionally drops a connection or 503s a single query,
        and a lone retry recovers it without slowing the scan or hammering a feed that is truly down."""
        last = None
        for attempt in (1, 2):
            if self.stopped.is_set():
                return []
            try:
                return self.fetcher(group, query, start, end)
            except Exception as exc:
                last = exc
                if attempt == 1 and not self.stopped.is_set():
                    time.sleep(RETRY_PAUSE_S)
        raise last

    def _resolve_shortlist(self, shortlist):
        """Swap Google News redirects for publisher URLs, bounded and best-effort. Picks are a
        subset of the shortlist, so resolving the shortlist resolves them too."""
        if not self.resolver:
            for item in shortlist:
                item.setdefault("resolved_url", item["url"])
            return
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="briefing-resolve") as pool:
            resolved = dict(zip((id(i) for i in shortlist),
                                pool.map(lambda i: self._safe_resolve(i["url"]), shortlist)))
        for item in shortlist:
            item["resolved_url"] = resolved.get(id(item)) or item["url"]

    def _safe_resolve(self, url):
        if self.stopped.is_set():
            return url
        try:
            return self.resolver(url)
        except Exception:
            return url

    def _run(self, start, end, interests=frozenset()):
        candidates = []
        try:
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="briefing-feed") as pool:
                pending = {pool.submit(self._fetch_topic, group, query, start, end): query
                           for group, query in TOPICS}
                for future in as_completed(pending):
                    if self.stopped.is_set():
                        for other in pending:
                            other.cancel()
                        break
                    try:
                        candidates.extend(future.result())
                        with self.lock:
                            self.job["topics_succeeded"] += 1
                    except Exception as exc:
                        with self.lock:
                            self.job["topics_failed"] += 1
                            self.job["errors"].append({"topic": pending[future], "error": str(exc)[:180]})
                    with self.lock:
                        self.job["topics_scanned"] += 1
            result = select_briefing(candidates, interests)
            if not self.stopped.is_set():
                self._resolve_shortlist(result["shortlist"])
            with self.lock:
                self.job.update(result)
                self.job["state"] = "cancelled" if self.stopped.is_set() else "complete" if result["articles"] else "error"
                self.job["finished_at"] = datetime.now(timezone.utc).isoformat()
                if self.job["state"] == "complete":
                    self._done_at, self._done_days = time.time(), self.job["days"]
                if self.stopped.is_set():
                    self.job["message"] = "Scan stopped when the server shut down. Generate a new briefing after restarting."
                elif not result["articles"]:
                    self.job["message"] = "No dated articles found in this window. Try a wider window or retry when the news source is available."
                elif len(result["articles"]) < 5:
                    self.job["message"] = "Fewer than five distinct recent stories were available. Showing only verified feed results."
                else:
                    self.job["message"] = "Your five reading picks are ready."
        except Exception as exc:
            with self.lock:
                self.job.update(state="error", message=f"Briefing failed: {str(exc)[:200]}")
