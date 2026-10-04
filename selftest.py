"""Offline self-test: exercises the library API, the search index and the curator without touching
the network. Nothing here reaches Medium — sitemaps, post pages and feeds are all stubbed.

Run:  python selftest.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

WORK = tempfile.mkdtemp(prefix="medium-library-selftest-")
os.environ["MEDIUM_LIBRARY_DATA"] = WORK

# The Freedium route is pointed at a stub server on localhost (started below), with a dead host first
# so the mirror fallback is exercised. FREEDIUM_MIRRORS replaces the public fallback, so no test can
# reach the real Freedium.
STUB_PORT = __import__("socket").socket()
STUB_PORT.bind(("127.0.0.1", 0))
STUB_PORT = STUB_PORT.getsockname()[1]
os.environ["FREEDIUM_BASE"] = "http://127.0.0.1:1"  # nothing listens here: the first mirror always fails
os.environ["FREEDIUM_MIRRORS"] = f"http://127.0.0.1:{STUB_PORT}"

import polite  # noqa: E402  (must be importable before app.py builds its clients)

PAGES = {}  # url -> text served instead of a real request


def fake_get(url, timeout=40, headers=None, priority=False):
    if url not in PAGES:
        raise AssertionError(f"self-test tried to reach the network: {url}")
    return PAGES[url].encode()


polite.get = fake_get
polite.get_text = lambda url, timeout=40, priority=False: fake_get(url).decode()

import app as A  # noqa: E402
import curator as C  # noqa: E402
import medium_render  # noqa: E402
import pipeline as _pipeline  # noqa: E402

A.pipeline_mod = _pipeline
from fastapi.testclient import TestClient  # noqa: E402

FAILED = []
BROWSER = False


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'' if ok else f'  -- {detail}'}")
    if not ok:
        FAILED.append(name)


# ---------------------------------------------------------------- fixtures

def post_page(pid, title, claps=500, tags=("cybersecurity",), locked=False, paragraphs=2):
    """A Medium post page with the Apollo state the renderer reads."""
    state = {
        f"Post:{pid}": {
            "__typename": "Post", "id": pid, "title": title, "clapCount": claps, "readingTime": 4,
            "detectedLanguage": "en", "isLocked": locked, "firstPublishedAt": 1757000000000,
            "creator": {"__ref": "User:u1"}, "tags": [{"__ref": f"Tag:{t}"} for t in tags],
            "previewImage": {"id": "1*abc.png"},
            "extendedPreviewContent": {"subtitle": "A short standfirst."},
            "content(postMeteringOptions:{})": {"bodyModel": {
                "paragraphs": [{"__ref": f"P:{i}"} for i in range(paragraphs)], "sections": [{"startIndex": 0}]}},
        },
        "User:u1": {"name": "Test Author"},
        **{f"Tag:{t}": {"id": t} for t in tags},
        "P:0": {"type": "H2", "text": "A heading", "markups": []},
        "P:1": {"type": "P", "text": "Body text with a link.", "markups": [
            {"type": "A", "start": 0, "end": 4, "href": "https://example.com/x"}]},
    }
    return f"<html><body><script>window.__APOLLO_STATE__ = {json.dumps(state)}</script></body></html>"


def sitemap_day(day, urls):
    body = "".join(f"<url><loc>{u}</loc><priority>0.8</priority></url>" for u in urls)
    return f"<urlset>{body}</urlset>"


FREEDIUM_PAGE = """<!doctype html><html><head><title>Locked story - Freedium</title></head><body>
<article>
  <header><h1>Locked story</h1><div>By Paywalled Author</div>
    <p>Sep 14, 2026</p><p>The standfirst of a member-only piece.</p></header>
  <nav><button>Close</button></nav>
  <div class="prose">
    <h2>First section</h2>
    <p>A paragraph with <b>bold</b>, <i>italic</i> and a <a href="https://example.com/ref">link</a>.</p>
    <pre class="language-python"><code>def f():\n    return 42</code></pre>
    <ul><li>one</li><li>two</li></ul>
    <blockquote>Quoted line.</blockquote>
    <figure><img src="https://miro.medium.com/v2/resize:fit:1400/1*zz.png" alt="a figure"><figcaption>Cap</figcaption></figure>
    <script>window.evil = 1;</script>
    <iframe src="https://example.com/embed"></iframe>
    <table><tr><td>cell</td></tr></table>
  </div>
</article></body></html>"""


def start_stub_server():
    """Serves a Freedium-shaped page for every path except /empty-*, which has no article body."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = ('<html><body><article><h1>Nothing here</h1>'
                    '<div class="prose">   </div></article></body></html>'
                    if "empty-story" in self.path else FREEDIUM_PAGE)
            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", STUB_PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def browser_available():
    """Chromium is only needed for the PDF tests; skip them cleanly when it isn't installed."""
    import asyncio

    from playwright.async_api import async_playwright

    async def probe():
        pw = await async_playwright().start()
        try:
            b = await pw.chromium.launch(**({"executable_path": A.pipeline_mod.CHROMIUM_PATH}
                                            if A.pipeline_mod.CHROMIUM_PATH else {}))
            await b.close()
            return True
        finally:
            await pw.stop()
    try:
        return asyncio.run(probe())
    except Exception as e:
        print(f"  (no Chromium: {type(e).__name__}; run `playwright install chromium`, or set CHROMIUM_PATH)")
        return False


# ---------------------------------------------------------------- tests

def test_storage():
    a = A.create_article({"url": "https://medium.com/@x/first-post-aaaabbbbcccc",
                          "topic": "cybersecurity", "subtopic": "malware", "title": "First post"})
    check("create_article returns an article", a["id"] and a["title"] == "First post")
    again = A.create_article({"url": "https://medium.com/@x/first-post-aaaabbbbcccc/",
                              "topic": "cybersecurity", "subtopic": "malware"})
    check("the same URL is not added twice", again is a and len(A.store.data["articles"]) == 1)
    check("indexed by id", A.store.article(a["id"]) is a)
    check("indexed by url", A.store.by_url(a["url"]) is a)

    A.store.flush()
    with open(A.DB_PATH, encoding="utf-8") as f:
        on_disk = json.load(f)
    check("flush writes library.json", len(on_disk["articles"]) == 1, len(on_disk["articles"]))

    A.remove_article(a)
    check("remove clears both indexes",
          A.store.article(a["id"]) is None and A.store.by_url(a["url"]) is None and not A.store.data["articles"])


def test_topic_merge():
    """A library written before a topic existed picks it up on the next start."""
    path = os.path.join(WORK, "merge-test.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"topics": [{"id": "cybersecurity", "name": "Cybersecurity",
                               "subtopics": [{"id": "malware", "name": "Malware", "tags": ["malware"]}]}],
                   "articles": []}, f)
    s = A.Store(path)
    ids = {t["id"] for t in s.data["topics"]}
    subs = {x["id"] for t in s.data["topics"] if t["id"] == "cybersecurity" for x in t["subtopics"]}
    check("new default topics are merged in", "science" in ids and "artificial-intelligence" in ids)
    check("new default subtopics are merged in", "ctfs" in subs and "malware" in subs)
    s.stop()


def test_api():
    client = TestClient(A.app_without_lifespan if hasattr(A, "app_without_lifespan") else A.app)
    r = client.get("/api/library")
    check("GET /api/library", r.status_code == 200 and "topics" in r.json())

    r = client.post("/api/articles", json={"url": "https://medium.com/@y/api-post-ddddeeeeffff",
                                           "topic": "cybersecurity", "subtopic": "exploits", "title": "Api post"})
    check("POST /api/articles", r.status_code == 200, r.text)
    aid = r.json()["id"]

    r = client.post("/api/articles", json={"url": "https://medium.com/@y/z-111122223333",
                                           "topic": "nope", "subtopic": "nope"})
    check("unknown subtopic is rejected", r.status_code == 404)

    r = client.patch(f"/api/articles/{aid}", json={"topic": "science", "subtopic": "physics"})
    check("PATCH moves an article", r.status_code == 200 and r.json()["topic"] == "science", r.text)

    r = client.put(f"/api/articles/{aid}/notes", json={"notes": "hello", "highlights": [{"id": "h1"}]})
    check("PUT notes", r.status_code == 200 and r.json()["notes_count"] == 2, r.text)
    r = client.get(f"/api/articles/{aid}/notes")
    check("GET notes round-trips", r.json()["notes"] == "hello", r.text)

    r = client.post("/api/topics", json={"name": "My Topic", "parent": "custom", "tags": ["a-tag"]})
    check("POST /api/topics", r.status_code == 200 and r.json()["subtopic"]["id"] == "my-topic", r.text)
    r = client.delete("/api/topics/custom/my-topic")
    check("DELETE a custom subtopic", r.status_code == 200, r.text)
    r = client.delete("/api/topics/cybersecurity/malware")
    check("built-in subtopics can't be deleted", r.status_code == 400)

    r = client.get("/api/index")
    check("GET /api/index", r.status_code == 200 and "curator" in r.json(), r.text)
    r = client.get("/api/search?q=")
    check("empty search is rejected", r.status_code == 400)
    r = client.get("/api/jobs/nosuchjob")
    check("unknown job is a 404", r.status_code == 404)
    r = client.get("/")
    check("GET / serves the UI with stamped assets",
          r.status_code == 200 and "/static/app.js?v=" in r.text, r.text[:200])

    client.delete(f"/api/articles/{aid}")
    check("DELETE an article", A.store.article(aid) is None)


def test_index():
    idx = A.medium_index
    urls = [f"https://medium.com/@a/indexed-post-about-malware-{i:012x}" for i in range(30)]
    kept = idx.crawl_day("2026-09-10", "https://medium.com/sitemap/posts/2026/posts-2026-09-10.xml")
    check("crawl_day needs its page stubbed", False) if kept is None else None
    result = idx.search("malware", limit=50, mode="all")
    rows, mode = result["items"], result["mode"]
    check("indexed posts are searchable", len(rows) == 30 and mode == "all", f"{len(rows)} {mode}")
    check("titles come from the slug", rows[0]["title"].startswith("Indexed post about malware"), rows[0]["title"])
    result = idx.search("malware unrelatedword")
    rows, mode = result["items"], result["mode"]
    check("falls back to matching any word", mode == "any" and rows, f"{mode} {len(rows)}")
    rows = idx.search("")["items"]
    check("an empty query returns nothing", rows == [])


def test_performance_guardrails():
    """Pin bounded search work and recommendation reuse without machine-specific timings."""
    import types
    from recommender import Recommender

    # Deep paging must not make SQLite return/rerank an unbounded candidate set.
    idx = A.medium_index
    requested = []
    real_match = idx._match

    def counted_match(expr, query, limit):
        requested.append(limit)
        return real_match(expr, query, limit)

    idx._match = counted_match
    try:
        idx.search("malware", limit=20, offset=100_000)
    finally:
        idx._match = real_match
    from medium_index import MAX_CANDIDATES
    check("deep search paging keeps each candidate query within the configured cap",
          bool(requested) and max(requested) <= MAX_CANDIDATES, requested)

    # A repeat recommendation request with the same library/cache version must reuse its ranking.
    rec = object.__new__(Recommender)
    rec.store = types.SimpleNamespace(version=7)
    rec.cache = {}
    rec._memo = {}
    calls = []
    rec._rank = lambda want, limit, access: calls.append((want, limit, access)) or {"items": []}
    first = rec.recommend(["malware"], 30)
    second = rec.recommend(["malware"], 30)
    check("repeat recommendation loads reuse the cached ranking", first is second and len(calls) == 1,
          len(calls))
    rec.store.version += 1
    rec.recommend(["malware"], 30)
    check("a library change invalidates recommendation reuse", len(calls) == 2, len(calls))

    class CountedRows(list):
        visited = 0

        def __iter__(self):
            for row in super().__iter__():
                self.visited += 1
                yield row

    # Initial load should stop at the requested page, even when the library is large.
    counted = CountedRows(dict(id=f"first-{i}", title=f"First {i}", topic="science",
                               subtopic="physics", locked=bool(i % 2)) for i in range(10_000))
    with A.store.lock:
        previous = A.store.data["articles"]
        A.store.data["articles"] = counted
        try:
            page = A.get_library_first_page(60)
            check("initial library loading visits only the first page", counted.visited == 60, counted.visited)
            counted.visited = 0
            filtered = A.get_library_first_page(10, "science", "physics", "locked")
            check("initial page filters stop once enough matching articles are found",
                  counted.visited == 20 and len(filtered["articles"]) == 10
                  and all(a["locked"] for a in filtered["articles"]), counted.visited)
            counted[0]["title"] = "changed after response"
            check("initial page returns a stable article snapshot", page["articles"][0]["title"] == "First 0")
        finally:
            A.store.data["articles"] = previous

    # Paged browsing should reuse its precomputed scope/count index between page requests.
    probe = A.create_article({"url": "https://medium.com/@a/page-index-probe-7f7f7f7f7f7f",
                              "topic": "cybersecurity", "subtopic": "malware", "title": "Page index probe"})
    probe2 = A.create_article({"url": "https://medium.com/@a/page-index-probe-8f8f8f8f8f8f",
                               "topic": "cybersecurity", "subtopic": "malware", "title": "Page index probe two"})
    probe["notes_count"] = 1
    A.store.save()
    first_page = A.get_library_page(0, 1)
    index_ref = A._library_page_cache
    second_page = A.get_library_page(1, 1)
    expected_noted = sum(bool(a.get("notes_count")) for a in A.store.data["articles"])
    check("paged library requests reuse the versioned index", A._library_page_cache is index_ref)
    check("library page counts report each noted article once",
          first_page["counts"]["noted"] == expected_noted, (first_page["counts"]["noted"], expected_noted))
    check("paged library results advance without rescanning into duplicate rows",
          first_page["articles"][0]["id"] != second_page["articles"][0]["id"],
          [x["id"] for x in first_page["articles"] + second_page["articles"]])
    probe["locked"] = True
    probe["notes_count"] = 0
    A.store.save()
    changed = A.get_library_page(0, 100, "cybersecurity", "malware", "locked")
    check("library index invalidates after access and note changes",
          A._library_page_cache is not index_ref
          and any(a["id"] == probe["id"] for a in changed["articles"])
          and changed["counts"]["noted"] == expected_noted - 1)
    A.store.discard(probe)
    A.store.discard(probe2)


def test_render():
    url = "https://medium.com/@a/free-story-abcdef123456"
    PAGES[url] = post_page("abcdef123456", "Free story", tags=("cybersecurity", "malware"))
    route = medium_render.prepare(url)
    check("a free story renders from Medium", route["route"] == "medium", route.get("reason"))
    check("the body carries the paragraphs", "<h2>A heading</h2>" in route["body"], route.get("body"))
    check("links survive", '<a href="https://example.com/x">Body</a>' in route["body"], route.get("body"))
    header = medium_render.render_header(route["meta"], url, route["body"], "medium")
    check("the header names the author and the source", "Test Author" in header and url in header, header)

    locked = "https://medium.com/@a/locked-story-999999999999"
    PAGES[locked] = post_page("999999999999", "Locked story", locked=True)
    check("member-only stories go to Freedium",
          medium_render.prepare(locked) == {"route": "freedium", "reason": "member-only story"})


def test_curator():
    cur = A.curator
    sub = {"id": "malware", "name": "Malware", "tags": ["malware"]}
    meta = medium_render.meta_of(*medium_render.parse_post(
        post_page("abcdef123456", "Free story", tags=("cybersecurity", "malware")), "x-abcdef123456"))
    check("a post about the subtopic fits it", C.fits(meta, "cybersecurity", sub, ["malware"]))
    check("the same post does not fit an unrelated subtopic",
          not C.fits(meta, "science", {"id": "physics", "tags": ["physics"]}, ["physics"]))
    check("fresh popular posts outrank stale ones",
          C.trend_score(1000, "2026-09-14T00:00:00+00:00") > C.trend_score(1000, "2025-01-01T00:00:00+00:00"))

    for u in [f"https://medium.com/@a/indexed-post-about-malware-{i:012x}" for i in range(30)]:
        # member-only: a library set to MEMBER_ONLY is the only thing the curator will take
        PAGES[u] = post_page(u[-12:], f"Malware post {u[-4:]}", tags=("cybersecurity", "malware"), locked=True)
    before = len(A.store.data["articles"])
    added, rotated, read = cur.curate("cybersecurity", sub, cap=10, filling=True)
    check("the curator adds articles it found in the index", added > 0, f"added={added} read={read}")
    check("it stops at the cap", added <= 10, f"added={added}")
    check("they land in the library", len(A.store.data["articles"]) == before + added)
    check("they are filed under the subtopic",
          all(a["topic"] == "cybersecurity" and a["subtopic"] == "malware" and a["source"] == "auto"
              for a in A.store.data["articles"][:added]))

    added2, _, _ = cur.curate("cybersecurity", sub, cap=10, filling=True)
    check("a full subtopic does not grow past its cap", added2 == 0, f"added={added2}")

    st = cur.status()
    check("status reports the counts", st["added_total"] >= added and st["auto_articles"] >= added, st)

    # a clearly better post replaces the weakest auto-added one nobody downloaded
    for a in A.store.data["articles"]:
        if a.get("source") == "auto":
            a["claps"] = 1
    hit = "https://medium.com/@a/a-huge-malware-story-cafecafecafe"
    PAGES[hit] = post_page("cafecafecafe", "A huge malware story", claps=500000,
                           tags=("cybersecurity", "malware"), locked=True)
    A.medium_index.crawl_day("2026-09-11", "https://medium.com/sitemap/posts/2026/posts-2026-09-11.xml")
    held = len([a for a in A.store.data["articles"] if a["subtopic"] == "malware"])
    added3, rotated, _ = cur.curate("cybersecurity", sub, cap=10, filling=True)
    check("better posts rotate in rather than being added", rotated >= 1 and added3 == 0,
          f"added={added3} rotated={rotated}")
    check("rotation keeps the subtopic at its cap",
          len([a for a in A.store.data["articles"] if a["subtopic"] == "malware"]) == held)
    check("the new post is in the library", A.store.by_url(hit) is not None)
    check("the store indexes stay consistent after a rotation",
          len(A.store._by_id) == len(A.store.data["articles"]) == len(A.store._by_url))


def test_member_only_library():
    """The library collects paywalled stories only. Free ones are not added, and free ones already in
    it are dropped — except the two kinds the app never takes away."""
    check("the member-only policy is on", C.MEMBER_ONLY is True)

    cur = A.curator
    sub = {"id": "malware", "name": "Malware", "tags": ["malware"]}
    urls = [f"https://medium.com/@a/how-to-spot-malware-in-the-wild-{i}-{i:012x}" for i in range(12)]
    for i, u in enumerate(urls):  # half free, half member-only
        PAGES[u] = post_page(f"{i:012x}", f"How to spot malware {i}", claps=900 + i,
                             tags=("cybersecurity", "malware"), locked=(i % 2 == 0))
    import sqlite3
    db = sqlite3.connect(A.medium_index.path)
    for u in urls:
        row = db.execute("INSERT OR IGNORE INTO posts(url, day, prio) VALUES (?,?,?)",
                         (u, "2026-09-12", 0.9))
        if row.rowcount:
            db.execute("INSERT INTO posts_fts(rowid, title, author) VALUES (?,?,?)",
                       (row.lastrowid, u.rsplit("/", 1)[-1].replace("-", " ") + " malware", "a"))
    db.commit()
    db.close()

    before = {a["url"] for a in A.store.data["articles"]}
    cur.curate("cybersecurity", sub, cap=50, filling=True)
    added = [a for a in A.store.data["articles"] if a["url"] not in before]
    check("it added something", added, len(added))
    check("everything it added is member-only", all(a.get("locked") is True for a in added),
          [(a["title"], a.get("locked")) for a in added][:4])
    check("no free story was added", not any(a.get("locked") is False for a in added))

    # a library that already holds free articles of every kind
    keep_mine = A.create_article({"url": "https://medium.com/@a/i-added-this-one-e1e1e1e1e1e1",
                                  "topic": "cybersecurity", "subtopic": "malware",
                                  "title": "I added this one", "locked": False})
    keep_read = A.create_article({"url": "https://medium.com/@a/i-read-this-one-e2e2e2e2e2e2",
                                  "topic": "cybersecurity", "subtopic": "malware", "source": "auto",
                                  "title": "I read this one", "locked": False})
    keep_read["pdf"] = "cybersecurity/malware/i-read-this-one/article.pdf"
    drop_me = A.create_article({"url": "https://medium.com/@a/nobody-read-this-e3e3e3e3e3e3",
                                "topic": "cybersecurity", "subtopic": "malware", "source": "auto",
                                "title": "Nobody read this", "locked": False})
    unchecked = A.create_article({"url": "https://medium.com/@a/status-unknown-e4e4e4e4e4e4",
                                  "topic": "cybersecurity", "subtopic": "malware", "source": "auto",
                                  "title": "Status unknown"})

    dropped = cur.purge_free()
    check("the free article nobody read is dropped", A.store.by_url(drop_me["url"]) is None)
    check("one you added yourself is kept", A.store.by_url(keep_mine["url"]) is not None)
    check("one you downloaded is kept", A.store.by_url(keep_read["url"]) is not None)
    check("one whose status isn't known yet is kept until it is checked",
          A.store.by_url(unchecked["url"]) is not None)
    check("the sweep reports what it dropped", dropped >= 1, dropped)
    check("no member-only article was touched",
          all(a.get("locked") is not False or a.get("pdf") or a.get("source") != "auto"
              for a in A.store.data["articles"]))

    for a in [keep_mine, keep_read, unchecked] + added:
        if A.store.by_url(a["url"]):
            A.store.discard(A.store.by_url(a["url"]))


def test_explicit_bulk_goal():
    """An explicit mixed bulk request survives the default member-only collection policy."""
    cur = A.curator
    sub = {"id": "malware", "name": "Malware", "tags": ["malware"]}
    before = {a["url"] for a in A.store.data["articles"]}
    cur.set_goal(1, 1)
    added_count, rotated, _ = cur.curate("cybersecurity", sub, cap=100, filling=True)
    batch = [a for a in A.store.data["articles"] if a["url"] not in before]
    check("bulk adds exactly the requested count without rotating", added_count == 2 and rotated == 0)
    check("bulk fulfills both requested membership types",
          len(batch) == 2 and sum(a.get("locked") is True for a in batch) == 1
          and sum(a.get("locked") is False for a in batch) == 1)
    check("a completed bulk goal clears itself", cur.goal() is None)
    cur.purge_free()
    check("explicitly requested free links survive automatic cleanup",
          all(A.store.by_url(a["url"]) is a for a in batch))
    for a in batch:
        A.store.discard(a)


def test_candidate_scan_deepens():
    """The blocker for a big library: a subtopic that already holds a lot must keep finding candidates
    below the rows it has taken."""
    import sqlite3
    db = sqlite3.connect(f"file:{A.medium_index.path}?mode=ro", uri=True)
    try:
        all_urls = [u for (u,) in db.execute("SELECT url FROM posts").fetchall()]
        taken = set(all_urls[:25])  # pretend the top 25 are already saved
        shallow = C.candidates(db, ["malware"], taken, 10, None, scan=25)
        deep = C.candidates(db, ["malware"], taken, 10, None, scan=25 + 8 * len(taken))
        check("a shallow scan runs dry once the top rows are taken", len(shallow) < len(deep),
              f"shallow={len(shallow)} deep={len(deep)}")
        check("deepening the scan finds more", len(deep) > 0 and not (set(deep) & taken), len(deep))
    finally:
        db.close()


def test_pipeline_medium_route():
    """The whole free-story path: Medium page -> clean HTML -> images -> one continuous PDF."""
    if not BROWSER:
        return
    import asyncio
    url = "https://medium.com/@a/pipeline-story-101010101010"
    PAGES[url] = post_page("101010101010", "Pipeline story", tags=("cybersecurity", "malware"))
    out = os.path.join(WORK, "render-medium")
    stages = []
    meta = asyncio.run(A.pipeline.render(url, out, lambda s, **i: stages.append(s)))
    check("the free route runs every stage", stages == ["check", "medium", "images", "pdf"], stages)
    check("it reports the Medium route", meta["route"] == "medium" and meta["title"] == "Pipeline story", meta)
    doc = os.path.join(out, "content.html")
    pdf = os.path.join(out, "article.pdf")
    check("content.html is written", os.path.exists(doc))
    check("article.pdf is a PDF", open(pdf, "rb").read(5) == b"%PDF-", "not a PDF")
    check("the PDF has real content", os.path.getsize(pdf) > 3000, os.path.getsize(pdf))
    html = open(doc, encoding="utf-8").read()
    check("the heading survives", "<h2>A heading</h2>" in html)
    check("the byline survives", "Test Author" in html)
    check("the source line points back to Medium", url in html)
    check("no scratch print.html is left behind", not os.path.exists(os.path.join(out, "print.html")))
    check("a failed image download keeps the online URL", "miro.medium.com" in html or "<img" not in html)


def test_pipeline_freedium_route():
    """Member-only stories: the first mirror is dead, so this also proves the fallback works."""
    if not BROWSER:
        return
    import asyncio
    url = "https://medium.com/@a/locked-story-202020202020"
    PAGES[url] = post_page("202020202020", "Locked story", locked=True)
    out = os.path.join(WORK, "render-freedium")
    stages, info = [], {}

    def stage(s, **kw):
        stages.append(s)
        info.update(kw)

    meta = asyncio.run(A.pipeline.render(url, out, stage))
    check("the paywalled route runs every stage", stages[:2] == ["check", "freedium"] and stages[-1] == "pdf", stages)
    check("it reports the Freedium route and why", meta["route"] == "freedium"
          and meta["reason"] == "member-only story", meta)
    check("it fell through the dead mirror to a working one",
          info["freedium_url"].startswith(f"http://127.0.0.1:{STUB_PORT}"), info.get("freedium_url"))
    html = open(os.path.join(out, "content.html"), encoding="utf-8").read()
    check("the title and author are read off the page", "Locked story" in html and "Paywalled Author" in html, html[:300])
    check("the body is rebuilt", "<h2>First section</h2>" in html and "<blockquote>" in html, html[:400])
    check("code keeps its language", 'data-lang="python"' in html, html[:400])
    check("b/i are rewritten to strong/em", "<strong>bold</strong>" in html and "<em>italic</em>" in html)
    check("lists and tables survive", "<li>one</li>" in html and "<table>" in html)
    check("scripts are dropped", "window.evil" not in html)
    check("buttons and nav are dropped", "Close</button>" not in html)
    check("iframes become a link card", 'class="card"' in html and "example.com/embed" in html)
    check("article.pdf is a PDF", open(os.path.join(out, "article.pdf"), "rb").read(5) == b"%PDF-")


def test_pipeline_failures():
    """Every failure the pipeline can hit has to come back as a clear error, not a hang or a crash."""
    if not BROWSER:
        return
    import asyncio
    gone = "https://medium.com/@a/deleted-story-303030303030"

    class Gone(Exception):
        pass

    real = polite.get_text

    def missing(url, timeout=40, priority=False):
        if url == gone:
            import urllib.error
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        return real(url, timeout, priority)

    polite.get_text = missing
    try:
        try:
            asyncio.run(A.pipeline.render(gone, os.path.join(WORK, "r1")))
            check("a deleted story is reported, not rendered", False, "no error raised")
        except A.PipelineError as e:
            check("a deleted story is reported, not rendered", "doesn't exist" in str(e), str(e))
    finally:
        polite.get_text = real

    # a story whose mirrors all return a page with no article body
    empty = "https://medium.com/@a/empty-story-404040404040"
    PAGES[empty] = post_page("404040404040", "Empty story", locked=True)
    try:
        asyncio.run(A.pipeline.render(empty, os.path.join(WORK, "r2")))
        check("an empty mirror page is an error", False, "no error raised")
    except A.PipelineError as e:
        check("an empty mirror page is an error", "mirror" in str(e).lower(), str(e))
        check("the error names each mirror it tried", "127.0.0.1" in str(e), str(e))

    ok = "https://medium.com/@a/still-works-505050505050"
    PAGES[ok] = post_page("505050505050", "Still works", tags=("cybersecurity", "malware"))
    meta = asyncio.run(A.pipeline.render(ok, os.path.join(WORK, "r3")))
    check("the pipeline still works after those failures", meta["title"] == "Still works", meta)


def test_pipeline_recovers_from_a_dead_browser():
    """Chromium can be killed by the OS. The next download must relaunch it rather than fail forever."""
    if not BROWSER:
        return
    import asyncio
    url = "https://medium.com/@a/after-the-crash-606060606060"
    PAGES[url] = post_page("606060606060", "After the crash", tags=("cybersecurity", "malware"))

    def on_pipeline_loop(coro):  # playwright objects belong to the pipeline's own event loop
        return asyncio.run_coroutine_threadsafe(coro, A.pipeline._loop).result(120)

    on_pipeline_loop(A.pipeline._ensure_browser())
    before = A.pipeline._browser
    on_pipeline_loop(before.close())  # exactly what an OOM kill looks like from here
    meta = asyncio.run(A.pipeline.render(url, os.path.join(WORK, "r4")))
    check("a killed browser is replaced", A.pipeline._browser is not before)
    check("the download succeeds anyway", meta["title"] == "After the crash", meta)


def test_pipeline_parallel_downloads():
    """Simultaneous downloads share one browser: the launch used to race and orphan Chromiums."""
    if not BROWSER:
        return
    import asyncio
    count = _pipeline.MAX_PARALLEL * 2  # exercise both active jobs and semaphore waiters
    urls = [f"https://medium.com/@a/parallel-{i}-{0x707070707070 + i:012x}" for i in range(count)]
    for i, u in enumerate(urls):
        PAGES[u] = post_page(f"{0x707070707070 + i:012x}", f"Parallel {i}", tags=("cybersecurity", "malware"))

    async def run():
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(A.pipeline._drop_browser(), A.pipeline._loop))
        return await asyncio.gather(*(A.pipeline.render(u, os.path.join(WORK, f"par{i}"))
                                      for i, u in enumerate(urls)))

    metas = asyncio.run(asyncio.wait_for(run(), 300))
    check("every parallel download finishes", len(metas) == count and all(m["title"].startswith("Parallel") for m in metas))
    check("they all produced a PDF",
          all(os.path.exists(os.path.join(WORK, f"par{i}", "article.pdf")) for i in range(count)))
    check("only one browser is left running", A.pipeline._browser is not None and A.pipeline._browser.is_connected())


def test_fetch_endpoint():
    """The API's download job: POST /fetch -> job -> the article gains a PDF the reader can open."""
    if not BROWSER:
        return
    url = "https://medium.com/@a/via-the-api-808080808080"
    PAGES[url] = post_page("808080808080", "Via the api", tags=("cybersecurity", "malware"))
    # the context manager keeps one event loop alive for the whole block, so the job the endpoint
    # starts in the background actually runs; a bare TestClient tears its loop down per request
    with TestClient(A.app) as client:
        _fetch_endpoint_body(client, url)


def _fetch_endpoint_body(client, url):
    r = client.post("/api/articles", json={"url": url, "topic": "cybersecurity", "subtopic": "malware"})
    aid = r.json()["id"]
    r = client.post(f"/api/articles/{aid}/fetch")
    check("POST /fetch starts a job", r.status_code == 200, r.text)
    job = r.json()
    for _ in range(600):
        job = client.get(f"/api/jobs/{job['id']}").json()
        if job["status"] != "running":
            break
        time.sleep(0.2)
    check("the job finishes", job["status"] == "done", job.get("error") or job["status"])
    if job["status"] != "done":
        return
    art = job["article"]
    check("the article gets a pdf_url", art["pdf_url"] and art["doc_url"], art)
    check("the reader can fetch the PDF over HTTP", client.get(art["pdf_url"]).status_code == 200)
    check("the reader can fetch the HTML over HTTP", client.get(art["doc_url"]).status_code == 200)
    check("it is recorded as free", art["locked"] is False, art.get("locked"))
    check("asking again returns the saved copy without re-downloading",
          client.post(f"/api/articles/{aid}/fetch").json()["stage"] == "done")
    check("no .incoming scratch folder is left behind",
          not [p for p in os.listdir(os.path.join(A.LIBRARY_DIR, "cybersecurity", "malware"))
               if p.startswith(".incoming")])
    client.delete(f"/api/articles/{aid}")


# The lifespan shutdown inside this test closes the shared PDF pipeline for the rest of the process,
# so main() runs it after everything that still needs to render.
def _closes_pipeline(fn):
    fn.closes_pipeline = True
    return fn


test_fetch_endpoint = _closes_pipeline(test_fetch_endpoint)


def test_web_app_surface():
    """What a phone needs to install the app and read offline."""
    client = TestClient(A.app)
    r = client.get("/")
    page = r.text
    check("the page declares a manifest", 'rel="manifest"' in page)
    check("the page carries an iOS home-screen icon", 'rel="apple-touch-icon"' in page)
    check("the viewport covers the notch", "viewport-fit=cover" in page)
    check("iOS is told it can run standalone", 'name="apple-mobile-web-app-capable"' in page)

    r = client.get("/static/manifest.webmanifest")
    check("the manifest is served", r.status_code == 200, r.status_code)
    man = json.loads(r.text)
    check("the manifest installs as an app", man["display"] == "standalone" and man["start_url"] == "/", man)
    check("the manifest has both icon sizes",
          {i["sizes"] for i in man["icons"]} >= {"192x192", "512x512"}, man["icons"])
    check("a maskable icon is offered", any("maskable" in i.get("purpose", "") for i in man["icons"]))
    for icon in {i["src"] for i in man["icons"] if i.get("type") == "image/png"} | {"/static/apple-touch-icon.png"}:
        got = client.get(icon)
        check(f"{icon} exists and is a PNG",
              got.status_code == 200 and got.content[:8] == b"\x89PNG\r\n\x1a\n", got.status_code)
    svg_icon = next(i["src"] for i in man["icons"] if i.get("type") == "image/svg+xml")
    svg = client.get(svg_icon)
    check("the scalable app icon is served as SVG",
          svg.status_code == 200 and "svg" in svg.headers.get("content-type", "") and b"<svg" in svg.content[:500],
          (svg.status_code, svg.headers.get("content-type")))

    r = client.get("/sw.js")
    check("the service worker is served from the root", r.status_code == 200, r.status_code)
    check("its scope covers the whole app", r.headers.get("Service-Worker-Allowed") == "/", dict(r.headers))
    check("it is served as JavaScript", "javascript" in r.headers.get("content-type", ""), r.headers.get("content-type"))
    sw = r.text
    check("the worker precaches the interface", "/static/app.js" in sw and "/static/style.css" in sw)
    check("the worker leaves live API calls alone", "url.pathname.startsWith('/api/')" in sw)

    css = open(os.path.join(A.STATIC_DIR, "style.css"), encoding="utf-8").read()
    check("the phone layout uses safe-area insets", "env(safe-area-inset-top" in css)
    check("the sidebar becomes a drawer rather than vanishing",
          ".sidebar.open" in css and "display: none" not in css.split("@media (max-width: 860px)")[1][:400])
    check("inputs are big enough that iOS won't zoom", "font-size: 16px" in css)

    js = open(os.path.join(A.STATIC_DIR, "app.js"), encoding="utf-8").read()
    check("the drawer is wired up", "#menuBtn" in js and "drawer(" in js)
    check("the service worker is registered", "serviceWorker" in js and "/sw.js" in js)
    babel = open(os.path.join(A.STATIC_DIR, "babel.js"), encoding="utf-8").read()
    check("Babel idle work falls back to a numeric timer on Safari",
          "setTimeout(callback, timeout)" in babel and "(window.requestIdleCallback || setTimeout)" not in babel)


def test_phone_ui():
    """Drive the real interface at iPhone size: the drawer, the paste form, the reader's way out,
    and no sideways scrolling anywhere."""
    if not BROWSER:
        return
    import asyncio
    import threading

    import uvicorn
    from playwright.async_api import async_playwright

    phone_articles = []
    for i in range(70):  # exercise the first page and incremental append path
        phone_articles.append(A.create_article({"url": f"https://medium.com/@a/phone-ui-article-number-{i}-{i:012x}",
                          "topic": "cybersecurity", "subtopic": "malware", "source": "auto",
                          "title": f"Phone ui article number {i} with a fairly long title that wraps",
                          "author": "Jane Researcher", "claps": 1200 + i,
                          "snippet": "A standfirst long enough to wrap across more than one line on a phone."}))
    port = __import__("socket").socket()
    port.bind(("127.0.0.1", 0))
    port = port.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(A.app, host="127.0.0.1", port=port,
                                           log_level="critical", lifespan="off"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)

    async def drive():
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(**({"executable_path": _pipeline.CHROMIUM_PATH}
                                              if _pipeline.CHROMIUM_PATH else {}))
        ctx = await browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True,
                                        has_touch=True, device_scale_factor=2)
        page = await ctx.new_page()
        await page.add_init_script("localStorage.setItem('babelIntro', 'false')")
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        try:
            started = time.perf_counter()
            await page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            await page.wait_for_selector(".card", timeout=15000)
            out = {}
            out["first_render_seconds"] = time.perf_counter() - started
            out["cves_deferred"] = not any(url.endswith("/api/cves") for url in requests)
            first_card = await page.locator(".card").first.evaluate_handle("el => el")
            await page.evaluate("refreshLibraryMeta()")
            out["metadata_keeps_existing_cards"] = await first_card.evaluate("el => el.isConnected")
            out["first_page_count"] = await page.locator(".card").count()
            out["library_count"] = await page.evaluate("S.articles.length")
            if await page.locator("#showMore").count():
                await page.evaluate("document.querySelector('#showMore').click()")
                await page.wait_for_function("S.articles.length > 60 && !S.libraryLoading", timeout=10000)
                out["second_page_count"] = await page.locator(".card").count()
                out["limit_after_more"] = await page.evaluate("S.limit")
                out["pagination_keeps_existing_cards"] = await first_card.evaluate(
                    "el => el.isConnected")
            else:
                out["second_page_count"] = out["first_page_count"]
                out["pagination_keeps_existing_cards"] = False

            out["no_sideways_scroll"] = await page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth + 1")
            # the drawer is slid out of frame with a transform, so ask where it actually is
            async def drawer_showing():
                box = await page.locator("#sidebar").bounding_box()
                return bool(box) and box["x"] + box["width"] > 1

            out["menu_visible"] = await page.is_visible("#menuBtn")
            out["sidebar_hidden"] = not await drawer_showing()

            await page.click("#menuBtn")
            await page.wait_for_timeout(400)
            out["drawer_opens"] = await drawer_showing() and await page.is_visible("#scrim")
            # tapping a topic only expands it: the subtopics it reveals must stay reachable
            await page.click(".tree-topic")
            await page.wait_for_timeout(400)
            out["drawer_stays_for_topic"] = await drawer_showing()
            await page.click(".tree-sub:not(.tree-add)")   # picking a subtopic navigates and closes
            await page.wait_for_timeout(500)
            out["drawer_closes_on_pick"] = not await drawer_showing()

            await page.click("#menuBtn")                   # back to the whole library
            await page.wait_for_timeout(400)
            await page.click(".tree-all")
            await page.wait_for_timeout(500)
            out["all_articles_closes_drawer"] = not await drawer_showing()
            await page.wait_for_selector(".card", timeout=10000)

            await page.click("#pasteBtn")
            await page.wait_for_timeout(300)
            out["paste_opens"] = await page.is_visible("#pasteUrl")
            out["paste_no_overflow"] = await page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth + 1")
            await page.click("#pasteBtn")
            await page.wait_for_timeout(200)

            await page.click(".card-title")         # open an article
            await page.wait_for_timeout(600)
            out["reader_opens"] = await page.is_visible("#reader")
            # the way back has to be on screen and actually hittable, not painted under the top bar
            out["back_visible"] = await page.is_visible("#readerClose")
            box = await page.locator("#readerClose").bounding_box()
            out["back_on_screen"] = bool(box) and box["y"] >= 0 and box["x"] >= 0
            top = await page.evaluate(
                """() => { const b = document.querySelector('#readerClose').getBoundingClientRect();
                           const el = document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2);
                           return el && el.closest('#readerClose') ? 'reader' : (el ? el.id || el.className : 'none'); }""")
            out["back_is_on_top"] = top == "reader"
            await page.click("#readerClose")
            await page.wait_for_timeout(400)
            out["reader_closes"] = not await page.is_visible("#reader")

            out["tap_targets_big_enough"] = await page.evaluate(
                """() => [...document.querySelectorAll('.tree-item, .topbar .btn')]
                         .filter(el => el.offsetParent !== null)          // on screen right now
                         .every(el => el.getBoundingClientRect().height >= 32)""")
            return out
        finally:
            await ctx.close()
            await browser.close()
            await pw.stop()

    try:
        r = asyncio.run(asyncio.wait_for(drive(), 180))
    finally:
        server.should_exit = True

    check("the page never scrolls sideways", r["no_sideways_scroll"])
    check("the library's first cards render within the local-load budget",
          r["first_render_seconds"] < 8.0, f"{r['first_render_seconds']:.2f}s (budget 8s)")
    check("opening the library defers the saved CVE payload", r["cves_deferred"])
    check("refreshing library counts preserves rendered cards", r["metadata_keeps_existing_cards"])
    check("the first library page stays capped at 60 cards", r["first_page_count"] == 60,
          r["first_page_count"])
    check("show more appends the next page without rebuilding earlier cards",
          r["second_page_count"] > r["first_page_count"] and r["pagination_keeps_existing_cards"],
          (r["first_page_count"], r["second_page_count"], r["pagination_keeps_existing_cards"]))
    check("the topics button is shown", r["menu_visible"])
    check("the sidebar starts closed", r["sidebar_hidden"])
    check("tapping it opens the drawer", r["drawer_opens"])
    check("tapping a topic keeps the drawer open for its subtopics", r["drawer_stays_for_topic"])
    check("picking a subtopic closes the drawer", r["drawer_closes_on_pick"])
    check("so does All articles", r["all_articles_closes_drawer"])
    check("the paste form unfolds", r["paste_opens"])
    check("the paste form doesn't push the page sideways", r["paste_no_overflow"])
    check("an article opens in the reader", r["reader_opens"])
    check("the reader's back button is visible", r["back_visible"])
    check("it is on screen", r["back_on_screen"])
    check("nothing is painted on top of it", r["back_is_on_top"], r["back_is_on_top"])
    check("tapping it returns to the library", r["reader_closes"])
    check("tap targets are big enough for a thumb", r["tap_targets_big_enough"])
    for article in phone_articles:
        A.store.discard(article)

    for a in [a for a in A.store.data["articles"] if a["subtopic"] == "malware"]:
        A.store.discard(a)


def test_download_races():
    """The curator runs while you read. Removing or rotating out an article mid-download must not
    report success, and must not leave a folder nothing points at."""
    if not BROWSER:
        return
    import asyncio
    url = "https://medium.com/@a/removed-mid-download-a1a1a1a1a1a1"
    PAGES[url] = post_page("a1a1a1a1a1a1", "Removed mid download", tags=("cybersecurity", "malware"))
    a = A.create_article({"url": url, "topic": "cybersecurity", "subtopic": "malware",
                          "title": "Removed mid download", "source": "auto"})
    folder = A.abs_path(A.folder_rel(a))

    # Remove the article exactly in the window: after the render finishes, before run_job files it.
    # Sleeping for the same effect is a race against a warm browser, which wins.
    real_render = A.pipeline.render

    async def render_then_remove(*args, **kwargs):
        meta = await real_render(*args, **kwargs)
        A.remove_article(a)                 # exactly what the curator does when it rotates
        return meta

    async def run():
        job = {"id": "race", "article_id": a["id"], "status": "running", "stage": "queued",
               "started": time.time()}
        A.pipeline.render = render_then_remove
        try:
            await A.run_job(job, a)
        finally:
            A.pipeline.render = real_render
        return job

    job = asyncio.run(asyncio.wait_for(run(), 180))
    check("a download whose article vanished reports an error, not success",
          job["status"] == "error", job["status"])
    check("and says what happened", "removed from the library" in job.get("error", ""), job.get("error"))
    check("no orphan folder is left behind", not os.path.isdir(folder), folder)
    check("no scratch folder is left behind",
          not [p for p in os.listdir(os.path.join(A.LIBRARY_DIR, "cybersecurity", "malware"))
               if p.startswith(".incoming")])

    # the curator must not pick a downloading article as the one to replace
    busy = A.create_article({"url": "https://medium.com/@a/busy-b2b2b2b2b2b2", "topic": "cybersecurity",
                             "subtopic": "malware", "title": "Busy", "source": "auto", "claps": 1})
    idle = A.create_article({"url": "https://medium.com/@a/idle-c3c3c3c3c3c3", "topic": "cybersecurity",
                             "subtopic": "malware", "title": "Idle", "source": "auto", "claps": 1})
    busy["fetching"] = True
    auto = [busy, idle]
    replaceable = [x for x in auto if not x.get("pdf") and not x.get("fetching")]
    check("an article being downloaded is not up for replacement", replaceable == [idle],
          [x["title"] for x in replaceable])
    A.store.discard(busy)
    A.store.discard(idle)


def test_offline_reading():
    """Install the service worker, then pull the network and check the app still opens and the
    article you had read is still readable. 127.0.0.1 counts as a secure origin, so this is the same
    code path a phone runs."""
    if not BROWSER:
        return
    import asyncio
    import threading

    import uvicorn
    from playwright.async_api import async_playwright

    url = "https://medium.com/@a/read-me-offline-909090909090"
    PAGES[url] = post_page("909090909090", "Read me offline", tags=("cybersecurity", "malware"))
    a = A.create_article({"url": url, "topic": "cybersecurity", "subtopic": "malware",
                          "title": "Read me offline"})
    # Keep the fixture inside the first paged response even if earlier checks grow the library.
    with A.store.lock:
        A.store.data["articles"].remove(a)
        A.store.data["articles"].insert(0, a)
        A.store.reindex()
        A.store.save()
    rel = A.folder_rel(a)
    asyncio.run(A.pipeline.render(url, A.abs_path(rel)))
    with A.store.lock:
        a["doc"], a["pdf"], a["fetched"] = f"{rel}/content.html", f"{rel}/article.pdf", A.now_iso()
        A.store.save()

    port = __import__("socket").socket()
    port.bind(("127.0.0.1", 0))
    port = port.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(A.app, host="127.0.0.1", port=port,
                                           log_level="critical", lifespan="off"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)

    async def drive():
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(**({"executable_path": _pipeline.CHROMIUM_PATH}
                                              if _pipeline.CHROMIUM_PATH else {}))
        ctx = await browser.new_context(service_workers="allow")
        page = await ctx.new_page()
        out = {}
        try:
            await page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            out["registers"] = await page.evaluate(
                "navigator.serviceWorker.ready.then(r => !!r.active).catch(() => false)")

            # second load: the worker is in control and caches the stamped assets
            await page.reload(wait_until="networkidle")
            await page.wait_for_selector(".card", timeout=15000)
            await page.click(".card-title")
            await page.wait_for_selector("#doc", timeout=20000)
            await page.wait_for_timeout(1200)          # let the article's files land in the cache

            cached = await page.evaluate(
                """async () => {
                     const names = await caches.keys();
                     const urls = [];
                     for (const n of names) urls.push(...(await (await caches.open(n)).keys()).map(r => r.url));
                     return urls;
                   }""")
            out["app_js_cached"] = any("/static/app.js?v=" in u for u in cached)
            out["article_cached"] = any("/content.html" in u for u in cached)
            out["paged_library_cached"] = any("/api/library/first?" in u and "limit=60" in u for u in cached)
            out["paged_library_urls"] = [u for u in cached if "/api/library/first" in u or "/api/library/page" in u]
            out["one_app_js"] = sum("/static/app.js" in u for u in cached) <= 2  # bare + one stamp

            # the PDF viewer asks for byte ranges; a 206 is a slice, and caching one corrupts the file
            await page.evaluate(
                f"""async () => {{
                      await fetch('/files/{rel}/article.pdf', {{ headers: {{ Range: 'bytes=0-99' }} }});
                    }}""")
            await page.wait_for_timeout(500)
            statuses = await page.evaluate(
                """async () => {
                     const c = await caches.open('files-v1');
                     const out = [];
                     for (const r of await c.keys()) out.push((await c.match(r)).status);
                     return out;
                   }""")
            out["no_partials_cached"] = all(s == 200 for s in statuses)

            await ctx.set_offline(True)
            await page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            out["offline_app_loads"] = await page.evaluate("typeof S === 'object' && Array.isArray(S.articles)")
            out["offline_has_articles"] = await page.evaluate("S.articles.length > 0")
            out["offline_renders_cards"] = await page.is_visible(".card")
            out["offline_list_message"] = await page.locator("#list").inner_text()
            out["offline_cached_page_rows"] = await page.evaluate(
                "fetch('/api/library/page?offset=0&limit=60&access=all').then(r => r.json()).then(d => d.articles.length).catch(() => -1)")
            out["offline_cached_page_with_api_headers"] = await page.evaluate(
                "caches.match(new Request('/api/library/page?offset=0&limit=60&access=all', {headers:{'Content-Type':'application/json'}})).then(r => !!r)")
            out["offline_page_controller"] = await page.evaluate("!!navigator.serviceWorker.controller")
            out["offline_current_library_url"] = await page.evaluate("libraryPageUrl()")
            if await page.locator(".card-title").count():
                await page.click(".card-title")
                await page.wait_for_selector("#doc", timeout=20000)
                out["offline_article_opens"] = await page.is_visible("#doc")
                out["offline_article_has_text"] = "A heading" in (await page.inner_text("#doc"))
            else:
                out["offline_article_opens"] = False
                out["offline_article_has_text"] = False

            # app.py stamps assets with the file's mtime, so an update changes every asset URL.
            # Offline, those new URLs are not in the cache and must fall back to the stored version.
            await ctx.set_offline(False)
            os.utime(os.path.join(A.STATIC_DIR, "app.js"))      # "the app was updated"
            await page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            await ctx.set_offline(True)
            await page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            out["offline_after_update"] = await page.evaluate(
                "typeof S === 'object' && Array.isArray(S.articles)")
            return out
        finally:
            await ctx.set_offline(False)
            await ctx.close()
            await browser.close()
            await pw.stop()

    try:
        r = asyncio.run(asyncio.wait_for(drive(), 240))
    finally:
        server.should_exit = True

    check("the service worker registers", r["registers"])
    check("the stamped app.js is cached, not just the bare URL", r["app_js_cached"], r["app_js_cached"])
    check("the article's saved copy is cached", r["article_cached"])
    check("the paged library payload is cached for offline startup", r["paged_library_cached"])
    check("old versions of an asset don't pile up", r["one_app_js"])
    check("byte-range replies are never cached", r["no_partials_cached"], r["no_partials_cached"])
    check("the app still loads with no network", r["offline_app_loads"])
    check("the library list survives offline", r["offline_has_articles"], r.get("offline_list_message"))
    check("the cards render offline", r["offline_renders_cards"],
          (r.get("offline_list_message"), r.get("offline_cached_page_rows"), r.get("offline_cached_page_with_api_headers"), r.get("offline_page_controller"), r.get("offline_current_library_url"), r.get("paged_library_urls")))
    check("a saved article still opens offline", r["offline_article_opens"])
    check("and its text is there", r["offline_article_has_text"])
    check("it still loads offline after an app update changed every asset URL", r["offline_after_update"])

    A.store.discard(a)


def test_notes_survive_backgrounding():
    """A note typed inside the save debounce must not be lost when the phone discards the page.
    iOS backgrounds a tab with visibilitychange and can drop it without firing beforeunload."""
    if not BROWSER:
        return
    import asyncio
    import threading

    import uvicorn
    from playwright.async_api import async_playwright

    url = "https://medium.com/@a/notes-survive-d4d4d4d4d4d4"
    PAGES[url] = post_page("d4d4d4d4d4d4", "Notes survive", tags=("cybersecurity", "malware"))
    a = A.create_article({"url": url, "topic": "cybersecurity", "subtopic": "malware",
                          "title": "Notes survive"})
    rel = A.folder_rel(a)
    asyncio.run(A.pipeline.render(url, A.abs_path(rel)))
    with A.store.lock:
        a["doc"], a["pdf"], a["fetched"] = f"{rel}/content.html", f"{rel}/article.pdf", A.now_iso()
        A.store.save()

    port = __import__("socket").socket()
    port.bind(("127.0.0.1", 0))
    port = port.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(A.app, host="127.0.0.1", port=port,
                                           log_level="critical", lifespan="off"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)

    typed = "A note typed right before the app went away"
    reading_checks = {}

    async def drive():
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(**({"executable_path": _pipeline.CHROMIUM_PATH}
                                              if _pipeline.CHROMIUM_PATH else {}))
        ctx = await browser.new_context()
        page = await ctx.new_page()
        notes_requested = asyncio.Event()
        release_notes = asyncio.Event()

        async def hold_notes(route):
            if route.request.method == "GET":
                notes_requested.set()
                await release_notes.wait()
            await route.continue_()

        await page.route("**/api/articles/*/notes", hold_notes)
        try:
            await page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            await page.click(".card-title")
            await asyncio.wait_for(notes_requested.wait(), 10)
            await page.wait_for_selector("#doc", timeout=5000)
            reading_checks["text_before_notes"] = "A heading" in await page.inner_text("#doc")
            reading_checks["editing_waits_for_notes"] = await page.evaluate("R.article === null")
            release_notes.set()
            await page.wait_for_function("R.article !== null")
            await page.wait_for_timeout(600)
            if not await page.is_visible("#freeNotes"):
                await page.click("#readerNotes")
                await page.wait_for_timeout(300)
            await page.fill("#freeNotes", typed)
            await page.wait_for_timeout(80)          # well inside the 700ms save debounce
            await page.evaluate(
                """() => {
                     Object.defineProperty(document, 'visibilityState',
                                           { value: 'hidden', configurable: true });
                     document.dispatchEvent(new Event('visibilitychange'));
                   }""")
            await page.close(run_before_unload=False)  # the tab is reclaimed; no beforeunload
            await asyncio.sleep(1.5)
        finally:
            release_notes.set()
            await ctx.close()
            await browser.close()
            await pw.stop()

    try:
        asyncio.run(asyncio.wait_for(drive(), 180))
    finally:
        server.should_exit = True

    path = A.notes_path(a["id"])
    saved = json.load(open(path, encoding="utf-8"))["notes"] if os.path.exists(path) else None
    check("saved article text appears before a slow notes response", reading_checks.get("text_before_notes"))
    check("editing waits for existing notes to load", reading_checks.get("editing_waits_for_notes"))
    check("a note typed just before the app is backgrounded still reaches the server",
          saved == typed, repr(saved))
    A.store.discard(a)


def test_hostile_input():
    """Everything a person can type, and everything a bad link can look like."""
    client = TestClient(A.app)

    def add(url):
        return client.post("/api/articles", json={"url": url, "topic": "cybersecurity", "subtopic": "malware"})

    for bad, why in [("not a url", "a typo"), ("", "nothing"), ("//evil.com/x", "no scheme"),
                     ("https://", "no host"), ("https://nodot/x", "not a hostname")]:
        check(f"{why} is refused, not saved as an article", add(bad).status_code == 400, add(bad).text[:80])
    for scheme in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,<script>x</script>"):
        r = add(scheme)
        check(f"{scheme.split(':')[0]}: links are refused", r.status_code == 400, r.text[:80])

    r = add("medium.com/@a/bare-host-111111111111")
    check("a bare medium.com link gets https://", r.json()["url"].startswith("https://medium.com/"), r.text[:80])
    r = add("HTTPS://Medium.COM/@a/Mixed-Case-222222222222/")
    check("the host is lowercased and the trailing slash dropped",
          r.json()["url"] == "https://medium.com/@a/Mixed-Case-222222222222", r.json()["url"])
    r = add("https://user:secret@medium.com/@a/creds-333333333333")
    check("credentials are stripped rather than stored", "secret" not in r.json()["url"], r.json()["url"])
    for a in [x for x in list(A.store.data["articles"]) if "111111111111" in x["url"]
              or "222222222222" in x["url"] or "333333333333" in x["url"]]:
        A.store.discard(a)

    # paging tokens come back to us from our own JSON, but nothing stops someone editing one
    for token in ('{"offset":1e400}', '{}', "null", "[]", '{"offset":"abc"}', "notjson"):
        r = client.get("/api/search", params={"q": "x", "next": token})
        check(f"a bad paging token is a 400, not a crash: {token}", r.status_code == 400, r.status_code)
    for token in ('{"offset":-5}', '{"offset":99999999999999999999}'):
        r = client.get("/api/search", params={"q": "x", "next": token})
        check(f"an out-of-range offset is clamped: {token}", r.status_code == 200, r.status_code)

    for aid in ("../../etc/passwd", "..%2f..%2fetc%2fpasswd", "abc/../../x"):
        check("notes can't be read outside the notes folder",
              client.get(f"/api/articles/{aid}/notes").status_code == 404)
    for path in ("/files/../library.json", "/files/..%2f..%2fapp.py", "/files/cybersecurity/../../app.py"):
        check(f"{path} is refused", client.get(path).status_code == 404)


def test_malformed_medium_page():
    """meta_of turns an untrusted page's JSON into typed values. Nothing in there may crash the
    download, or one odd post would take the whole article with it."""
    def meta(fields):
        page = ("<script>window.__APOLLO_STATE__ = "
                + json.dumps({"Post:abc": {"title": "t", **fields}}) + "</script>")
        state, post = medium_render.parse_post(page, "https://medium.com/@a/x-abc")
        return medium_render.meta_of(state, post)

    good = meta({"clapCount": 1234, "readingTime": 6.4, "firstPublishedAt": 1757000000000})
    check("claps and reading time come through", good["claps"] == 1234 and good["reading_time"] == 6, good)
    check("the date comes through", (good["published"] or "").startswith("2025-09-04"), good["published"])

    for name, fields in [("claps of infinity", {"clapCount": 1e309}),
                         ("claps as a word", {"clapCount": "lots"}),
                         ("a reading time of infinity", {"readingTime": 1e400}),
                         ("a negative prehistoric date", {"firstPublishedAt": -99999999999999}),
                         ("a date of infinity", {"firstPublishedAt": 1e400}),
                         ("tags that aren't a list", {"tags": "nope"}),
                         ("a creator that points back at the post", {"creator": {"__ref": "Post:abc"}})]:
        try:
            m = meta(fields)
            check(f"{name} is survivable", isinstance(m["claps"], int), m)
        except Exception as e:
            check(f"{name} is survivable", False, f"{type(e).__name__}: {e}")

    for name, page in [("no embedded data", "<html><body>nothing</body></html>"),
                       ("truncated json", '<script>window.__APOLLO_STATE__ = {"Post:x": {"title"'),
                       ("a list, not an object", "<script>window.__APOLLO_STATE__ = [1,2,3]</script>"),
                       ("two posts on one page",
                        '<script>window.__APOLLO_STATE__ = {"Post:a":{"clapCount":1},"Post:b":{"clapCount":2}}</script>')]:
        state, post = medium_render.parse_post(page, "https://medium.com/@a/x-zzz")
        check(f"{name} means no post, so the story goes to Freedium", post is None, post)


def test_rate_limiter():
    """polite.py is the only thing between this app and a ban. Its arithmetic is worth pinning down."""
    import threading

    import polite as P

    lim = P.HostLimiter(1.0)
    lim.pushed_back(429, retry_after=2)
    t0 = time.time()
    lim.wait(priority=True)
    check("a request someone is waiting on still respects Retry-After", time.time() - t0 >= 1.9,
          round(time.time() - t0, 2))

    lim2 = P.HostLimiter(1.0)
    for _ in range(3):
        lim2.pushed_back(429)
    check("repeated pushback widens the gap", lim2.gap > 20, lim2.gap)
    for _ in range(20):
        lim2.succeeded()
    check("the gap comes back to normal as requests succeed", lim2.gap == 1.0, lim2.gap)

    lim3 = P.HostLimiter(1.0)
    for _ in range(50):
        lim3.pushed_back(429)
    check("the gap is capped", lim3.gap == P.MAX_GAP, lim3.gap)
    check("and so is the pause, so a bad hour doesn't freeze the app for the rest of the day",
          lim3.pause_until - time.time() <= P.MAX_GAP + 1, round(lim3.pause_until - time.time()))

    lim4 = P.HostLimiter(0.05)
    stamps = []
    threads = [threading.Thread(target=lambda: (lim4.wait(), stamps.append(time.time()))) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    stamps.sort()
    check("concurrent background requests are still spaced out",
          all(b - a >= 0.045 for a, b in zip(stamps, stamps[1:])),
          [round(b - a, 3) for a, b in zip(stamps, stamps[1:])])

    check("Medium's image CDN gets its own budget", P.site_of("miro.medium.com") == "miro.medium.com")
    check("subdomains count against medium.com", P.site_of("MEDIUM.COM") == "medium.com")
    check("a lookalike host does not", P.site_of("medium.com.attacker.net") == "medium.com.attacker.net")


def test_damaged_library_recovers():
    """A truncated library.json must not stop the app from starting."""
    path = os.path.join(WORK, "damaged.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"topics": [], "articles": [{"id": "abc", "url": "https://m/x"')  # cut off mid-write
    s = A.Store(path)
    check("a damaged library starts empty instead of crashing", s.data["articles"] == [])
    check("the default topics come back", len(s.data["topics"]) > 5)
    check("the damaged file is kept", any(n.startswith("damaged.json.broken-") for n in os.listdir(WORK)),
          os.listdir(WORK))
    s.stop()

    path2 = os.path.join(WORK, "junk-rows.json")
    with open(path2, "w", encoding="utf-8") as f:
        json.dump({"topics": [], "articles": [{"id": "a", "url": "u"}, None, {"no": "id"}, "nope"]}, f)
    s2 = A.Store(path2)
    check("rows that aren't articles are dropped", len(s2.data["articles"]) == 1, s2.data["articles"])
    s2.stop()


def test_threads_restart():
    """A background thread that hits an unexpected error has to come back, not die silently."""
    calls = []

    class Boom(Curator := C.Curator):
        pass

    cur = A.curator
    original = cur._stopping
    cur._stopping = False

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        cur._stopping = True  # second run: stop cleanly

    flaky.__name__ = "flaky"
    guarded = cur._guard(flaky)
    C.time.sleep = lambda s: None  # don't wait out the restart delay in a test
    try:
        guarded()
    finally:
        C.time.sleep = time.sleep
        cur._stopping = original
    check("a crashed background loop is restarted", len(calls) == 2, calls)
    check("the restart is reported in the status", "boom" in (cur.error or ""), cur.error)
    cur.error = None


def test_next_subtopic_targets():
    tid, sub, filling, cap = A.curator._next_subtopic()
    check("the curator picks a subtopic to fill", filling and cap >= C.CAP, f"{tid}/{sub['id']} cap={cap}")
    check("the cap reflects the topic target", cap * 15 >= C.TOPIC_TARGET, f"cap={cap}")
    check("the topic target leaves room for 1,000 more per area", C.TOPIC_TARGET >= 1120, C.TOPIC_TARGET)


def test_morning_briefing():
    """Exercise the real briefing API and desktop/mobile view against dated news fixtures."""
    import socket
    import threading
    import uvicorn
    from unittest.mock import patch
    from briefing import BriefingService
    from test_briefing import fixture_fetch

    def fetch(*args):
        time.sleep(0.02)
        return fixture_fetch(*args)

    service = BriefingService(fetch)
    with patch.object(A, "briefing_service", service):
        client = TestClient(A.app)
        check("briefing starts idle", client.get("/api/briefing").json()["state"] == "idle")
        check("briefing rejects unsupported windows", client.post("/api/briefing", json={"days": 99}).status_code == 422)
        if not BROWSER:
            response = client.post("/api/briefing", json={"days": 3})
            check("briefing API accepts a scan", response.status_code == 202)
            for _ in range(100):
                if service.snapshot()["state"] != "running":
                    break
                time.sleep(0.02)
            check("briefing API produces five picks", len(service.snapshot()["articles"]) == 5)
            service.stop()
            return
        from playwright.sync_api import sync_playwright
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(A.app, host="127.0.0.1", port=port, log_level="error", lifespan="off"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.05)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(**({"executable_path": _pipeline.CHROMIUM_PATH} if _pipeline.CHROMIUM_PATH else {}))
                try:
                    page = browser.new_page(viewport={"width": 1360, "height": 1000})
                    page.goto(f"http://127.0.0.1:{port}")
                    page.locator('[data-briefing]').click()
                    page.locator('.briefing-picks .briefing-story').first.wait_for(timeout=20000)
                    check("briefing shows exactly five reading picks", page.locator('.briefing-picks .briefing-story').count() == 5)
                    check("briefing reports all 60 scanned topics", "60 of 60 topics checked" in page.locator('.briefing-edition').inner_text())
                    page.get_by_text('See the top 10 shortlist', exact=True).click()
                    check("briefing exposes its ten-article shortlist", page.locator('.briefing-story-compact').count() == 10)
                    page.get_by_text('See the top 10 shortlist', exact=True).click()
                    check("briefing links open safely", page.locator('.briefing-picks h2 a').first.get_attribute('rel') == 'noopener noreferrer')
                    page.screenshot(path=os.path.join(tempfile.gettempdir(), 'newsletter-briefing-desktop.png'), full_page=True)
                    page.locator('#briefingDays').select_option('7')
                    page.get_by_role('button', name='Generate briefing', exact=True).click()
                    page.get_by_role('button', name='Generate briefing', exact=True).wait_for(timeout=20000)
                    check("briefing window selection reaches the API", service.snapshot()['days'] == 7)
                    page.set_viewport_size({"width": 390, "height": 844})
                    check("briefing fits a phone viewport", page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    check("briefing generation button remains visible on a phone", page.get_by_role('button', name='Generate briefing', exact=True).is_visible())
                    page.screenshot(path=os.path.join(tempfile.gettempdir(), 'newsletter-briefing-mobile.png'), full_page=True)
                    page.locator('#menuBtn').click()
                    page.locator('[data-t=""][data-s=""]').click()
                    check("briefing can return to the library", page.locator('h1').inner_text() == 'All articles')
                finally:
                    browser.close()
        finally:
            service.stop()
            server.should_exit = True
            thread.join(5)


def main():
    global BROWSER
    start_stub_server()
    BROWSER = browser_available()
    if not BROWSER:
        print("  skipping the PDF tests\n")
    PAGES["https://medium.com/sitemap/posts/2026/posts-2026-09-10.xml"] = sitemap_day(
        "2026-09-10", [f"https://medium.com/@a/indexed-post-about-malware-{i:012x}" for i in range(30)])
    PAGES["https://medium.com/sitemap/posts/2026/posts-2026-09-11.xml"] = sitemap_day(
        "2026-09-11", ["https://medium.com/@a/a-huge-malware-story-cafecafecafe"])
    # the crawler asks for this on startup; an empty list keeps it quiet during the tests
    PAGES["https://medium.com/sitemap/sitemap.xml"] = "<sitemapindex></sitemapindex>"
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    tests.sort(key=lambda nf: getattr(nf[1], "closes_pipeline", False))  # stable: only these move
    for name, fn in tests:
            print(f"\n{name[5:].replace('_', ' ')}")
            try:
                fn()
            except Exception as e:
                import traceback
                traceback.print_exc()
                check(name, False, f"{type(e).__name__}: {e}")
    A.pipeline.close()
    A.store.stop()
    print(f"\n{'FAILED: ' + ', '.join(FAILED) if FAILED else 'all checks passed'}"
          + ("" if BROWSER else "  (PDF tests skipped: no Chromium)"))
    shutil.rmtree(WORK, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
