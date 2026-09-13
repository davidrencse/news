"""Keeps the library fresh by cycling trending Medium articles in, forever, in the background.

Each cycle takes the subtopic that was curated longest ago:
1. Candidates come from the local search index (medium_index.py): recent posts first, then the
   whole window, best-rated by Medium's sitemap first.
2. A few post pages it hasn't seen are read through polite.py (shared, self-throttling limiter),
   for the real title, author, claps, language and tags. Results are cached, so no page is read twice.
3. Posts that fit the subtopic are ranked by trend: claps weighted down by age.
4. Up to CAP auto-added articles per subtopic. Past that, a clearly better post replaces the weakest
   auto-added article you never downloaded. Articles you added yourself or downloaded are never removed.

While subtopics are below FILL_TARGET the curator cycles quickly (the limiter still spaces requests);
after that it slows to one subtopic every CYCLE_SECONDS.
"""
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
from datetime import date, datetime, timedelta, timezone

import polite

CAP = 12               # auto-added articles kept per subtopic
FILL_TARGET = 8        # below this, cycle quickly
PAGES_PER_CYCLE = 5    # post pages read per cycle
ADDS_PER_CYCLE = 3     # keeps churn gentle once a subtopic is full
CYCLE_SECONDS = 120
FAST_CYCLE_SECONDS = 3
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
    i = page.find("window.__APOLLO_STATE__")
    if i < 0:
        return None
    try:
        state, _ = json.JSONDecoder().raw_decode(page[page.find("{", i):])
    except ValueError:
        return None
    pid = url.rstrip("/").rsplit("/", 1)[-1].rsplit("-", 1)[-1]
    post = state.get(f"Post:{pid}")
    if not post:
        posts = [v for k, v in state.items() if k.startswith("Post:") and "clapCount" in v]
        post = posts[0] if len(posts) == 1 else None
    if not post:
        return None
    creator = state.get((post.get("creator") or {}).get("__ref", ""), {})
    tags = []
    for t in post.get("tags") or []:
        ref = t.get("__ref", "") if isinstance(t, dict) else ""
        tags.append(state.get(ref, {}).get("id") or ref.split(":", 1)[-1])
    image = (post.get("previewImage") or {}).get("id")
    ts = post.get("firstPublishedAt")
    return {
        "title": (post.get("title") or "").strip(),
        "claps": int(post.get("clapCount") or 0),
        "author": creator.get("name") or "",
        "published": datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat(timespec="seconds") if ts else None,
        "lang": post.get("detectedLanguage"),
        "response": bool(post.get("inResponseToPostResult")),
        "tags": [t for t in tags if t],
        "snippet": ((post.get("extendedPreviewContent") or {}).get("subtitle") or "").strip(),
        "image": f"https://miro.medium.com/v2/resize:fill:320:214/{image}" if image else None,
    }


def candidates(db, phrases, exclude, limit, since=None):
    match = " OR ".join('"%s"' % p.replace('"', "") for p in phrases)
    day_filter = "AND posts.day >= ?" if since else ""
    out = []
    for min_prio in (0.7, 0.5, 0.0):  # widen only when the best-rated posts run out
        params = [match, min_prio] + ([since] if since else [])
        rows = db.execute("SELECT posts.url FROM posts_fts JOIN posts ON posts.id = posts_fts.rowid "
                          f"WHERE posts_fts MATCH ? AND posts.prio >= ? {day_filter} "
                          "ORDER BY posts.prio DESC, posts_fts.rank LIMIT 200", params).fetchall()
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
            auto = sum(1 for a in self.store.data["articles"] if a.get("source") == "auto")
        last_key = max(st["last"], key=st["last"].get) if st["last"] else None
        return {"enabled": st["enabled"], "current": self.current, "last": last_key,
                "last_at": st["last"].get(last_key) if last_key else None,
                "added_total": st["added"], "rotated_total": st["rotated"], "auto_articles": auto,
                "pages_cached": len(self.cache), "error": self.error,
                "next_in_s": max(0, round(self.next_at - time.time())) if self.next_at else None}

    # ------------------------------------------------------------ loop
    def start(self):
        threading.Thread(target=self._run, daemon=True, name="curator").start()

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
            tid, sub, filling = pick
            self.current = f"{tid}/{sub['id']}"
            try:
                self.curate(tid, sub)
                self.error = None
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
            finally:
                self.current = None
            self._sleep(FAST_CYCLE_SECONDS if filling else CYCLE_SECONDS)

    def _next_subtopic(self):
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
        # subtopics still filling (and not exhausted today) come first, then round-robin by last visit
        def order(item):
            k = f"{item[0]}/{item[1]['id']}"
            filling = counts.get(k, 0) < FILL_TARGET and st["exhausted"].get(k) != today
            return (0 if filling else 1, st["last"].get(k, ""))
        tid, sub = min(subs, key=order)
        k = f"{tid}/{sub['id']}"
        return tid, sub, counts.get(k, 0) < FILL_TARGET and st["exhausted"].get(k) != today

    def curate(self, tid, sub):
        key, phrases = f"{tid}/{sub['id']}", phrases_for(tid, sub)
        with self.store.lock:
            have = {a["url"] for a in self.store.data["articles"]}
        since = (date.today() - timedelta(days=RECENT_DAYS)).isoformat()
        db = sqlite3.connect(f"file:{self.index.path}?mode=ro", uri=True)
        try:
            pool = candidates(db, phrases, have, 40, since) + candidates(db, phrases, have, 60)
        finally:
            db.close()
        pool = list(dict.fromkeys(pool))

        unread = [u for u in pool if u not in self.cache][:PAGES_PER_CYCLE]
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

        added = rotated = 0
        for url, m in good:
            if added + rotated >= ADDS_PER_CYCLE:
                break
            with self.store.lock:
                if any(a["url"] == url for a in self.store.data["articles"]):
                    continue
                auto = [a for a in self.store.data["articles"]
                        if a["topic"] == tid and a["subtopic"] == sub["id"] and a.get("source") == "auto"]
            fields = {"url": url, "topic": tid, "subtopic": sub["id"], "title": m["title"], "author": m["author"],
                      "snippet": m["snippet"], "image": m["image"], "published": m["published"],
                      "claps": m["claps"], "source": "auto"}
            if len(auto) < CAP:
                self.add_article(fields)
                added += 1
                continue
            replaceable = [a for a in auto if not a.get("pdf")]
            weakest = min(replaceable, key=lambda a: trend_score(a.get("claps"), a.get("published")), default=None)
            if weakest and trend_score(m["claps"], m["published"]) > 1.2 * trend_score(weakest.get("claps"), weakest.get("published")):
                self.remove_article(weakest)
                self.add_article(fields)
                rotated += 1
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
