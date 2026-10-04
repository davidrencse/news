"""Deterministic briefing checks; all news feeds are local fixtures."""
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


if __name__ == "__main__":
    unittest.main()
