"""Medium Library — local server.

Run:  .venv\\Scripts\\python app.py   then open http://127.0.0.1:8765
"""
import asyncio
import hashlib
import html
import json
import os
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
from fastapi.responses import FileResponse
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
    for name in ("Medium-Library", "search-index"):
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
FEED_TTL = 15 * 60


# ---------------------------------------------------------------- storage

class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.version = 0  # bumps on every save so the UI knows when to reload
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
            for a in self.data["articles"]:
                if a.get("claps") is not None and "source" not in a:
                    a["source"] = "auto"  # added by the curator before articles recorded their source
        else:
            self.data = {"topics": default_topics(), "articles": []}
            self.save()

    def save(self):
        with self.lock:
            self.version += 1
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)

    def topic(self, tid):
        return next((t for t in self.data["topics"] if t["id"] == tid), None)

    def subtopic(self, tid, sid):
        t = self.topic(tid)
        return t and next((s for s in t["subtopics"] if s["id"] == sid), None)

    def article(self, aid):
        return next((a for a in self.data["articles"] if a["id"] == aid), None)


store = Store(DB_PATH)
pipeline = PdfPipeline()
jobs: dict[str, dict] = {}
feed_cache: dict[str, tuple[float, list]] = {}


# ---------------------------------------------------------------- helpers

def slugify(s, limit=70):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:limit].rstrip("-") or "untitled"


def normalize_url(url):
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlsplit(url)
    if not p.netloc:
        raise HTTPException(400, "That doesn't look like a URL.")
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", ""))


def article_id(url):
    return hashlib.sha1(url.encode()).hexdigest()[:12]


def guess_title(url):
    last = urlsplit(url).path.rstrip("/").split("/")[-1]
    last = re.sub(r"-[0-9a-f]{8,14}$", "", last)  # Medium appends a hex post id
    return last.replace("-", " ").strip().capitalize() or url


def pdf_rel_path(a):
    return f"{a['topic']}/{a['subtopic']}/{slugify(a['title'])}-{a['id'][:8]}.pdf"


def abs_path(rel):
    return os.path.join(LIBRARY_DIR, *rel.split("/"))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def public(a):
    out = dict(a)
    out["pdf_url"] = f"/files/{a['pdf']}" if a.get("pdf") and os.path.exists(abs_path(a["pdf"])) else None
    return out


# ---------------------------------------------------------------- RSS discovery

NS = {"dc": "http://purl.org/dc/elements/1.1/", "content": "http://purl.org/rss/1.0/modules/content/"}


def fetch_tag_feed(tag):
    hit = feed_cache.get(tag)
    if hit and time.time() - hit[0] < FEED_TTL:
        return hit[1]
    root = ET.fromstring(polite.get(f"https://medium.com/feed/tag/{tag}", timeout=20))
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
        store.data["articles"] = [a for a in store.data["articles"] if not (a["topic"] == tid and a["subtopic"] == sid)]
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
    saved = {a["url"] for a in store.data["articles"]}
    for it in items:
        it["saved"] = it["url"] in saved
        it["id"] = article_id(it["url"])
    return {"tags": sub["tags"], "items": items, "errors": errors}


@app.get("/api/search")
async def search_medium(q: str = "", next: str | None = None):
    """Search all of Medium. Returns links only; nothing is downloaded until an article is opened."""
    q = q.strip()
    if not q:
        raise HTTPException(400, "Type something to search for.")
    try:
        offset = int(json.loads(next)["offset"]) if next else 0
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Bad paging token.")
    res = await asyncio.to_thread(web_search, q, offset)
    saved = {a["url"] for a in store.data["articles"]}
    items = [dict(it, id=article_id(it["url"]), saved=it["url"] in saved) for it in res["items"]]
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
        existing = next((a for a in store.data["articles"] if a["url"] == url), None)
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
        store.data["articles"].insert(0, a)
        store.save()
    return a


def remove_article(a):
    with store.lock:
        if a in store.data["articles"]:
            store.data["articles"].remove(a)
            store.save()
    if a.get("pdf") and os.path.exists(abs_path(a["pdf"])):
        os.remove(abs_path(a["pdf"]))


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
        old = a.get("pdf")
        a["topic"], a["subtopic"] = body.topic, body.subtopic
        if old and os.path.exists(abs_path(old)):
            new = pdf_rel_path(a)
            os.makedirs(os.path.dirname(abs_path(new)), exist_ok=True)
            shutil.move(abs_path(old), abs_path(new))
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


async def run_job(job, a):
    def stage(s):
        job["stage"] = s

    try:
        rel = pdf_rel_path(a)
        meta = await pipeline.render(a["url"], abs_path(rel), stage)
        with store.lock:
            if meta.get("title") and a["title"] != meta["title"]:
                # rename file to match the real title now that we know it
                a["title"] = meta["title"]
                better = pdf_rel_path(a)
                if better != rel:
                    shutil.move(abs_path(rel), abs_path(better))
                    rel = better
            if a.get("pdf") and a["pdf"] != rel and os.path.exists(abs_path(a["pdf"])):
                os.remove(abs_path(a["pdf"]))
            a["author"] = meta.get("author") or a["author"]
            a["snippet"] = a["snippet"] or meta.get("subtitle", "")
            a["pdf"], a["fetched"] = rel, now_iso()
            store.save()
        job.update(status="done", stage="done", article=public(a))
    except PipelineError as e:
        job.update(status="error", error=str(e))
    except Exception as e:
        job.update(status="error", error=f"{type(e).__name__}: {e}")


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
    job = {"id": uuid.uuid4().hex[:10], "article_id": aid, "status": "running", "stage": "queued",
           "freedium_url": pipeline.freedium_url(a["url"])}
    jobs[job["id"]] = job
    asyncio.create_task(run_job(job, a))
    return job


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "Job not found.")
    return job


curator = Curator(medium_index, store, create_article, remove_article)

app.mount("/files", StaticFiles(directory=LIBRARY_DIR), name="files")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8765"))
    print(f"\n  Medium Library -> http://127.0.0.1:{port}\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
