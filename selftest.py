"""Offline self-test: exercises the library API, the search index and the curator without touching
the network. Nothing here reaches Medium — sitemaps, post pages and feeds are all stubbed.

Run:  python selftest.py
"""
import json
import os
import shutil
import sys
import tempfile

WORK = tempfile.mkdtemp(prefix="medium-library-selftest-")
os.environ["MEDIUM_LIBRARY_DATA"] = WORK

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
from fastapi.testclient import TestClient  # noqa: E402

FAILED = []


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
    rows, mode = idx.search("malware", limit=50)
    check("indexed posts are searchable", len(rows) == 30 and mode == "all", f"{len(rows)} {mode}")
    check("titles come from the slug", rows[0]["title"].startswith("Indexed post about malware"), rows[0]["title"])
    rows, mode = idx.search("malware unrelatedword")
    check("falls back to matching any word", mode == "any" and rows, f"{mode} {len(rows)}")
    rows, _ = idx.search("")
    check("an empty query returns nothing", rows == [])


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
        PAGES[u] = post_page(u[-12:], f"Malware post {u[-4:]}", tags=("cybersecurity", "malware"))
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
    PAGES[hit] = post_page("cafecafecafe", "A huge malware story", claps=500000, tags=("cybersecurity", "malware"))
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


def test_next_subtopic_targets():
    tid, sub, filling, cap = A.curator._next_subtopic()
    check("the curator picks a subtopic to fill", filling and cap >= C.CAP, f"{tid}/{sub['id']} cap={cap}")
    check("the cap reflects the topic target", cap * 15 >= C.TOPIC_TARGET, f"cap={cap}")
    check("the topic target leaves room for 1,000 more per area", C.TOPIC_TARGET >= 1120, C.TOPIC_TARGET)


def main():
    PAGES["https://medium.com/sitemap/posts/2026/posts-2026-09-10.xml"] = sitemap_day(
        "2026-09-10", [f"https://medium.com/@a/indexed-post-about-malware-{i:012x}" for i in range(30)])
    PAGES["https://medium.com/sitemap/posts/2026/posts-2026-09-11.xml"] = sitemap_day(
        "2026-09-11", ["https://medium.com/@a/a-huge-malware-story-cafecafecafe"])
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            print(f"\n{name[5:].replace('_', ' ')}")
            try:
                fn()
            except Exception as e:
                import traceback
                traceback.print_exc()
                check(name, False, f"{type(e).__name__}: {e}")
    A.store.stop()
    print(f"\n{'FAILED: ' + ', '.join(FAILED) if FAILED else 'all checks passed'}")
    shutil.rmtree(WORK, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
