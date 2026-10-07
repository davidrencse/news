"""Library of Babel — local server.

Run:  .venv\\Scripts\\python app.py   then open http://127.0.0.1:8765
"""
import asyncio
import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel

import polite
import paper_sources
from briefing import (BriefingService, interest_tokens, news_document, resolve_article_url)
from briefing import fetch_article as fetch_news_article  # app.py already has a fetch_article route
from curator import Curator
from medium_index import MediumIndex
from performance import MAX_ACTIVE_FETCHES
from pipeline import PdfPipeline, PipelineError, localize_images
from recommender import Recommender
from topics import default_topics
from telegram_storage import RemoteFiles, safe_relative
from remote_library import (remote_store_type, RemotePapers, RemoteIndex,
                            remote_curator_type)

ROOT = os.path.dirname(os.path.abspath(__file__))
FALLBACK_STORAGE = r"E:\storage"
LOW_SPACE_GB = 10


def choose_data_dir():
    """Where PDFs, library.json and the search index live.

    MEDIUM_LIBRARY_DATA overrides everything. Otherwise data stays next to the app until that
    drive has less than LOW_SPACE_GB free; then it is moved to E:\\storage once and stays there.
    """
    if os.environ.get("MEDIUM_LIBRARY_DATA"):
        return os.environ["MEDIUM_LIBRARY_DATA"]
    marker = os.path.join(ROOT, "data-location.txt")  # makes a move permanent
    if os.path.exists(marker):
        with open(marker, encoding="utf-8") as f:
            return f.read().strip() or ROOT
    if shutil.disk_usage(ROOT).free / 1e9 >= LOW_SPACE_GB or not os.path.isdir(FALLBACK_STORAGE):
        return ROOT
    target = os.path.join(FALLBACK_STORAGE, "medium-library")
    os.makedirs(target, exist_ok=True)
    for name in ("Medium-Library", "search-index", "notes", "Papers"):
        src, dst = os.path.join(ROOT, name), os.path.join(target, name)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.move(src, dst)
    with open(marker, "w", encoding="utf-8") as f:
        f.write(target)
    print(f"\n  Low disk space next to the app: moved library data to {target}")
    return target


STORAGE_MODE = os.environ.get("MEDIUM_LIBRARY_STORAGE", "telegram")
if STORAGE_MODE not in ("telegram", "local"):
    raise RuntimeError("MEDIUM_LIBRARY_STORAGE must be telegram or local")
REMOTE = RemoteFiles() if STORAGE_MODE == "telegram" else None
# A virtual path only; remote mode never calls choose_data_dir or creates these directories.
DATA_DIR = "__telegram_only__" if REMOTE else choose_data_dir()
LIBRARY_DIR = os.path.join(DATA_DIR, "Medium-Library")
DB_PATH = os.path.join(LIBRARY_DIR, "library.json")
PAPER_DB_PATH = os.path.join(DATA_DIR, "Papers", "papers.json")
STATIC_DIR = os.path.join(ROOT, "static")
NOTES_DIR = os.path.join(DATA_DIR, "notes")  # highlights + notes per article, kept apart from downloads
FEED_TTL = 15 * 60


# ---------------------------------------------------------------- storage

SAVE_DEBOUNCE = 1.5  # seconds; the curator saves thousands of times, the file is written once per burst


class Store:
    """library.json plus in-memory indexes by id and url.

    The library grows to tens of thousands of articles, so nothing here may be linear per write:
    lookups go through dicts, and save() marks the file dirty for a writer thread that rewrites it at
    most every SAVE_DEBOUNCE seconds (atomically, via a temp file). flush() forces the write out.
    """

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self._write_lock = threading.Lock()  # one writer at a time; see flush()
        self.version = 0  # bumps on every save so the UI knows when to reload
        self._dirty = False
        self._stopping = False
        self._wake = threading.Event()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.data = self._load(path)
        self.data.setdefault("topics", default_topics())
        self.data.setdefault("articles", [])
        self.data["articles"] = [a for a in self.data["articles"]
                                 if isinstance(a, dict) and a.get("id") and a.get("url")]
        for a in self.data["articles"]:
            if a.get("claps") is not None and "source" not in a:
                a["source"] = "auto"  # added by the curator before articles recorded their source
            a.pop("fetching", None)  # no download survives a restart, so none of these flags are live
        self.merge_default_topics()
        self.reindex()
        threading.Thread(target=self._writer, daemon=True, name="library-writer").start()
        self.flush()

    def _load(self, path):
        """The library, or an empty one if the file can't be read.

        A half-written or damaged library.json must not stop the app from starting. The unreadable
        file is kept next to it as library.json.broken-<time> so nothing is quietly thrown away.
        """
        if not os.path.exists(path):
            self._dirty = True
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("library.json is not an object")
            return data
        except Exception as e:
            kept = f"{path}.broken-{int(time.time())}"
            try:
                shutil.copy2(path, kept)
            except Exception:
                kept = "(could not be copied)"
            print(f"\n  {os.path.basename(path)} could not be read ({type(e).__name__}: {e}).\n"
                  f"  Starting with an empty library; the old file is kept at {kept}\n")
            self._dirty = True
            return {}

    # -------------------------------------------------- indexes
    def reindex(self):
        with self.lock:
            self._by_id = {a["id"]: a for a in self.data["articles"]}
            self._by_url = {a["url"]: a for a in self.data["articles"]}

    def merge_default_topics(self):
        """Add topics and subtopics introduced in topics.py since this library was created.

        Existing entries keep their name and tags (the user may have edited them); nothing is removed,
        so a topic dropped from the defaults keeps the articles already filed under it.
        """
        by_id = {t["id"]: t for t in self.data["topics"]}
        for default in default_topics():
            t = by_id.get(default["id"])
            if not t:
                self.data["topics"].append(default)
                self._dirty = True
                continue
            have = {s["id"] for s in t["subtopics"]}
            for s in default["subtopics"]:
                if s["id"] not in have:
                    t["subtopics"].append(s)
                    self._dirty = True

    # -------------------------------------------------- persistence
    def save(self):
        """Mark the library changed. The writer thread puts it on disk within SAVE_DEBOUNCE seconds."""
        with self.lock:
            self.version += 1
            self._dirty = True
        self._wake.set()

    def flush(self):
        """Write the library out now, if it changed.

        The write lock is held across the whole write, so flush() never returns while another thread's
        write is still in flight: when it returns, everything saved before the call is on disk.
        """
        with self._write_lock:
            with self.lock:
                if not self._dirty:
                    return
                # Compact separators, not indent=2: for a large library this cuts serialize time to about
                # a quarter and the file by a third (≈5.1 s/190 MB → 1.4 s/126 MB at 520k articles), which
                # matters because this runs under store.lock and blocks readers for its duration. The file
                # is machine-written and machine-read, so human-readable indentation buys nothing.
                blob = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
                self._dirty = False
            tmp = f"{self.path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(blob)
            os.replace(tmp, self.path)

    def _writer(self):
        while not self._stopping:
            self._wake.wait(SAVE_DEBOUNCE)
            self._wake.clear()
            try:
                self.flush()
            except Exception as e:  # a failed write must not kill the thread; the next one retries
                print(f"  could not write {self.path}: {type(e).__name__}: {e}")
                with self.lock:
                    self._dirty = True
                time.sleep(5)

    def stop(self):
        self._stopping = True
        self._wake.set()
        self.flush()

    # -------------------------------------------------- lookups
    def topic(self, tid):
        return next((t for t in self.data["topics"] if t["id"] == tid), None)

    def subtopic(self, tid, sid):
        t = self.topic(tid)
        return t and next((s for s in t["subtopics"] if s["id"] == sid), None)

    def article(self, aid):
        return self._by_id.get(aid)

    def by_url(self, url):
        return self._by_url.get(url)

    def add(self, a):
        with self.lock:
            self.data["articles"].insert(0, a)
            self._by_id[a["id"]] = a
            self._by_url[a["url"]] = a
            self.save()

    def discard(self, a):
        """Remove exactly this article. False if it isn't in the library (already removed, or a copy
        of a row rather than the row itself), so callers don't delete files that are still in use."""
        with self.lock:
            if self._by_id.get(a["id"]) is not a:
                return False
            del self._by_id[a["id"]]
            self._by_url.pop(a["url"], None)
            for i, x in enumerate(self.data["articles"]):  # by identity, not value
                if x is a:
                    del self.data["articles"][i]
                    break
            self.save()
            return True


store = remote_store_type(Store, REMOTE)(DB_PATH) if REMOTE else Store(DB_PATH)


class PaperStore:
    """Separate durable store for research-paper records; article schema remains unchanged."""
    def __init__(self, path):
        self.path, self.lock = path, threading.RLock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("Paper store is not a list")
            self.records = {x["id"]: x for x in data if isinstance(x, dict) and x.get("id")}
        except FileNotFoundError:
            self.records = {}
        except (OSError, ValueError, TypeError) as exc:
            backup = f"{path}.broken-{int(time.time())}"
            try:
                shutil.copy2(path, backup)
                print(f"  Paper store could not be read ({type(exc).__name__}: {exc}); kept a copy at {backup}")
            except OSError:
                print(f"  Paper store could not be read ({type(exc).__name__}: {exc})")
            self.records = {}

    def all(self):
        with self.lock:
            return sorted((json.loads(json.dumps(x)) for x in self.records.values()),
                          key=lambda x: x.get("saved_at") or x.get("published") or "", reverse=True)

    def _write(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(self.records.values()), f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def upsert(self, record):
        with self.lock:
            self.records[record["id"]] = record
            self._write()
            return json.loads(json.dumps(record))

    def upsert_many(self, records):
        with self.lock:
            for record in records:
                self.records[record["id"]] = record
            self._write()
            return len(records)


paper_store = RemotePapers(REMOTE) if REMOTE else PaperStore(PAPER_DB_PATH)
briefing_service = BriefingService(resolver=resolve_article_url)
pipeline = None if REMOTE else PdfPipeline(STATIC_DIR)
jobs: dict[str, dict] = {}
_tasks: set[asyncio.Task] = set()  # the event loop only keeps weak references to running tasks
JOB_TTL = 3600                     # finished jobs the UI may still poll for
feed_cache: dict[str, tuple[float, list]] = {}


# ---------------------------------------------------------------- helpers

def slugify(s, limit=70):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:limit].rstrip("-") or "untitled"


def normalize_url(url):
    """A clean https URL, or a 400 saying why not.

    Without the host check, a typo ("not a url") or another scheme ("file:///etc/passwd") became
    "https://not a url" and was saved as a perfectly real-looking article that could never load.
    """
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        if re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
            raise HTTPException(400, "Only http and https links can be saved.")
        url = "https://" + url
    p = urlsplit(url)
    # credentials are dropped rather than kept: no article link needs them, and library.json would
    # be storing a password in plain text
    hostport = p.netloc.rsplit("@", 1)[-1]
    host = hostport.rsplit(":", 1)[0]
    if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host, re.I):
        raise HTTPException(400, "That doesn't look like a link to an article.")
    return urlunsplit((p.scheme.lower(), hostport.lower(), p.path.rstrip("/"), "", ""))


def article_id(url):
    return hashlib.sha1(url.encode()).hexdigest()[:12]


def guess_title(url):
    last = urlsplit(url).path.rstrip("/").split("/")[-1]
    last = re.sub(r"-[0-9a-f]{8,14}$", "", last)  # Medium appends a hex post id
    return last.replace("-", " ").strip().capitalize() or url


def pdf_rel_path(a):
    """Where PDFs lived before articles got their own folder (still used for older downloads)."""
    return f"{a['topic']}/{a['subtopic']}/{slugify(a['title'])}-{a['id'][:8]}.pdf"


def folder_rel(a):
    """An article's folder: content.html (reader), images/, article.pdf (single page)."""
    return f"{a['topic']}/{a['subtopic']}/{slugify(a['title'])}-{a['id'][:8]}"


def notes_path(aid):
    return os.path.join(NOTES_DIR, re.sub(r"[^0-9a-f]", "", aid) + ".json")


def abs_path(rel):
    return os.path.join(LIBRARY_DIR, *rel.split("/"))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_exists_cache: dict[str, tuple[float, bool]] = {}
EXISTS_TTL = 30  # seconds; /api/library would otherwise stat two files per article on every poll


def downloaded(rel):
    """Does this article's file exist, cached briefly. Only downloads change these, and a download
    rewrites the article's row (with a fresh path) anyway, so a stale 'yes' can't outlive the file by
    more than EXISTS_TTL. The cache matters most in Telegram mode: there each check is a synchronous
    round-trip over the TGFS pipe, serialized through one lock, and a library page runs one per row —
    so an uncached /api/library poll would fire dozens of pipe calls every time (see EXISTS_TTL)."""
    if not rel:
        return False
    hit = _exists_cache.get(rel)
    now = time.time()
    if hit and now - hit[0] < EXISTS_TTL:
        return hit[1]
    if REMOTE:
        try:
            ok = REMOTE.exists("Medium-Library/" + safe_relative(rel))
        except ValueError:
            return False
    else:
        ok = os.path.exists(abs_path(rel))
    if len(_exists_cache) > 100_000:  # a whole library's worth of stale paths, after moves and removals
        _exists_cache.clear()
    _exists_cache[rel] = (now, ok)
    return ok


def warm_downloaded_cache(articles):
    """Prime _exists_cache for a whole page in one TGFS round-trip, so public() over the rows does not
    fire one serialized pipe call per downloaded row. Telegram mode only; degrades silently to the
    per-row checks in downloaded() on any error or count mismatch."""
    if not REMOTE:
        return
    now = time.time()
    want = []
    for a in articles:
        for rel in (a.get("doc"), a.get("pdf")):
            if rel and not ((hit := _exists_cache.get(rel)) and now - hit[0] < EXISTS_TTL):
                want.append(rel)
    want = list(dict.fromkeys(want))  # dedupe, keep order
    pairs = []
    for rel in want:
        try:
            pairs.append((rel, "Medium-Library/" + safe_relative(rel)))
        except ValueError:
            pass  # leave invalid paths to downloaded(), which returns False for them
    if not pairs:
        return
    try:
        results = REMOTE.exists_many([key for _, key in pairs])
    except Exception:
        return
    if len(results) != len(pairs):
        return
    if len(_exists_cache) > 100_000:
        _exists_cache.clear()
    for (rel, _), ok in zip(pairs, results):
        _exists_cache[rel] = (now, bool(ok))


def public(a):
    out = dict(a)
    out["pdf_url"] = f"/files/{a['pdf']}" if downloaded(a.get("pdf")) else None
    out["doc_url"] = f"/files/{a['doc']}" if downloaded(a.get("doc")) else None
    return out


# ---------------------------------------------------------------- RSS discovery

NS = {"dc": "http://purl.org/dc/elements/1.1/", "content": "http://purl.org/rss/1.0/modules/content/"}


def fetch_tag_feed(tag):
    hit = feed_cache.get(tag)
    if hit and time.time() - hit[0] < FEED_TTL:
        return hit[1]
    root = ET.fromstring(polite.get(f"https://medium.com/feed/tag/{tag}", timeout=20, priority=True))
    items = []
    for it in root.iter("item"):
        link = it.findtext("link") or ""
        desc = it.findtext("description") or ""
        img = re.search(r'<img[^>]+src="([^"]+)"', desc)
        snip = re.search(r'<p class="medium-feed-snippet">(.*?)</p>', desc, re.S)
        pub = it.findtext("pubDate")
        try:
            published = parsedate_to_datetime(pub).isoformat() if pub else None
        except Exception:
            published = None
        try:
            url = normalize_url(link)
        except HTTPException:  # one odd <link> shouldn't sink the whole tag
            continue
        items.append({
            "url": url,
            "title": html.unescape((it.findtext("title") or "").strip()),
            "author": it.findtext("dc:creator", namespaces=NS) or "",
            "published": published,
            "image": img.group(1) if img else None,
            "snippet": html.unescape(re.sub(r"<[^>]+>", "", snip.group(1))).strip() if snip else "",
            "categories": [c.text for c in it.findall("category") if c.text],
            "tag": tag,
        })
    feed_cache[tag] = (time.time(), items)
    return items


# ---------------------------------------------------------------- Medium-wide search
# Our own search engine: medium_index.py crawls Medium's public post sitemaps (one per day)
# into a local SQLite full-text index in the background. While the index is still young,
# live tag feeds top up the results.

PAGE_SIZE = 20
STOPWORDS = {"a", "an", "and", "the", "of", "for", "to", "in", "on", "with", "how", "what", "why", "is", "are", "vs", "or", "by"}
medium_index = RemoteIndex() if REMOTE else MediumIndex(os.path.join(DATA_DIR, "search-index", "medium.db"), default_days=90)


def search_feeds(q, max_tags=6):
    words = [w for w in re.findall(r"[a-z0-9]+", q.lower()) if w not in STOPWORDS]
    if not words:
        return []
    # candidate tags: the whole phrase, adjacent pairs, then single words
    tags = list(dict.fromkeys(["-".join(words)] + ["-".join(words[i:i + 2]) for i in range(len(words) - 1)] + words))[:max_tags]
    pool = {}
    for tag in tags:  # polite.py spaces out the requests
        try:
            for it in fetch_tag_feed(tag):
                pool.setdefault(it["url"], it)
        except Exception:
            continue  # tag doesn't exist or feed failed

    def score(it):
        hay = f"{it['title']} {it['snippet']} {' '.join(it['categories'])}".lower()
        return sum(w in hay for w in words)

    ranked = sorted(pool.values(), key=lambda it: (score(it), it["published"] or ""), reverse=True)
    return [it for it in ranked if score(it)]


def enrich(item):
    """Fill a search hit out with real metadata when the curator has already read that post.

    The index only knows what a URL reveals - a title made from the slug, and the day it was
    published. For the few thousand posts the curator has opened we know the real title, the
    subtitle, the image, the clap count and whether it is member-only, which turns a bare link
    into the same card the rest of the app shows.
    """
    m = curator.cache.get(item["url"])
    if not m or not m.get("title"):
        return item
    return dict(item, title=m["title"], snippet=m.get("snippet") or "", image=m.get("image"),
                author=m.get("author") or item.get("author") or "", claps=m.get("claps"),
                reading_time=m.get("reading_time"), locked=m.get("locked"),
                published=m.get("published") or item.get("published"))


def web_search(q, offset=0):
    """Returns {items, next, provider, notice, index}: one page from the local index,
    topped up with live tag-feed matches on the first page while the index is thin."""
    try:
        taste = recommender.word_profile()
    except Exception:
        taste = None  # search is the app's backbone; it works with or without knowing the reader
    res = medium_index.search(q, PAGE_SIZE, offset, taste=taste)
    found = res["items"]
    items, provider = [enrich(it) for it in found], "index"
    st = medium_index.status()
    # Each live tag feed is a rate-limited request to Medium (seconds, not milliseconds), so only
    # reach for them while the index is young or when it has nothing at all for this query.
    young = st["days_indexed"] < min(30, st["days_setting"])
    if offset == 0 and (not found or (young and len(found) < PAGE_SIZE)):
        seen = {i["url"] for i in found}
        extra = [it for it in search_feeds(q, 6 if young else 3) if it["url"] not in seen][:PAGE_SIZE]
        items += extra
        if extra and not found:
            provider = "feeds"
    notice = None
    if REMOTE:
        notice = "Telegram storage mode: live feed search only; the full search index stays in Telegram."
    elif young:
        notice = (f"Your Medium search index is still being built: {st['posts']:,} articles from "
                  f"{st['days_indexed']} days so far. Results improve as it keeps crawling in the background.")
    elif res["mode"] != "all" and found:
        # The net was widened, but say so only when this page really does show a partial match:
        # widening often still fills the page with titles that have every word.
        missing = sorted({w for it in found for w in it.get("missing") or []})
        if missing:
            notice = ("Not enough titles contain all of your words, so the results lower down are "
                      f"missing some of them ({', '.join(missing[:4])}).")
    nxt = {"offset": offset + PAGE_SIZE} if res["more"] else None
    return {"items": items, "next": nxt, "provider": provider, "notice": notice, "index": st,
            "parsed": res["parsed"]}


# ---------------------------------------------------------------- app

@asynccontextmanager
async def lifespan(_app):
    medium_index.start()
    curator.start()
    yield
    if REMOTE and _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
    curator.stop()
    medium_index.stop()
    briefing_service.stop()
    if pipeline:
        pipeline.close()
    await asyncio.to_thread(store.stop)
    if REMOTE:
        await asyncio.to_thread(REMOTE.close)


app = FastAPI(title="Library of Babel", lifespan=lifespan)


class BriefingIn(BaseModel):
    days: int = 3
    force: bool = False  # the explicit Generate button regenerates instead of reusing a fresh edition


class BriefingSaveIn(BaseModel):
    url: str
    title: str | None = None
    source: str | None = None
    published: str | None = None
    snippet: str | None = None


BRIEFING_TOPIC, BRIEFING_SUBTOPIC = "custom", "morning-briefing"
_briefing_saving: set[str] = set()  # article ids whose readable copy is being written right now


@app.get("/api/briefing")
def briefing_status():
    return briefing_service.snapshot()


@app.post("/api/briefing", status_code=202)
def create_briefing(body: BriefingIn):
    with store.lock:
        interests = interest_tokens(store.data["topics"])
    try:
        return briefing_service.start(body.days, interests=interests, force=body.force)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


async def _write_readable_copy(a):
    """Fetch the publisher page, extract a readable copy, and swap it into the article's folder.
    Mirrors run_job's safe write: build in an .incoming- folder, then replace atomically."""
    if REMOTE:
        meta = await asyncio.to_thread(fetch_news_article, a["url"])
        content = news_document(a, meta)
        await asyncio.to_thread(save_remote_document, a, meta, content)
        return
    incoming = abs_path(f"{a['topic']}/{a['subtopic']}/.incoming-{a['id'][:8]}")

    def build():
        shutil.rmtree(incoming, ignore_errors=True)
        os.makedirs(incoming, exist_ok=True)
        meta = fetch_news_article(a["url"])
        content = localize_images(news_document(a, meta), incoming)
        with open(os.path.join(incoming, "content.html"), "w", encoding="utf-8") as f:
            f.write(content)
        return meta

    meta = await asyncio.to_thread(build)
    with store.lock:
        if store.article(a["id"]) is not a:
            shutil.rmtree(incoming, ignore_errors=True)
            raise PipelineError("This story was removed from the library while it was being saved.")
        if meta.get("title"):
            a["title"] = meta["title"]
        a["image"] = a.get("image") or meta.get("image")
        rel = folder_rel(a)
        if a.get("doc"):
            shutil.rmtree(abs_path(posixpath.dirname(a["doc"])), ignore_errors=True)
        shutil.rmtree(abs_path(rel), ignore_errors=True)
        os.makedirs(os.path.dirname(abs_path(rel)), exist_ok=True)
        os.replace(incoming, abs_path(rel))
        a["doc"], a["fetched"] = f"{rel}/content.html", now_iso()
        _exists_cache.pop(a["doc"], None)
        store.save()


def remote_article_content(a):
    """Render sanitized HTML in memory; do not launch a disk-backed Chromium profile."""
    import medium_render
    from pipeline import FREEDIUM_MIRRORS
    route = medium_render.prepare(a['url'])
    if route['route'] == 'medium':
        meta, body = route['meta'], route['body']
    else:
        meta = None
        problems = []
        for base in FREEDIUM_MIRRORS:
            try:
                meta = fetch_news_article(base.rstrip('/') + '/' + a['url'])
                break
            except Exception as exc:
                problems.append(f"{urlsplit(base).hostname}: {polite.describe_error(exc)}")
                continue
        if meta is None:
            raise PipelineError((route.get('reason') or 'Direct article download failed') + '. '
                                + 'Fallbacks failed: ' + '; '.join(problems)
                                + '. No article was uploaded. Open the original article to check access.')
        body = meta['body']
    meta = dict(meta, route=route['route'], reason=route.get('reason', 'free story'))
    return meta, medium_render.render_header(meta, a['url'], body, route['route']) + '\n' + body


def save_remote_document(a, meta, content):
    from pipeline import IMG_SRC, MAGIC
    # Versioned keys avoid exposing half-replaced content if the final metadata save fails.
    rel = 'remote-articles/' + a['id'] + '/' + uuid.uuid4().hex
    images = {}
    for i, url in enumerate(dict.fromkeys(html.unescape(u) for u in IMG_SRC.findall(content))):
        try:
            # max_bytes caps the read itself; an oversized body raises instead of being read into RAM.
            data = polite.get(url, timeout=30, priority=True, max_bytes=20 * 1024 * 1024)
            ext = next((ext for magic, ext in MAGIC if data.startswith(magic)), None)
            if ext not in ('.png', '.jpg', '.gif', '.webp'):
                continue
            image_path = f'images/{i:03d}{ext}'
            REMOTE.write('Medium-Library/' + rel + '/' + image_path, data)
            images[url] = image_path
        except Exception:
            continue
    # An unavailable image is omitted, so source servers cannot create a browser disk cache.
    content = IMG_SRC.sub(lambda m: '<img src="' + images.get(html.unescape(m.group(1)), '') + '"', content)
    REMOTE.write('Medium-Library/' + rel + '/content.html', content.encode())
    with store.lock:
        if store.article(a['id']) is not a:
            raise PipelineError('This article was removed while it was being saved.')
        a.update(title=meta.get('title') or a['title'], author=meta.get('author') or a.get('author', ''),
                 doc=rel + '/content.html', fetched=now_iso(), via=meta.get('route'))
        store.save()
    store.flush()


@app.post("/api/briefing/save")
async def save_briefing(body: BriefingSaveIn):
    # resolve_article_url may fetch to unwrap a Google News redirect; keep that off the event loop.
    # normalize_url stays inline (pure, fast) so its 400 for a bad URL propagates as usual.
    real = await asyncio.to_thread(resolve_article_url, normalize_url(body.url))
    a = create_article({"url": real, "title": body.title, "source": "briefing",
                        "snippet": body.snippet or "", "published": body.published,
                        "topic": BRIEFING_TOPIC, "subtopic": BRIEFING_SUBTOPIC, "locked": False})
    if downloaded(a.get("doc")):
        return {"article": public(a), "fetched": True}
    with store.lock:
        if a["id"] in _briefing_saving:
            return {"article": public(a), "fetched": False, "error": "This story is already being saved."}
        _briefing_saving.add(a["id"])
    try:
        await _write_readable_copy(a)
        return {"article": public(a), "fetched": True}
    except Exception as exc:
        # The article stays in the library as a link to the original even when extraction fails.
        return {"article": public(a), "fetched": False, "error": str(exc)[:200]}
    finally:
        with store.lock:
            _briefing_saving.discard(a["id"])


class TopicIn(BaseModel):
    name: str
    parent: str | None = None  # topic id; None => goes under "custom"
    tags: list[str] | None = None


class ArticleIn(BaseModel):
    url: str
    topic: str
    subtopic: str
    title: str | None = None
    author: str | None = None
    snippet: str | None = None
    image: str | None = None
    published: str | None = None
    claps: int | None = None
    source: str | None = None  # "auto" when the curator added it
    locked: bool | None = None  # True = member-only, when the caller already checked


class MoveIn(BaseModel):
    topic: str
    subtopic: str


@app.get("/api/library")
def get_library():
    with store.lock:  # the curator threads edit article dicts; copy a consistent snapshot
        topics = json.loads(json.dumps(store.data["topics"]))
        articles = [dict(a) for a in store.data["articles"]]
        version = store.version
    # FastAPI's generic encoder recursively walks every field of every article. A large
    # link-only library can contain tens of thousands of rows, making that walk dominate
    # the request. These values are already JSON-compatible, so serialize them once.
    warm_downloaded_cache(articles)  # one TGFS round-trip instead of one per downloaded row
    rows = []
    for article in articles:
        row = public(article)
        if article.get("source") == "bulk":
            # Index imports have no body, image, or paywall data yet. Omitting their
            # empty fields saves several MB without changing how the UI reads them.
            row = {key: value for key, value in row.items() if value is not None and value != ""}
        rows.append(row)
    payload = {"topics": topics, "articles": rows, "version": version}
    return Response(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    media_type="application/json")


# A browse session requests many small pages. Build counts and scope lists once for each Store
# version so page two and later are O(page size), instead of recounting the full library each time.
_library_page_cache_lock = threading.Lock()
_library_page_cache = None


def _library_page_index():
    global _library_page_cache
    with _library_page_cache_lock:
        with store.lock:
            version = store.version
            if _library_page_cache and _library_page_cache["version"] == version:
                return _library_page_cache
            topics = json.loads(json.dumps(store.data["topics"]))
            articles = [dict(a) for a in store.data["articles"]]

        def bucket():
            return {"total": 0, "free": 0, "locked": 0, "noted": 0, "subtopics": {}}

        counts = {"total": 0, "free": 0, "locked": 0, "noted": 0, "topics": {}}
        scopes = {("", ""): {"all": [], "free": [], "locked": []}}
        for topic in topics:
            tid = topic["id"]
            counts["topics"][tid] = bucket()
            scopes[(tid, "")] = {"all": [], "free": [], "locked": []}
            for subtopic in topic["subtopics"]:
                sid = subtopic["id"]
                counts["topics"][tid]["subtopics"][sid] = bucket()
                scopes[(tid, sid)] = {"all": [], "free": [], "locked": []}

        for article in articles:
            tid, sid = article.get("topic", ""), article.get("subtopic", "")
            if tid not in counts["topics"]:
                counts["topics"][tid] = bucket()
                scopes[(tid, "")] = {"all": [], "free": [], "locked": []}
            topic_counts = counts["topics"][tid]
            if sid not in topic_counts["subtopics"]:
                topic_counts["subtopics"][sid] = bucket()
                scopes[(tid, sid)] = {"all": [], "free": [], "locked": []}
            sub_counts = topic_counts["subtopics"][sid]
            for target in (counts, topic_counts, sub_counts):
                target["total"] += 1
                if article.get("locked") is False:
                    target["free"] += 1
                elif article.get("locked") is True:
                    target["locked"] += 1
                if article.get("notes_count"):
                    target["noted"] += 1
            for scope_key in (("", ""), (tid, ""), (tid, sid)):
                scope = scopes[scope_key]
                scope["all"].append(article)
                if article.get("locked") is False:
                    scope["free"].append(article)
                elif article.get("locked") is True:
                    scope["locked"].append(article)

        _library_page_cache = {"version": version, "topics": topics, "counts": counts, "scopes": scopes}
        return _library_page_cache


@app.get("/api/library/first")
def get_library_first_page(limit: int = 60, topic_id: str = "", subtopic_id: str = "", access: str = "all"):
    """Return the first screen without waiting for the full-library counts index."""
    limit = max(1, min(limit, 100))
    access = access if access in ("all", "free", "locked") else "all"
    if subtopic_id and not topic_id:
        raise HTTPException(400, "A topic is required with a subtopic.")
    with store.lock:
        topics = json.loads(json.dumps(store.data["topics"]))
        version = store.version
        selected = []
        for article in store.data["articles"]:
            if topic_id and article.get("topic") != topic_id:
                continue
            if subtopic_id and article.get("subtopic") != subtopic_id:
                continue
            if access == "free" and article.get("locked") is not False:
                continue
            if access == "locked" and article.get("locked") is not True:
                continue
            selected.append(dict(article))
            if len(selected) >= limit:
                break
    warm_downloaded_cache(selected)  # one TGFS round-trip instead of one per downloaded row
    rows = []
    for article in selected:
        row = public(article)
        if article.get("source") == "bulk":
            row = {key: value for key, value in row.items() if value is not None and value != ""}
        rows.append(row)
    return {"topics": topics, "articles": rows, "version": version}


@app.get("/api/library/page")
def get_library_page(offset: int = 0, limit: int = 60, topic_id: str = "", subtopic_id: str = "", access: str = "all"):
    """Small browse payload backed by a versioned index of counts and article scopes."""
    offset = max(0, min(offset, 1_000_000))
    limit = max(1, min(limit, 100))
    access = access if access in ("all", "free", "locked") else "all"
    if subtopic_id and not topic_id:
        raise HTTPException(400, "A topic is required with a subtopic.")
    index = _library_page_index()
    topics, counts, version = index["topics"], index["counts"], index["version"]
    scope = index["scopes"].get((topic_id, subtopic_id), {"all": [], "free": [], "locked": []})
    selected_scope = scope[access]
    total = len(selected_scope)
    selected = [dict(article) for article in selected_scope[offset:offset + limit]]

    warm_downloaded_cache(selected)  # one TGFS round-trip instead of one per downloaded row
    rows = []
    for article in selected:
        row = public(article)
        if article.get("source") == "bulk":
            row = {key: value for key, value in row.items() if value is not None and value != ""}
        rows.append(row)
    return {"topics": topics, "articles": rows, "counts": counts, "total": total,
            "offset": offset, "limit": limit, "version": version}


@app.get("/api/library/search")
def search_saved_library(q: str = "", limit: int = 20):
    """Search saved article metadata without sending the full library to the browser."""
    q = q.strip().lower()
    if not q:
        return {"items": [], "total": 0}
    limit = max(1, min(limit, 100))
    phrases, plain, minus = [], [], []
    for token in re.findall(r'-?"[^\"]*"|\S+', q):
        neg = token.startswith("-")
        body = (token[1:] if neg else token).strip('"')
        words = re.findall(r"[a-z0-9]+", body)
        if neg:
            minus.extend(words)
        elif len(words) > 1 and token.lstrip("-").startswith('"'):
            phrases.append(" ".join(words))
        else:
            plain.extend(words)
    words = [*plain, *(word for phrase in phrases for word in phrase.split())]
    if not words:
        return {"items": [], "total": 0}
    with store.lock:
        articles = tuple(store.data["articles"])
    scored = []
    for article in articles:
        title = (article.get("title") or "").lower()
        rest = " ".join((article.get("author") or "", article.get("snippet") or "",
                         article.get("topic") or "", article.get("subtopic") or "")).lower()
        if any(word in title or word in rest for word in minus):
            continue
        if any(phrase not in title and phrase not in rest for phrase in phrases):
            continue
        in_title = sum(word in title for word in words)
        anywhere = sum(word in title or word in rest for word in words)
        if not anywhere:
            continue
        order = [title.find(word) for word in plain if word in title]
        span = max(order) - min(order) + 1 if len(order) > 1 else 1
        score = (2.5 * anywhere / len(words) + 1.2 * in_title / len(words)
                 + 0.6 * (len(order) / span if len(order) > 1 else 0)
                 + 0.5 * sum(phrase in title for phrase in phrases)
                 + 0.3 * bool(article.get("fetched")))
        scored.append((score, article.get("added") or "", article))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    top = [article for _, _, article in scored[:limit]]
    warm_downloaded_cache(top)  # one TGFS round-trip instead of one per downloaded row
    return {"items": [public(article) for article in top], "total": len(scored)}


@app.post("/api/topics")
def create_topic(body: TopicIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Name is required.")
    parent = store.topic(body.parent or "custom")
    if not parent:
        raise HTTPException(404, "Parent topic not found.")
    sid = slugify(name, 50)
    if any(s["id"] == sid for s in parent["subtopics"]):
        raise HTTPException(409, f"'{name}' already exists in {parent['name']}.")
    tags = [slugify(t, 60) for t in (body.tags or []) if t.strip()] or [sid]
    sub = {"id": sid, "name": name, "tags": tags, "custom": True}
    with store.lock:
        parent["subtopics"].append(sub)
        store.save()
    if not REMOTE:
        os.makedirs(os.path.join(LIBRARY_DIR, parent["id"], sid), exist_ok=True)
    return {"topic": parent["id"], "subtopic": sub}


@app.patch("/api/topics/{tid}/{sid}")
def update_subtopic_tags(tid: str, sid: str, body: TopicIn):
    sub = store.subtopic(tid, sid)
    if not sub:
        raise HTTPException(404, "Subtopic not found.")
    with store.lock:
        sub["name"] = body.name.strip() or sub["name"]
        if body.tags is not None:
            sub["tags"] = [slugify(t, 60) for t in body.tags if t.strip()]
        store.save()
    return sub


@app.delete("/api/topics/{tid}/{sid}")
def delete_subtopic(tid: str, sid: str):
    t, sub = store.topic(tid), store.subtopic(tid, sid)
    if not sub:
        raise HTTPException(404, "Subtopic not found.")
    if not sub.get("custom"):
        raise HTTPException(400, "Built-in subtopics can't be deleted.")
    with store.lock:
        t["subtopics"].remove(sub)
        # the subtopic's articles go with it, files included, so nothing is left orphaned on disk
        for a in [a for a in store.data["articles"] if a["topic"] == tid and a["subtopic"] == sid]:
            remove_article(a)
        store.save()
    folder = os.path.join(LIBRARY_DIR, tid, sid)
    if not REMOTE and os.path.isdir(folder) and not os.listdir(folder):
        os.rmdir(folder)
    return {"ok": True}


@app.get("/api/discover/{tid}/{sid}")
async def discover(tid: str, sid: str):
    sub = store.subtopic(tid, sid)
    if not sub:
        raise HTTPException(404, "Subtopic not found.")
    def fetch_all():
        out = []
        for tag in sub["tags"]:  # sequential; polite.py spaces out the requests
            try:
                out.append((tag, fetch_tag_feed(tag)))
            except Exception as e:
                msg = "Medium is rate-limiting requests, try again in a minute" if "429" in str(e) else str(e)
                out.append((tag, RuntimeError(msg)))
        return out

    seen, items, errors = set(), [], []
    for tag, res in await asyncio.to_thread(fetch_all):
        if isinstance(res, Exception):
            errors.append(f"{tag}: {res}")
            continue
        for it in res:
            if it["url"] not in seen:
                seen.add(it["url"])
                items.append(it)
    items.sort(key=lambda i: i["published"] or "", reverse=True)
    for it in items:
        it["saved"] = store.by_url(it["url"]) is not None
        it["id"] = article_id(it["url"])
    return {"tags": sub["tags"], "items": items, "errors": errors}


@app.get("/api/discover")
async def discover_all():
    """Trending posts across every topic that aren't saved yet (from the curator's page cache; no network)."""
    items = await asyncio.to_thread(curator.suggestions)
    return {"items": [dict(it, id=article_id(it["url"])) for it in items]}


@app.get("/api/recommend")
async def recommend(labels: str = "", access: str = "all", limit: int = 60):
    """Posts picked for you from what you've read. labels: comma-separated Medium tags to narrow to."""
    include_locked = {"free": False, "locked": True}.get(access)
    res = await asyncio.to_thread(recommender.recommend, labels.split(","), max(1, min(limit, 300)), include_locked)
    res["items"] = [dict(it, id=article_id(it["url"]), saved=store.by_url(it["url"]) is not None)
                     for it in res["items"]]
    res["labels"] = await asyncio.to_thread(recommender.all_labels)
    return res


class DismissIn(BaseModel):
    url: str
    undo: bool = False


@app.post("/api/recommend/dismiss")
async def dismiss(body: DismissIn):
    """'Not for me': the post stops being suggested, and posts like it rank lower."""
    url = normalize_url(body.url.strip())
    if not url:
        raise HTTPException(400, "Which post?")
    await asyncio.to_thread(recommender.dismiss, url, body.undo)
    return {"ok": True, "dismissed": not body.undo}


@app.post("/api/articles/{aid}/read")
def mark_read(aid: str):
    """The reader opened this article; recommendations learn from it."""
    a = store.article(aid)
    if not a:
        raise HTTPException(404, "Article not found.")
    with store.lock:
        a["read_at"] = now_iso()
        store.save()
    return {"ok": True}


@app.get("/api/search")
async def search_medium(q: str = "", next: str | None = None):
    """Search all of Medium. Returns links only; nothing is downloaded until an article is opened."""
    q = q.strip()
    if not q:
        raise HTTPException(400, "Type something to search for.")
    try:
        # OverflowError: a token carrying 1e400 parses as inf, which int() refuses
        offset = int(json.loads(next)["offset"]) if next else 0
    except (ValueError, KeyError, TypeError, OverflowError):
        raise HTTPException(400, "Bad paging token.")
    offset = max(0, min(offset, 100_000))
    res = await asyncio.to_thread(web_search, q, offset)
    items = [dict(it, id=article_id(it["url"]), saved=store.by_url(it["url"]) is not None) for it in res["items"]]
    return {"q": q, "items": items, "next": json.dumps(res["next"]) if res["next"] else None,
            "provider": res["provider"], "notice": res["notice"], "index": res["index"],
            "parsed": res["parsed"]}


class PaperImportIn(BaseModel):
    doi: str


@app.get("/api/papers")
def list_papers():
    # The UI never uses the full Crossref record; drop any legacy `raw` blob so the shelf payload
    # stays small (it was over half the bytes) and the view renders quickly even with many papers.
    items = paper_store.all()
    for item in items:
        item.pop("raw", None)
    return {"items": items}


@app.post("/api/papers/import")
async def import_paper(body: PaperImportIn):
    """Resolve a DOI through Crossref and upsert it into the research-paper shelf."""
    try:
        record = await asyncio.to_thread(paper_sources.enrich, body.doi)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except (RuntimeError, OSError):  # Crossref 5xx/429 (RuntimeError) or a network failure (URLError/timeout)
        raise HTTPException(502, "Crossref is unavailable right now. Try again in a moment.")
    record["saved_at"] = now_iso()
    # Offload the Telegram write like search_import does; upsert() blocks on a pipe round-trip and
    # must not run on the event loop, or one DOI import freezes every other request during the upload.
    return await asyncio.to_thread(paper_store.upsert, record)


class PaperSearchIn(BaseModel):
    query: str
    rows: int = 50


@app.post("/api/papers/search_import")
async def search_import_papers(body: PaperSearchIn):
    """Add every Crossref match for a topic query to the shelf in one durable write."""
    try:
        records = await asyncio.to_thread(paper_sources.search, body.query, body.rows)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except (RuntimeError, OSError):  # Crossref 5xx/429 or a network failure
        raise HTTPException(502, "Crossref is unavailable right now. Try again in a moment.")
    if not records:
        raise HTTPException(404, "Crossref returned no papers for that topic. Try different words.")
    now = now_iso()
    for record in records:
        record["saved_at"] = now
    added = await asyncio.to_thread(paper_store.upsert_many, records)
    items = paper_store.all()
    for item in items:
        item.pop("raw", None)
    return {"added": added, "items": items}


@app.delete("/api/papers/{paper_id}")
def remove_paper(paper_id: str):
    with paper_store.lock:
        removed = paper_store.records.pop(paper_id, None)
        if not removed:
            raise HTTPException(404, "Paper not found in your library.")
        if REMOTE:
            try:
                paper_store.persist()
            except Exception:
                paper_store.records[paper_id] = removed
                raise
            return {"ok": True}
        tmp = paper_store.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(paper_store.records.values()), f, ensure_ascii=False, indent=2)
        os.replace(tmp, paper_store.path)
    return {"ok": True}


class IndexSettings(BaseModel):
    days: int | None = None
    paused: bool | None = None
    curator_enabled: bool | None = None
    goal_free: int | None = None    # bulk fill: extra free articles to add (0 with goal_locked=0 cancels)
    goal_locked: int | None = None  # bulk fill: extra member-only articles to add


def full_status():
    return {**medium_index.status(), "curator": curator.status(), "network": polite.status(),
            "library_version": store.version, "articles": len(store.data["articles"])}


@app.get("/api/index")
def index_status():
    return full_status()


@app.post("/api/index")
def index_settings(body: IndexSettings):
    if REMOTE:
        raise HTTPException(409, "Local indexing and automatic curation are disabled in Telegram storage mode.")
    if body.days is not None:
        medium_index.set_days(body.days)
    if body.paused is not None:
        medium_index.set_paused(body.paused)
    if body.curator_enabled is not None:
        curator.set_enabled(body.curator_enabled)
    if body.goal_free is not None or body.goal_locked is not None:
        curator.set_goal(body.goal_free or 0, body.goal_locked or 0)
    return full_status()


def create_article(fields):
    """Add an article, or return the one already saved with that URL. Used by the API and the curator."""
    url = normalize_url(fields["url"])
    with store.lock:
        existing = store.by_url(url)
        if existing:
            return existing
        a = {
            "id": article_id(url), "url": url,
            "title": (fields.get("title") or "").strip() or guess_title(url),
            "author": fields.get("author") or "", "snippet": fields.get("snippet") or "", "image": fields.get("image"),
            "published": fields.get("published"), "claps": fields.get("claps"),
            "topic": fields["topic"], "subtopic": fields["subtopic"],
            "added": now_iso(), "pdf": None, "fetched": None,
        }
        if fields.get("source"):
            a["source"] = fields["source"]
        if fields.get("locked") is not None:
            a["locked"] = bool(fields["locked"])  # True = member-only (paywalled)
        store.add(a)
    return a


def downloading(aid):
    return any(j["article_id"] == aid and j["status"] == "running" for j in list(jobs.values()))


def remove_article(a):
    """Drop an article and its files. Returns False while it is downloading or already removed."""
    if downloading(a["id"]):
        return False
    if not store.discard(a):
        return False
    if REMOTE:
        # Keep archived content recoverable; the persisted tombstone removes it from the library.
        return True
    if a.get("doc"):
        shutil.rmtree(abs_path(posixpath.dirname(a["doc"])), ignore_errors=True)
    elif a.get("pdf") and os.path.exists(abs_path(a["pdf"])):
        os.remove(abs_path(a["pdf"]))
    if os.path.exists(notes_path(a["id"])):
        os.remove(notes_path(a["id"]))
    return True


@app.post("/api/articles")
def add_article(body: ArticleIn):
    if not store.subtopic(body.topic, body.subtopic):
        raise HTTPException(404, "Subtopic not found.")
    return public(create_article(body.model_dump()))


@app.patch("/api/articles/{aid}")
def move_article(aid: str, body: MoveIn):
    a = store.article(aid)
    if not a:
        raise HTTPException(404, "Article not found.")
    if not store.subtopic(body.topic, body.subtopic):
        raise HTTPException(404, "Subtopic not found.")
    if downloading(aid):
        raise HTTPException(409, "This article is downloading right now. Move it when it finishes.")
    with store.lock:
        old_pdf, old_doc = a.get("pdf"), a.get("doc")
        a["topic"], a["subtopic"] = body.topic, body.subtopic
        if REMOTE:
            pass  # Physical remote keys stay stable when a story changes topic.
        elif old_doc:
            old_dir, new_rel = abs_path(posixpath.dirname(old_doc)), folder_rel(a)
            if os.path.isdir(old_dir) and old_dir != abs_path(new_rel):
                os.makedirs(os.path.dirname(abs_path(new_rel)), exist_ok=True)
                shutil.move(old_dir, abs_path(new_rel))
            a["doc"], a["pdf"] = f"{new_rel}/content.html", f"{new_rel}/article.pdf"
        elif old_pdf and os.path.exists(abs_path(old_pdf)):
            new = pdf_rel_path(a)
            os.makedirs(os.path.dirname(abs_path(new)), exist_ok=True)
            shutil.move(abs_path(old_pdf), abs_path(new))
            a["pdf"] = new
        store.save()
    return public(a)


@app.delete("/api/articles/{aid}")
def delete_article(aid: str):
    a = store.article(aid)
    if not a:
        raise HTTPException(404, "Article not found.")
    if not remove_article(a):
        raise HTTPException(409, "This article is downloading right now. Try again when it finishes.")
    return {"ok": True}


def prune_jobs():
    """Drop finished jobs the UI has had its chance to read, so a long session doesn't grow forever."""
    cutoff = time.time() - JOB_TTL
    for jid in [j["id"] for j in list(jobs.values()) if j["status"] != "running" and j["started"] < cutoff]:
        jobs.pop(jid, None)


async def run_job(job, a):
    def stage(s, **info):
        job["stage"] = s
        job.update(info)  # route ("medium" | "freedium"), reason, freedium_url
        job.setdefault("timings", {})[s] = round(time.time() - job["started"], 1)  # seconds since start

    if REMOTE:
        try:
            stage("fetching")
            meta, content = await asyncio.to_thread(remote_article_content, a)
            stage("uploading")
            await asyncio.to_thread(save_remote_document, a, meta, content)
            job.update(status="done", stage="done", article=public(a))
        except Exception as exc:
            job.update(status="error", error=str(exc))
        finally:
            with store.lock:
                a.pop("fetching", None)
        return

    # render into a scratch folder, then swap it in under the article's real title
    incoming = abs_path(f"{a['topic']}/{a['subtopic']}/.incoming-{a['id'][:8]}")
    try:
        shutil.rmtree(incoming, ignore_errors=True)
        meta = await pipeline.render(a["url"], incoming, stage)
        with store.lock:
            # The article can be deleted, or rotated out by the curator, while it is downloading.
            # Writing its files in anyway would leave a folder nothing points at, and tell the reader
            # the download worked when the article is gone.
            if store.article(a["id"]) is not a:
                raise PipelineError("This article was removed from the library while it was downloading.")
            if meta.get("title"):
                a["title"] = meta["title"]
            rel = folder_rel(a)
            if a.get("doc"):
                shutil.rmtree(abs_path(posixpath.dirname(a["doc"])), ignore_errors=True)
            elif a.get("pdf") and os.path.isfile(abs_path(a["pdf"])):
                os.remove(abs_path(a["pdf"]))  # a download from before articles had folders
            shutil.rmtree(abs_path(rel), ignore_errors=True)
            os.makedirs(os.path.dirname(abs_path(rel)), exist_ok=True)  # the article may have been moved
            os.replace(incoming, abs_path(rel))
            a["author"] = meta.get("author") or a["author"]
            a["snippet"] = a["snippet"] or meta.get("subtitle", "")
            a["doc"], a["pdf"], a["fetched"] = f"{rel}/content.html", f"{rel}/article.pdf", now_iso()
            _exists_cache.pop(a["doc"], None)   # these files just changed
            _exists_cache.pop(a["pdf"], None)
            a["via"], a["via_reason"] = meta.get("route"), meta.get("reason")
            if meta.get("route") == "medium":
                a["locked"] = False
            elif meta.get("reason") in ("member-only story", "paywalled (HTTP 402)"):
                a["locked"] = True
            store.save()
        job.setdefault("timings", {})["done"] = round(time.time() - job["started"], 1)
        job.update(status="done", stage="done", article=public(a))
    except PipelineError as e:
        job.update(status="error", error=str(e))
    except Exception as e:
        job.update(status="error", error=f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(incoming, ignore_errors=True)
        with store.lock:
            a.pop("fetching", None)


@app.post("/api/articles/{aid}/fetch")
async def fetch_article(aid: str, force: bool = False):
    a = store.article(aid)
    if not a:
        raise HTTPException(404, "Article not found.")
    if not force and (downloaded(a.get("doc")) or downloaded(a.get("pdf"))):
        return {"id": None, "status": "done", "stage": "done", "article": public(a)}
    running = next((j for j in jobs.values() if j["article_id"] == aid and j["status"] == "running"), None)
    if running:
        return running
    prune_jobs()
    # Admission control: only MAX_PARALLEL render at once and the rest wait, so without a cap a burst of
    # distinct requests would pile up unbounded tasks, job records and article references. Deduplicated
    # re-requests above never reach here, so retrying a download already in flight is unaffected.
    if sum(1 for j in jobs.values() if j["status"] == "running") >= MAX_ACTIVE_FETCHES:
        raise HTTPException(503, "Too many downloads are in progress. Let some finish, then try again.")
    job = {"id": uuid.uuid4().hex[:10], "article_id": aid, "status": "running", "stage": "queued", "started": time.time()}
    jobs[job["id"]] = job
    with store.lock:
        a["fetching"] = True
    task = asyncio.create_task(run_job(job, a))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job


class NotesIn(BaseModel):
    notes: str = ""
    summary: str = ""  # whole-article summary (drafted in the app or pasted from ChatGPT)
    highlights: list[dict] = []


def notes_count(data):
    return len(data.get("highlights") or []) + sum(1 for k in ("notes", "summary") if (data.get(k) or "").strip())


def read_notes(aid):
    if REMOTE:
        try:
            return json.loads(REMOTE.read("notes/" + re.sub(r"[^0-9a-f]", "", aid) + ".json"))
        except FileNotFoundError:
            return {"notes": "", "summary": "", "highlights": []}
    path = notes_path(aid)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {"notes": data.get("notes") or "", "summary": data.get("summary") or "",
                "highlights": data.get("highlights") or []}
    return {"notes": "", "summary": "", "highlights": []}


@app.get("/api/articles/{aid}/notes")
def get_notes(aid: str):
    if not store.article(aid):
        raise HTTPException(404, "Article not found.")
    return read_notes(aid)


@app.get("/api/notebook")
def notebook():
    """Every article with a summary, notes or highlights, most recently edited first."""
    with store.lock:
        articles = {a["id"]: a for a in store.data["articles"]}
    entries = []
    if REMOTE:
        for item in REMOTE.list("notes"):
            aid = item['name'][:-5]
            if item['directory'] or not item['name'].endswith('.json') or aid not in articles:
                continue
            data = read_notes(aid)
            if notes_count(data):
                edited = datetime.fromtimestamp(item['mtime'], timezone.utc).isoformat(timespec='seconds')
                entries.append({"article": public(articles[aid]), "edited": edited, **data})
        entries.sort(key=lambda e: e['edited'], reverse=True)
        return {"entries": entries}
    if os.path.isdir(NOTES_DIR):
        for name in os.listdir(NOTES_DIR):
            aid = name[:-5]
            if not name.endswith(".json") or aid not in articles:
                continue
            data = read_notes(aid)
            if not notes_count(data):
                continue
            edited = datetime.fromtimestamp(os.path.getmtime(notes_path(aid)), timezone.utc).isoformat(timespec="seconds")
            entries.append({"article": public(articles[aid]), "edited": edited, **data})
    entries.sort(key=lambda e: e["edited"], reverse=True)
    return {"entries": entries}


@app.put("/api/articles/{aid}/notes")
def put_notes(aid: str, body: NotesIn):
    a = store.article(aid)
    if not a:
        raise HTTPException(404, "Article not found.")
    data = body.model_dump()
    raw = json.dumps(data, ensure_ascii=False, indent=1)
    if len(raw) > 2_000_000:
        raise HTTPException(413, "Notes are too large to save.")
    if REMOTE:
        REMOTE.write("notes/" + re.sub(r"[^0-9a-f]", "", aid) + ".json", raw.encode())
    else:
        os.makedirs(NOTES_DIR, exist_ok=True)
        tmp = notes_path(aid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(raw)
        os.replace(tmp, notes_path(aid))
    count = notes_count(data)
    with store.lock:
        if a.get("notes_count", 0) != count:
            a["notes_count"] = count
            store.save()
    return {"ok": True, "notes_count": count}


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "Job not found.")
    return job


curator = (remote_curator_type(Curator) if REMOTE else Curator)(medium_index, store, create_article, remove_article)
recommender = Recommender(store, curator.cache, notes_path, None if REMOTE else medium_index)

# Defense in depth. Article content is already sanitized (text escaped, tags allowlisted, links forced
# to http(s)), so this is a backstop: inline scripts/styles stay allowed because the app's own theme
# guard and lazy-CSS onload handlers need them, but connect-src/script-src('self')/object-src/base-uri/
# frame-ancestors keep any injected content from loading external code, exfiltrating the library, or
# being framed. img-src allows https so Medium's CDN thumbnails still load.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' https: data:; font-src 'self'; connect-src 'self'; object-src 'none'; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def _secure(response):
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


@app.middleware("http")
async def cache_headers(request, call_next):
    """Let the browser keep what rarely changes. Stamped /static URLs (?v=mtime) and vendor libraries are
    cached for a year; article images for a day. Everything else under /files and /static revalidates
    (cheap 304s via ETag/Last-Modified), so re-downloaded articles show up immediately."""
    response = _secure(await call_next(request))
    if REMOTE:
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            try:
                await asyncio.to_thread(store.flush)
            except Exception:
                from fastapi.responses import JSONResponse
                response = _secure(JSONResponse({"detail": "Telegram save failed. Keep the app open and retry; changes remain in memory."}, status_code=503))
        response.headers['Cache-Control'] = 'no-store'
        if request.url.path == '/':
            response.headers['Clear-Site-Data'] = '"cache"'
        return response
    path = request.url.path
    if response.status_code != 200 or "cache-control" in response.headers:
        return response
    if path.startswith("/static/") and ("v" in request.query_params or path.startswith("/static/vendor/")):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path.startswith("/files/") and "/images/" in path:
        response.headers["Cache-Control"] = "public, max-age=86400"
    elif path.startswith(("/files/", "/static/")):
        response.headers["Cache-Control"] = "no-cache"
    return response


class Compress:
    """gzip text responses (JS, CSS, JSON, article HTML) — the vendor libraries alone are ~1 MB raw
    over Wi-Fi to a phone. PDFs are already compressed, and a byte-range reply must not be re-encoded,
    so those pass straight through.

    Level 6 is the ratio/CPU sweet spot: over level 1 it drops KaTeX from 91 KB to 76 KB on the wire
    and app.js from 38 KB to 31 KB, while level 9 saves barely another 1%. This is a single-user app
    whose bottleneck is the phone's link, not the server CPU, and the big assets are immutable, so the
    browser compresses each only once per cache lifetime."""

    def __init__(self, app):
        self.app, self.gzip = app, GZipMiddleware(app, minimum_size=1024, compresslevel=6)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if b"range" not in headers and not scope["path"].endswith(".pdf"):
                return await self.gzip(scope, receive, send)
        return await self.app(scope, receive, send)


app.add_middleware(Compress)

if REMOTE:
    @app.get("/files/{path:path}")
    def remote_file(path: str):
        import mimetypes
        try:
            path = safe_relative(path)
            # Only article media, never library records or recovery snapshots.
            if path.rsplit('.', 1)[-1].lower() not in ('html', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'avif'):
                raise HTTPException(404)
            data = REMOTE.read('Medium-Library/' + path)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404)
        return Response(data, media_type=mimetypes.guess_type(path)[0] or 'application/octet-stream',
                        headers={'Cache-Control': 'no-store'})
else:
    app.mount("/files", StaticFiles(directory=LIBRARY_DIR), name="files")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/sw.js")
def service_worker():
    """Served from the root so the worker's scope covers the whole app, not just /static/."""
    if REMOTE:
        return Response("self.addEventListener('install', e => e.waitUntil(self.skipWaiting()));\n"
                        "self.addEventListener('activate', e => e.waitUntil(caches.keys().then(ns => Promise.all(ns.filter(n => /^(shell|files|data)-v/.test(n)).map(n => caches.delete(n)))).then(() => self.clients.claim())));\n"
                        "self.addEventListener('fetch', e => e.respondWith(fetch(e.request, {cache: 'no-store'})));",
                        media_type='application/javascript', headers={'Cache-Control': 'no-store', 'Service-Worker-Allowed': '/'})
    with open(os.path.join(STATIC_DIR, "sw.js"), encoding="utf-8") as f:
        return Response(f.read(), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/")
def index():
    """index.html with each local asset URL stamped by its modification time, so the browser never
    keeps running an old app.js/style.css after an update."""
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        page = f.read()

    def stamp(m):
        path = os.path.join(STATIC_DIR, *m.group(2).split("/"))
        version = int(os.path.getmtime(path)) if os.path.exists(path) else 0
        return f'{m.group(1)}/static/{m.group(2)}?v={version}"'

    page = re.sub(r'((?:src|href)=")/static/([^"?]+)"', stamp, page)
    return HTMLResponse(page, headers={"Cache-Control": "no-cache"})


def lan_address():
    """This machine's address on the local network, for opening the app on a phone."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packets are sent; this just picks the outbound interface
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8765"))
    # 0.0.0.0 so a phone on the same Wi-Fi can open the library. There is no login: anyone on that
    # network can read it. Set HOST=127.0.0.1 to keep it to this machine.
    host = os.environ.get("HOST", "0.0.0.0")
    lan = lan_address() if host == "0.0.0.0" else None
    print(f"\n  Library of Babel -> http://127.0.0.1:{port}")
    if lan:
        print(f"  On your phone    -> http://{lan}:{port}   (same Wi-Fi; anyone on it can read your library)")
        print("  In Safari, tap Share -> Add to Home Screen to install it as an app.")
    print()
    uvicorn.run(app, host=host, port=port, log_level="warning")
