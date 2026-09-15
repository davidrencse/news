"""Keeps the library fresh by cycling trending Medium articles in, forever, in the background.

Each cycle takes the subtopic that was curated longest ago:
1. Candidates come from the local search index (medium_index.py): recent posts first, then the
   whole window, best-rated by Medium's sitemap first.
2. A few post pages it hasn't seen are read through polite.py (shared, self-throttling limiter),
   for the real title, author, claps, language and tags. Results are cached, so no page is read twice.
3. Posts that fit the subtopic are ranked by trend: claps weighted down by age.
4. Each topic aims for TOPIC_TARGET articles, shared by its subtopics (at least CAP each). Past its
   share, a clearly better post replaces the weakest auto-added article you never downloaded.
   Articles you added yourself or downloaded are never removed.

Filling a topic to its target is a long background job: every post page goes through polite.py's
limiter, so the curator reads about one page a second no matter how many it wants. The target is a
ceiling it walks towards over days of uptime, not something the app fetches up front.

While subtopics are below their share the curator cycles quickly (the limiter still spaces requests);
after that it slows to one subtopic every CYCLE_SECONDS. A second thread records whether each library
article is member-only (paywalled) or free.
"""
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.error
from datetime import date, datetime, timedelta, timezone

import medium_render
import polite

CAP = 12               # minimum auto-added articles kept per subtopic
TOPIC_TARGET = 1120    # each topic aims for at least this many; its subtopics share the target
PAGES_PER_CYCLE = 5    # post pages read per cycle
ADDS_PER_CYCLE = 3     # keeps churn gentle once a subtopic is full
CYCLE_SECONDS = 120
FAST_CYCLE_SECONDS = 3
POOL_SIZE = 100        # candidate URLs considered per cycle
SCAN_LIMIT = 4000      # index rows looked at per priority tier; a full subtopic has excluded most of them
RECENT_DAYS = 14
MIN_INDEX_DAYS = 7

ENGLISH = re.compile(r"\b(the|a|an|to|of|and|for|in|on|how|why|what|with|your|is|you|i|my|from|guide|using|vs|"
                     r"when|this|are|it|we|can|do|not|new|best|about|into|will|should)\b")

# Search phrases for subtopics whose tag names are too thin or too ambiguous on their own.
QUERIES = {
    "cybersecurity/blue-teaming": ["blue team", "soc analyst", "incident response", "siem", "detection engineering"],
    "artificial-intelligence/research": ["ai research", "research paper", "arxiv", "paper explained", "paper review"],
    "computer-science/computer-architecture": ["computer architecture", "cpu architecture", "risc v", "instruction set", "cache memory", "arm architecture"],
    "computer-science/networking": ["computer networking", "tcp", "network protocol", "dns", "bgp", "osi model"],
    "computer-science/theory": ["computer science", "theory of computation", "turing", "complexity theory", "p vs np"],
    "software-engineering/performance": ["performance optimization", "performance tuning", "latency", "profiling", "benchmark"],
    "engineering/electrical-engineering": ["electrical engineering", "circuit", "power electronics", "pcb", "power grid"],
    "engineering/embedded-systems": ["embedded systems", "microcontroller", "arduino", "esp32", "raspberry pi", "firmware", "rtos"],
    "engineering/semiconductors": ["semiconductors", "semiconductor", "transistor", "chip design"],
    "engineering/telecommunications": ["telecommunications", "5g", "6g", "telecom", "satellite internet", "starlink"],
    "engineering/mechanical-engineering": ["mechanical engineering", "cad", "solidworks", "3d printing", "engineering design", "thermodynamics"],
    "engineering/aerospace": ["aerospace", "aviation", "spacex", "rocket", "aircraft", "boeing", "airbus"],
    "hardware/fabrication": ["tsmc", "asml", "euv", "semiconductor manufacturing", "chip manufacturing", "foundry", "wafer"],
    "hardware/hardware-news": ["intel", "amd", "tech news", "qualcomm", "apple silicon"],
    "industry/semiconductors": ["semiconductor industry", "semiconductors", "chip war", "tsmc", "chipmakers"],
    "industry/industrial-policy": ["industrial policy", "chips act", "reshoring", "inflation reduction act", "made in china 2025", "subsidies"],
    "industry/energy": ["energy", "renewable energy", "oil", "solar", "power grid"],
    "geopolitics/trade": ["trade war", "tariffs", "global trade", "free trade"],
    "geopolitics/technology-policy": ["tech policy", "technology policy", "ai regulation", "eu ai act", "ai act", "antitrust", "tech regulation"],
    "business/venture-capital": ["venture capital", "vc funding", "venture capitalist", "seed funding", "series a", "fundraising"],
    "business/markets": ["stock market", "investing", "stocks", "s p 500"],
    "business/strategy": ["business strategy", "competitive strategy", "strategy"],
    "science/materials-science": ["materials science", "graphene", "superconductor", "battery technology", "metamaterials"],
    "science/energy": ["nuclear energy", "fusion", "nuclear power", "energy"],
    "science/research": ["scientific research", "scientists", "study finds", "new research"],
    "science/space": ["space exploration", "astronomy", "nasa", "spacex", "black hole", "exoplanet"],
}

# Broad Medium tags per topic: a post with one of these plus a subtopic phrase in its title fits.
TOPIC_TAGS = {
    "cybersecurity": {"cybersecurity", "security", "infosec", "hacking", "cyber-security", "information-security", "ethical-hacking", "bug-bounty", "penetration-testing"},
    "artificial-intelligence": {"artificial-intelligence", "ai", "machine-learning", "llm", "generative-ai", "deep-learning", "chatgpt", "openai", "data-science", "agentic-ai"},
    "computer-science": {"computer-science", "programming", "software-engineering", "algorithms", "coding"},
    "software-engineering": {"software-engineering", "programming", "software-development", "devops", "web-development", "coding", "technology"},
    "engineering": {"engineering", "electronics", "hardware", "technology", "robotics"},
    "hardware": {"hardware", "technology", "semiconductors", "nvidia", "gpu", "computers", "tech"},
    "industry": {"business", "economics", "manufacturing", "supply-chain", "energy", "technology"},
    "geopolitics": {"politics", "geopolitics", "world", "international-relations", "china", "russia", "economy", "war"},
    "business": {"business", "startup", "entrepreneurship", "economics", "finance", "investing", "money", "leadership"},
    "science": {"science", "physics", "space", "mathematics", "research", "technology"},
}


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def phrases_for(tid, sub):
    return QUERIES.get(f"{tid}/{sub['id']}") or [t.replace("-", " ") for t in sub.get("tags", [])] or [sub["name"].lower()]


def fits(meta, tid, sub, phrases):
    tags = set(meta["tags"])
    if tags & (set(sub.get("tags", [])) | {slug(p) for p in phrases}):
        return True
    title = meta["title"].lower()
    return bool(tags & TOPIC_TAGS.get(tid, set())) and any(re.search(r"\b%s\b" % re.escape(p), title) for p in phrases)


def trend_score(claps, published):
    """Claps weighted down by age, so fresh popular posts can beat older ones."""
    age = 30
    if published:
        try:
            age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(published)).days)
        except ValueError:
            pass
    return (claps or 0) / (age + 3) ** 0.8


def post_meta(url):
    """Title, claps, author, language and tags from a post page's embedded Apollo state."""
    try:
        page = polite.get_text(url)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return None
        raise
    state, post = medium_render.parse_post(page, url)
    return medium_render.meta_of(state, post) if post else None


def candidates(db, phrases, exclude, limit, since=None, scan=SCAN_LIMIT):
    """`limit` indexed post URLs matching any of `phrases` and not already in `exclude`.

    `scan` is how far down the ranking to look. It has to grow with the library: a subtopic holding a
    thousand articles has already taken most of the best-rated rows, so a fixed window would come back
    empty and the subtopic would look exhausted while plenty of candidates remain below it.
    """
    match = " OR ".join('"%s"' % p.replace('"', "") for p in phrases)
    day_filter = "AND posts.day >= ?" if since else ""
    out = []
    for min_prio in (0.7, 0.5, 0.0):  # widen only when the best-rated posts run out
        params = [match, min_prio] + ([since] if since else []) + [scan]
        rows = db.execute("SELECT posts.url FROM posts_fts JOIN posts ON posts.id = posts_fts.rowid "
                          f"WHERE posts_fts MATCH ? AND posts.prio >= ? {day_filter} "
                          "ORDER BY posts.prio DESC, posts_fts.rank LIMIT ?", params).fetchall()
        for (url,) in rows:
            words = url.rsplit("/", 1)[-1].replace("-", " ")
            if url in exclude or url in out or re.search(r"[^\x00-\x7f]", url) or not ENGLISH.search(words):
                continue
            out.append(url)
            if len(out) >= limit:
                return out
    return out


class MetaCache:
    """Post-page metadata by URL; None marks a page that couldn't be read or parsed."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self._data = json.load(f)

    def __contains__(self, url):
        return url in self._data

    def get(self, url):
        return self._data.get(url)

    def set(self, url, meta):
        with self._lock:
            self._data[url] = meta

    def __len__(self):
        return len(self._data)

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f)
            os.replace(tmp, self.path)


class Curator:
    def __init__(self, index, store, add_article, remove_article):
        """index: MediumIndex. store: app Store (data + lock + save). add_article(fields) and
        remove_article(article) change the library the same way the API does."""
        self.index = index
        self.store = store
        self.add_article = add_article
        self.remove_article = remove_article
        self.cache = MetaCache(os.path.join(os.path.dirname(index.path), "curation-cache.json"))
        self.current = None
        self.error = None
        self.next_at = None
        self.backfill_left = None
        self._wake = threading.Event()
        self._stopping = False

    # ------------------------------------------------------------ state kept in library.json
    def _state(self):
        with self.store.lock:
            st = self.store.data.setdefault("curation", {})
            st.setdefault("enabled", True)
            st.setdefault("last", {})
            st.setdefault("exhausted", {})
            st.setdefault("added", 0)
            st.setdefault("rotated", 0)
            return st

    @property
    def enabled(self):
        return self._state()["enabled"]

    def set_enabled(self, on):
        with self.store.lock:
            self._state()["enabled"] = bool(on)
            self.store.save()
        self._wake.set()

    def status(self):
        st = self._state()
        with self.store.lock:
            articles = self.store.data["articles"]
            auto = sum(1 for a in articles if a.get("source") == "auto")
            member_only = sum(1 for a in articles if a.get("locked") is True)
            free = sum(1 for a in articles if a.get("locked") is False)
        last_key = max(st["last"], key=st["last"].get) if st["last"] else None
        return {"enabled": st["enabled"], "current": self.current, "last": last_key,
                "last_at": st["last"].get(last_key) if last_key else None,
                "added_total": st["added"], "rotated_total": st["rotated"], "auto_articles": auto,
                "member_only": member_only, "free": free, "backfill_left": self.backfill_left,
                "pages_cached": len(self.cache), "error": self.error,
                "next_in_s": max(0, round(self.next_at - time.time())) if self.next_at else None}

    # ------------------------------------------------------------ loop
    def start(self):
        threading.Thread(target=self._guard(self._run), daemon=True, name="curator").start()
        threading.Thread(target=self._guard(self._backfill), daemon=True, name="paywall-check").start()

    def _guard(self, fn):
        """Keep a background thread alive. An unexpected error restarts the loop instead of silently
        ending it for the rest of the session — these threads are the library's only way to grow."""
        def run():
            while not self._stopping:
                try:
                    fn()
                    return  # returned on its own: it is stopping
                except Exception as e:
                    self.error = f"{fn.__name__} restarted after {type(e).__name__}: {e}"
                    time.sleep(30)
        return run

    def stop(self):
        self._stopping = True
        self._wake.set()

    def _sleep(self, seconds):
        self.next_at = time.time() + seconds
        self._wake.wait(seconds)
        self._wake.clear()
        self.next_at = None

    def _run(self):
        self._sleep(15)
        while not self._stopping:
            if not self.enabled:
                self._sleep(3600)
                continue
            if self.index.status()["days_indexed"] < MIN_INDEX_DAYS:
                self._sleep(60)
                continue
            medium = polite.status().get("medium.com", {})
            if medium.get("waiting_s", 0) > 30:  # Medium asked us to back off; don't queue behind it
                self._sleep(medium["waiting_s"])
                continue
            pick = self._next_subtopic()
            if not pick:
                self._sleep(CYCLE_SECONDS)
                continue
            tid, sub, filling, cap = pick
            self.current = f"{tid}/{sub['id']}"
            try:
                self.curate(tid, sub, cap, filling)
                self.error = None
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
            finally:
                self.current = None
            self._sleep(FAST_CYCLE_SECONDS if filling else CYCLE_SECONDS)

    def _backfill(self):
        """Record paywall status (and fresh claps) for library articles that don't have it yet.

        Uses cached page data when it already says, otherwise reads the post page through the shared
        limiter. Pages Medium refuses are marked unknown and retried the next day."""
        time.sleep(20)
        changed, todo = 0, []
        while not self._stopping:
            today = date.today().isoformat()
            if not todo:  # rescanning a library of tens of thousands per article would dominate this loop
                with self.store.lock:
                    todo = [a for a in self.store.data["articles"]
                            if "locked" not in a or (a["locked"] is None and a.get("locked_checked") != today)]
                self.backfill_left = len(todo)
            if not todo:
                if changed:
                    self.store.save()
                    self.cache.save()
                    changed = 0
                time.sleep(300)
                continue
            wait = polite.status().get("medium.com", {}).get("waiting_s", 0)
            if wait > 30:  # Medium asked us to back off
                time.sleep(wait)
                continue
            a = todo[-1]  # oldest first; the list is newest-first
            meta = self.cache.get(a["url"])
            if not (meta and "locked" in meta):
                try:
                    meta = post_meta(a["url"])
                except urllib.error.HTTPError as e:
                    if e.code in (429, 503):
                        # the limiter has slowed down; wait out its pause rather than spinning on this one
                        time.sleep(max(5, polite.status().get("medium.com", {}).get("waiting_s", 0)))
                        continue
                    meta = None
                except Exception:
                    meta = None
                if meta:
                    self.cache.set(a["url"], meta)
            with self.store.lock:
                a["locked"] = meta["locked"] if meta else None
                a["locked_checked"] = today
                if meta and a.get("claps") is not None:
                    a["claps"] = max(a["claps"], meta["claps"])
            todo.pop()
            self.backfill_left = len(todo)
            changed += 1
            if changed >= 20:
                self.store.save()
                self.cache.save()
                changed = 0

    def _next_subtopic(self):
        """(tid, sub, filling, cap) for the subtopic to visit next.

        Each topic aims for TOPIC_TARGET articles. Subtopics that ran out of candidates today keep what
        they have; the other subtopics in that topic share the rest (never less than CAP each)."""
        st = self._state()
        today = date.today().isoformat()
        with self.store.lock:
            subs = [(t["id"], s) for t in self.store.data["topics"] for s in t["subtopics"]
                    if t["id"] != "custom" or s.get("tags")]
            counts = {}
            for a in self.store.data["articles"]:
                if a.get("source") == "auto":
                    k = f"{a['topic']}/{a['subtopic']}"
                    counts[k] = counts.get(k, 0) + 1
        if not subs:
            return None
        exhausted = lambda k: st["exhausted"].get(k) == today
        caps = {}
        for tid in {t for t, _ in subs}:
            keys = [f"{tid}/{s['id']}" for t, s in subs if t == tid]
            settled = sum(counts.get(k, 0) for k in keys if exhausted(k))
            active = [k for k in keys if not exhausted(k)] or keys
            share = math.ceil(max(0, TOPIC_TARGET - settled) / len(active))
            for k in keys:
                caps[k] = max(CAP, share)

        topic_totals = {}
        for k, n in counts.items():
            topic_totals[k.split("/")[0]] = topic_totals.get(k.split("/")[0], 0) + n

        # subtopics still filling come first, from the topic furthest below its target; then round-robin
        def order(item):
            k = f"{item[0]}/{item[1]['id']}"
            if counts.get(k, 0) < caps[k] and not exhausted(k):
                return (0, topic_totals.get(item[0], 0), st["last"].get(k, ""))
            return (1, 0, st["last"].get(k, ""))
        tid, sub = min(subs, key=order)
        k = f"{tid}/{sub['id']}"
        return tid, sub, counts.get(k, 0) < caps[k] and not exhausted(k), caps[k]

    def curate(self, tid, sub, cap=CAP, filling=False):
        key, phrases = f"{tid}/{sub['id']}", phrases_for(tid, sub)
        with self.store.lock:
            have = {a["url"] for a in self.store.data["articles"]}
            auto = [a for a in self.store.data["articles"]
                    if a["topic"] == tid and a["subtopic"] == sub["id"] and a.get("source") == "auto"]
        since = (date.today() - timedelta(days=RECENT_DAYS)).isoformat()
        # look further down the ranking the more this subtopic already holds; see candidates()
        scan = SCAN_LIMIT + 8 * len(auto)
        db = sqlite3.connect(f"file:{self.index.path}?mode=ro", uri=True)
        try:
            recent = POOL_SIZE * 2 // 5
            pool = (candidates(db, phrases, have, recent, since, scan)
                    + candidates(db, phrases, have, POOL_SIZE - recent, None, scan))
        finally:
            db.close()
        pool = list(dict.fromkeys(pool))

        unread = [u for u in pool if u not in self.cache][:PAGES_PER_CYCLE * (4 if filling else 1)]
        for url in unread:
            try:
                self.cache.set(url, post_meta(url))
            except urllib.error.HTTPError as e:
                self.cache.set(url, None)
                if e.code in (429, 503):
                    break  # the limiter has slowed down; finish this cycle with what we have
            except Exception:
                continue
        if unread:
            self.cache.save()

        good = [(u, m) for u in pool if (m := self.cache.get(u)) and m["title"] and m["lang"] in ("en", None)
                and not m["response"] and fits(m, tid, sub, phrases)]
        good.sort(key=lambda x: trend_score(x[1]["claps"], x[1]["published"]), reverse=True)

        def take(url, fields):
            """Add one post to the subtopic. add_article normalizes the URL, so what comes back can
            turn out to be an article we already hold; only a genuinely new one counts."""
            a = self.add_article(fields)
            have.update((url, a["url"]))
            if any(x is a for x in auto):
                return False
            auto.append(a)
            return True

        def drop(a):
            for i, x in enumerate(auto):  # by identity: two articles can compare equal by value
                if x is a:
                    del auto[i]
                    break
            self.remove_article(a)

        added = rotated = 0
        for url, m in good:
            if not filling and added + rotated >= ADDS_PER_CYCLE:
                break
            if url in have:
                continue
            fields = {"url": url, "topic": tid, "subtopic": sub["id"], "title": m["title"], "author": m["author"],
                      "snippet": m["snippet"], "image": m["image"], "published": m["published"],
                      "claps": m["claps"], "source": "auto", "locked": m.get("locked")}
            if len(auto) < cap:
                added += take(url, fields)
                continue
            replaceable = [a for a in auto if not a.get("pdf")]
            weakest = min(replaceable, key=lambda a: trend_score(a.get("claps"), a.get("published")), default=None)
            if weakest and trend_score(m["claps"], m["published"]) > 1.2 * trend_score(weakest.get("claps"), weakest.get("published")):
                drop(weakest)
                rotated += take(url, fields)
            else:
                break  # everything after this ranks lower still

        with self.store.lock:
            st = self._state()
            st["last"][key] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            st["added"] += added
            st["rotated"] += rotated
            if not unread and not added:
                st["exhausted"][key] = date.today().isoformat()
            self.store.save()
        return added, rotated, len(unread)
