"""Deterministic briefing checks; all news feeds are local fixtures."""
import base64
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import threading
import time
import unittest
from xml.sax.saxutils import escape

from briefing import BriefingService, TOPICS, feed_url, parse_feed, select_briefing

NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


def rss_item(title, published, url="https://example.com/story", source="Example"):
    return f"<item><title>{escape(title)}</title><link>{escape(url)}</link><source>{escape(source)}</source><pubDate>{escape(published)}</pubDate></item>"


def feed(*items):
    return ("<rss><channel>" + "".join(items) + "</channel></rss>").encode()


def candidate(i, group="AI", source=None):
    return {"title": f"Distinct {i} topic{i} development{i} report{i}",
            "url": f"https://example.com/{i}", "source": source or f"Publisher {i}",
            "published": NOW.isoformat(), "category": group, "topics": [f"Topic {i}"], "score": 6.0 - i / 100}


def fixture_fetch(group, query, start, end):
    i = next(i for i, topic in enumerate(TOPICS) if topic == (group, query))
    result = candidate(i, group)
    result.update(title=f"{query.title()}: development{i} report{i}", published=end.isoformat(), topics=[query])
    return [result]


class BriefingTests(unittest.TestCase):
    def test_exact_window_and_safe_links(self):
        start = NOW - timedelta(days=3)
        raw = feed(
            rss_item("Valid story - Example", format_datetime(NOW)),
            rss_item("At start", format_datetime(start), "https://example.com/start"),
            rss_item("Old", format_datetime(start - timedelta(seconds=1))),
            rss_item("Future", format_datetime(NOW + timedelta(seconds=1))),
            rss_item("Missing date", ""),
            rss_item("Bad link", format_datetime(NOW), "javascript:alert(1)"),
            rss_item("Bad host", format_datetime(NOW), "https://[invalid"),
        )
        items = parse_feed(raw, "AI", "artificial intelligence", start, NOW)
        self.assertEqual([x["title"] for x in items], ["Valid story", "At start"])

    def test_malformed_source_is_not_reported_as_success(self):
        with self.assertRaises(ValueError):
            parse_feed(b"<html><body>Denied</body></html>", "AI", "AI", NOW, NOW)

    def test_ten_then_five_and_diversity(self):
        rows = [candidate(i, "AI" if i < 10 else "Science", "One publisher" if i < 10 else f"Other {i}") for i in range(20)]
        result = select_briefing(rows)
        self.assertEqual(len(result["shortlist"]), 10)
        self.assertEqual(len(result["articles"]), 5)
        self.assertTrue({x["url"] for x in result["articles"]} <= {x["url"] for x in result["shortlist"]})
        self.assertGreater(len({x["category"] for x in result["articles"]}), 1)
        self.assertGreater(len({x["source"] for x in result["articles"]}), 1)

    def test_duplicates_and_fewer_results(self):
        one = candidate(1)
        clone = dict(one, url="https://other.example.com/same", topics=["Another topic"])
        result = select_briefing([one, clone, one])
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(len(result["articles"]), 1)
        self.assertEqual(set(result["articles"][0]["topics"]), {"Topic 1", "Another topic"})

    def test_scan_at_least_fifty_and_partial_failure(self):
        seen = []
        def fetch(group, query, start, end):
            seen.append(query)
            self.assertEqual(end, NOW)
            self.assertEqual(start, NOW - timedelta(days=3))
            if query == TOPICS[0][1]:
                raise OSError("Fixture outage")
            return fixture_fetch(group, query, start, end)
        service = BriefingService(fetch)
        service.start(3, NOW)
        job = self.wait(service)
        self.assertGreaterEqual(len(set(seen)), 50)
        self.assertEqual(job["topics_scanned"], len(TOPICS))
        self.assertEqual(job["topics_failed"], 1)
        self.assertEqual(job["topics_succeeded"], len(TOPICS) - 1)
        self.assertEqual(job["state"], "complete")
        self.assertEqual((len(job["shortlist"]), len(job["articles"])), (10, 5))

    def test_repeated_clicks_share_one_bounded_run(self):
        gate = threading.Event()
        active = peak = 0
        lock = threading.Lock()
        def fetch(*args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(active, peak)
            gate.wait(2)
            with lock:
                active -= 1
            return fixture_fetch(*args)
        service = BriefingService(fetch)
        try:
            first = service.start(1, NOW)
            second = service.start(7, NOW)
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(second["days"], 1)
        finally:
            gate.set()
        self.wait(service)
        self.assertLessEqual(peak, 4)
        next_job = service.start(7, NOW + timedelta(hours=1))
        self.wait(service)
        self.assertNotEqual(first["id"], next_job["id"])
        self.assertEqual(next_job["window_end"], (NOW + timedelta(hours=1)).isoformat())

    def test_all_failed_and_invalid_window(self):
        def fail(*args):
            raise OSError("Unavailable")
        service = BriefingService(fail)
        with self.assertRaises(ValueError):
            service.start(99)
        service.start(3, NOW)
        job = self.wait(service)
        self.assertEqual(job["state"], "error")
        self.assertEqual(job["articles"], [])
        self.assertEqual(job["topics_failed"], len(TOPICS))

    def test_feed_query_uses_click_dates(self):
        url = feed_url("AI chips", NOW - timedelta(days=3), NOW)
        self.assertIn("after%3A2026-10-01", url)
        self.assertIn("before%3A2026-10-05", url)

    def wait(self, service):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = service.snapshot()
            if job["state"] != "running":
                return job
            time.sleep(0.01)
        self.fail("Fixture scan did not complete")


class RankingTests(unittest.TestCase):
    def row(self, i, title, score, group="AI", source=None):
        return {"title": title, "url": f"https://ex.com/{i}", "source": source or f"Pub {i}",
                "published": NOW.isoformat(), "category": group, "topics": [f"t{i}"], "score": score}

    def test_interest_boost_lifts_followed_subjects(self):
        from briefing import interest_tokens, select_briefing
        interests = interest_tokens([{"name": "AI", "subtopics": [
            {"name": "LLMs", "tags": ["large-language-models"]}]}])
        self.assertIn("language", interests)
        # The boost is capped so it never swamps freshness; a close race is what it is meant to tip.
        rows = [self.row(0, "Weather stays mild this weekend", 4.8, "World"),
                self.row(1, "New large language models cut costs", 3.5, "AI")]
        result = select_briefing(rows, interests)
        top = result["articles"][0]
        self.assertEqual(top["url"], "https://ex.com/1")        # boosted past the fresher weather item
        self.assertGreater(top["score"], top["base_score"])     # the boost is recorded, not hidden
        self.assertEqual(rows[0]["score"], 4.8)                 # inputs are not mutated

    def test_clustering_groups_retellings_and_caps_dominance(self):
        from briefing import select_briefing
        rows = [self.row(0, "Apple unveils new M5 chip for laptops", 5.0, "Devices"),
                self.row(1, "Apple reveals the new M5 chip in laptops", 4.9, "Devices"),
                self.row(2, "Floods displace thousands across the region", 4.0, "World")]
        result = select_briefing(rows)
        clusters = {x["cluster"] for x in result["shortlist"]}
        self.assertEqual(len(result["articles"]), 2)            # the two M5 retellings collapse to one slot
        self.assertEqual(len(clusters), 2)
        m5 = next(x for x in result["shortlist"] if "M5" in x["title"])
        self.assertEqual(m5["related"], 1)


class ResolveTests(unittest.TestCase):
    def google(self, payload):
        seg = base64.urlsafe_b64encode(b"\x08\x13\x22" + payload).decode().rstrip("=")
        return f"https://news.google.com/rss/articles/{seg}?oc=5"

    def test_passthrough_leaves_publisher_urls(self):
        from briefing import resolve_article_url
        url = "https://www.bbc.com/news/world-123"
        self.assertEqual(resolve_article_url(url, fetch=self.fail), url)  # no fetch for a real link

    def test_decodes_embedded_publisher_url(self):
        from briefing import resolve_article_url
        url = self.google(b"https://www.reuters.com/tech/story-9")
        self.assertEqual(resolve_article_url(url, fetch=self.fail), "https://www.reuters.com/tech/story-9")

    def test_falls_back_to_scraping_landing_page(self):
        from briefing import resolve_article_url
        page = b'<a href="https://news.google.com/x">self</a><a href="https://apnews.com/article/abc">real</a>'
        url = "https://news.google.com/rss/articles/NOTBASE64?oc=5"
        self.assertEqual(resolve_article_url(url, fetch=lambda u, **k: page), "https://apnews.com/article/abc")

    def test_fetch_failure_returns_original(self):
        from briefing import resolve_article_url
        def boom(*a, **k):
            raise OSError("down")
        url = "https://news.google.com/rss/articles/NOTBASE64?oc=5"
        self.assertEqual(resolve_article_url(url, fetch=boom), url)


class ExtractTests(unittest.TestCase):
    def page(self, body):
        return ('<html><head><meta property="og:title" content="A Clear Title">'
                '<meta property="og:image" content="/img/hero.jpg"></head>'
                f'<body>{body}</body></html>').encode()

    def test_extracts_article_and_drops_chrome(self):
        from briefing import fetch_article
        body = ('<nav>site menu junk</nav><article><h2>Section</h2>'
                '<p>' + "The committee released its findings on Thursday afternoon. " * 12 + '</p>'
                '<ul><li>first point</li><li>second point</li></ul>'
                '<img src="/media/pic.png"></article><footer>footer junk</footer>')
        meta = fetch_article("https://news.site/story", fetch=lambda u, **k: self.page(body))
        self.assertEqual(meta["title"], "A Clear Title")
        self.assertEqual(meta["image"], "https://news.site/img/hero.jpg")
        self.assertIn("committee released", meta["body"])
        self.assertNotIn("menu junk", meta["body"])
        self.assertNotIn("footer junk", meta["body"])
        self.assertIn("<ul>", meta["body"])
        self.assertIn("<li>first point</li>", meta["body"])
        self.assertIn("</ul>", meta["body"])
        self.assertLess(meta["body"].index("<ul>"), meta["body"].index("<li>first point</li>"))
        self.assertIn('src="https://news.site/media/pic.png"', meta["body"])

    def test_thin_page_raises(self):
        from briefing import fetch_article
        with self.assertRaises(ValueError):
            fetch_article("https://news.site/x", fetch=lambda u, **k: self.page("<p>too short</p>"))

    def test_escapes_html_in_text(self):
        from briefing import fetch_article
        body = "<article><p>" + "Risk &amp; reward in &lt;markets&gt; this quarter. " * 12 + "</p></article>"
        meta = fetch_article("https://news.site/x", fetch=lambda u, **k: self.page(body))
        self.assertIn("&amp;", meta["body"])
        self.assertNotIn("<markets>", meta["body"])  # angle brackets from the source are neutralised


class ResolverWiringTests(unittest.TestCase):
    def wait(self, service):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = service.snapshot()
            if job["state"] != "running":
                return job
            time.sleep(0.01)
        self.fail("scan did not complete")

    def test_shortlist_urls_are_resolved(self):
        service = BriefingService(fixture_fetch, resolver=lambda u: u + "#r")
        service.start(3, NOW)
        job = self.wait(service)
        self.assertTrue(job["shortlist"])
        self.assertTrue(all(x["resolved_url"] == x["url"] + "#r" for x in job["shortlist"]))

    def test_without_resolver_url_is_unchanged(self):
        service = BriefingService(fixture_fetch)
        service.start(3, NOW)
        job = self.wait(service)
        self.assertTrue(all(x["resolved_url"] == x["url"] for x in job["shortlist"]))

    def test_fresh_edition_is_reused_without_now(self):
        calls = []
        def fetch(*a):
            calls.append(a[1])
            return fixture_fetch(*a)
        service = BriefingService(fetch)
        service.start(3)                 # real click: no `now`
        self.wait(service)
        runs = len(set(calls))
        service.start(3)                 # immediate repeat within the anti-thrash window
        self.wait(service)
        self.assertEqual(len(set(calls)), runs)   # reused, the news source was not hit again
        forced = service.start(3, force=True)
        self.wait(service)
        self.assertEqual(forced["state"] in ("running", "complete"), True)


if __name__ == "__main__":
    unittest.main()
