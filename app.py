"""Medium Library — local server.

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
from pydantic import BaseModel

import polite
from curator import Curator
from medium_index import MediumIndex
from pipeline import PdfPipeline, PipelineError
from topics import default_topics

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
    for name in ("Medium-Library", "search-index", "notes"):
        src, dst = os.path.join(ROOT, name), os.path.join(target, name)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.move(src, dst)
    with open(marker, "w", encoding="utf-8") as f:
        f.write(target)
    print(f"\n  Low disk space next to the app: moved library data to {target}")
    return target


DATA_DIR = choose_data_dir()
LIBRARY_DIR = os.path.join(DATA_DIR, "Medium-Library")
DB_PATH = os.path.join(LIBRARY_DIR, "library.json")
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
                blob = json.dumps(self.data, indent=2, ensure_ascii=False)
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


store = Store(DB_PATH)
pipeline = PdfPipeline(STATIC_DIR)
jobs: dict[str, dict] = {}
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
    """os.path.exists(abs_path(rel)), cached briefly. Only downloads change these, and a download
    rewrites the article's row anyway, so a stale 'yes' can't outlive the file by more than EXISTS_TTL."""
    if not rel:
        return False
    hit = _exists_cache.get(rel)
    now = time.time()
    if hit and now - hit[0] < EXISTS_TTL:
        return hit[1]
    ok = os.path.exists(abs_path(rel))
    if len(_exists_cache) > 100_000:  # a whole library's worth of stale paths, after moves and removals
        _exists_cache.clear()
    _exists_cache[rel] = (now, ok)
    return ok


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
        items.append({
            "url": normalize_url(link),
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
medium_index = MediumIndex(os.path.join(DATA_DIR, "search-index", "medium.db"), default_days=90)  # ~4 MB per day


def search_feeds(q):
    words = [w for w in re.findall(r"[a-z0-9]+", q.lower()) if w not in STOPWORDS]
    if not words:
        return []
    # candidate tags: the whole phrase, adjacent pairs, then single words
    tags = list(dict.fromkeys(["-".join(words)] + ["-".join(words[i:i + 2]) for i in range(len(words) - 1)] + words))[:6]
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


def web_search(q, offset=0):
    """Returns {items, next, provider, notice, index}: one page from the local index,
    topped up with live tag-feed matches on the first page while the index is thin."""
    found, mode = medium_index.search(q, PAGE_SIZE, offset)
    items, provider = list(found), "index"
    if offset == 0 and len(found) < PAGE_SIZE:
        seen = {i["url"] for i in found}
        extra = [it for it in search_feeds(q) if it["url"] not in seen][:PAGE_SIZE]
        items += extra
        if extra and not found:
            provider = "feeds"
    st = medium_index.status()
    notice = None
    if st["days_indexed"] < min(30, st["days_setting"]):
        notice = (f"Your Medium search index is still being built: {st['posts']:,} articles from "
                  f"{st['days_indexed']} days so far. Results improve as it keeps crawling in the background.")
    elif mode == "any" and found:
        notice = "No indexed title contains all of your words, so these match some of them."
    nxt = {"offset": offset + PAGE_SIZE} if len(found) == PAGE_SIZE else None
    return {"items": items, "next": nxt, "provider": provider, "notice": notice, "index": st}


# ---------------------------------------------------------------- app

@asynccontextmanager
async def lifespan(_app):
    medium_index.start()
    curator.start()
    yield
    curator.stop()
    medium_index.stop()
    pipeline.close()
    store.stop()


app = FastAPI(title="Medium Library", lifespan=lifespan)


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


class MoveIn(BaseModel):
    topic: str
    subtopic: str


@app.get("/api/library")
def get_library():
    with store.lock:  # the curator adds and removes articles from its own thread
        return {"topics": store.data["topics"], "articles": [public(a) for a in store.data["articles"]]}


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
        # articles are unfiled rather than lost; their PDFs stay on disk
        for a in [a for a in store.data["articles"] if a["topic"] == tid and a["subtopic"] == sid]:
            store.discard(a)
        store.save()
    folder = os.path.join(LIBRARY_DIR, tid, sid)
    if os.path.isdir(folder) and not os.listdir(folder):
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
            "provider": res["provider"], "notice": res["notice"], "index": res["index"]}


class IndexSettings(BaseModel):
    days: int | None = None
    paused: bool | None = None
    curator_enabled: bool | None = None


def full_status():
    return {**medium_index.status(), "curator": curator.status(), "network": polite.status(),
            "library_version": store.version, "articles": len(store.data["articles"])}


@app.get("/api/index")
def index_status():
    return full_status()


@app.post("/api/index")
def index_settings(body: IndexSettings):
    if body.days is not None:
        medium_index.set_days(body.days)
    if body.paused is not None:
        medium_index.set_paused(body.paused)
    if body.curator_enabled is not None:
        curator.set_enabled(body.curator_enabled)
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


def remove_article(a):
    if not store.discard(a):
        return
    if a.get("doc"):
        shutil.rmtree(abs_path(posixpath.dirname(a["doc"])), ignore_errors=True)
    elif a.get("pdf") and os.path.exists(abs_path(a["pdf"])):
        os.remove(abs_path(a["pdf"]))
    if os.path.exists(notes_path(a["id"])):
        os.remove(notes_path(a["id"]))


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
    with store.lock:
        old_pdf, old_doc = a.get("pdf"), a.get("doc")
        a["topic"], a["subtopic"] = body.topic, body.subtopic
        if old_doc:
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
    remove_article(a)
    return {"ok": True}


JOB_TTL = 3600  # finished jobs the UI may still poll for


def prune_jobs():
    """Drop finished jobs the UI has had its chance to read, so a long session doesn't grow forever."""
    cutoff = time.time() - JOB_TTL
    for jid in [j["id"] for j in jobs.values() if j["status"] != "running" and j["started"] < cutoff]:
        jobs.pop(jid, None)


async def run_job(job, a):
    def stage(s, **info):
        job["stage"] = s
        job.update(info)  # route ("medium" | "freedium"), reason, freedium_url
        job.setdefault("timings", {})[s] = round(time.time() - job["started"], 1)  # seconds since start

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
    if not force and a.get("pdf") and os.path.exists(abs_path(a["pdf"])):
        return {"id": None, "status": "done", "stage": "done", "article": public(a)}
    running = next((j for j in jobs.values() if j["article_id"] == aid and j["status"] == "running"), None)
    if running:
        return running
    job = {"id": uuid.uuid4().hex[:10], "article_id": aid, "status": "running", "stage": "queued", "started": time.time()}
    prune_jobs()
    jobs[job["id"]] = job
    with store.lock:
        a["fetching"] = True  # the curator leaves this one alone until the download settles
    asyncio.create_task(run_job(job, a))
    return job


class NotesIn(BaseModel):
    notes: str = ""
    highlights: list[dict] = []


@app.get("/api/articles/{aid}/notes")
def get_notes(aid: str):
    if not store.article(aid):
        raise HTTPException(404, "Article not found.")
    path = notes_path(aid)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"notes": "", "highlights": []}


@app.put("/api/articles/{aid}/notes")
def put_notes(aid: str, body: NotesIn):
    a = store.article(aid)
    if not a:
        raise HTTPException(404, "Article not found.")
    data = body.model_dump()
    raw = json.dumps(data, ensure_ascii=False, indent=1)
    if len(raw) > 2_000_000:
        raise HTTPException(413, "Notes are too large to save.")
    os.makedirs(NOTES_DIR, exist_ok=True)
    tmp = notes_path(aid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(raw)
    os.replace(tmp, notes_path(aid))
    count = len(data["highlights"]) + (1 if data["notes"].strip() else 0)
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


curator = Curator(medium_index, store, create_article, remove_article)

app.mount("/files", StaticFiles(directory=LIBRARY_DIR), name="files")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/sw.js")
def service_worker():
    """Served from the root so the worker's scope covers the whole app, not just /static/."""
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
    print(f"\n  Medium Library -> http://127.0.0.1:{port}")
    if lan:
        print(f"  On your phone    -> http://{lan}:{port}   (same Wi-Fi; anyone on it can read your library)")
        print("  In Safari, tap Share -> Add to Home Screen to install it as an app.")
    print()
    uvicorn.run(app, host=host, port=port, log_level="warning")
