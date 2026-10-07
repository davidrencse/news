"""Regressions for defects found in the 2026-10-06 auditor pass (findings 1-11)."""
import asyncio
import http.client
import json
import os
import tempfile
import unittest
from unittest.mock import patch


class _FakeBrowser:
    def __init__(self, connected):
        self._connected, self.closed = connected, False

    def is_connected(self):
        return self._connected

    async def close(self):
        self.closed, self._connected = True, False


class AuditRegressions(unittest.TestCase):
    def test_relative_date_with_huge_count_is_not_a_date(self):
        # Finding 6: int() on a 5000-digit run trips Python's int-string limit; /api/search 500'd.
        import relevance
        self.assertIsNone(relevance._as_day("9" * 5000 + "d"))
        relevance.parse_query("since:" + "9" * 5000 + "d")  # must not raise

    def test_lone_surrogate_paragraph_renders(self):
        # Finding 7a: an unpaired surrogate in a paragraph must not abort the whole render.
        import medium_render
        out = medium_render.inline({}, "\ud83d hi", [{"type": "STRONG", "start": 0, "end": 2}])
        self.assertIn("hi", out)

    def test_junk_markup_offsets_do_not_raise(self):
        # Finding 7b: non-numeric / inf markup offsets become 0 via whole(), not an exception.
        import medium_render
        medium_render.inline({}, "hello", [{"type": "STRONG", "start": "x", "end": float("inf")}])

    def test_non_dict_paragraph_is_skipped(self):
        # Finding 8: a non-dict paragraph must be skipped, not crash render_body.
        import medium_render
        meta = {"title": "", "subtitle": ""}
        out = medium_render.render_body({}, ["a bare string", None, {"type": "P", "text": "ok"}], set(), meta)
        self.assertIn("ok", out)

    def test_notes_file_that_is_a_json_list_is_safe(self):
        # Finding 11: a notes file that is valid JSON but not an object raised AttributeError.
        import recommender
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(["not", "an", "object"], f)
            path = f.name
        try:
            self.assertFalse(recommender._has_notes(path))
        finally:
            os.unlink(path)

    def test_recommend_memo_is_size_capped(self):
        # Finding 9: the memo is bounded so a label-cycling client can't grow it without limit.
        import recommender
        self.assertTrue(0 < recommender.MEMO_MAX <= 256)

    def test_prepare_routes_dropped_connection_to_freedium(self):
        # Finding 2: a dropped/truncated connection must fall back to Freedium, not escape prepare()
        # (where local mode misreads it as a Chromium crash, and Telegram mode fails the download).
        import medium_render
        for err in (ConnectionResetError("reset"), http.client.IncompleteRead(b"partial")):
            with patch.object(medium_render.polite, "status", return_value={}), \
                    patch.object(medium_render.polite, "get_text", side_effect=err):
                route = medium_render.prepare("https://medium.com/x/story")
            self.assertEqual(route["route"], "freedium")


class PipelineBrowserDrop(unittest.TestCase):
    def test_drop_browser_only_discards_a_crashed_browser(self):
        # Finding 2b: _drop_browser must not close a live shared browser (other concurrent renders use
        # it); it may only discard one that has actually crashed.
        import pipeline
        pl = pipeline.PdfPipeline(tempfile.gettempdir())
        run = lambda coro: asyncio.run_coroutine_threadsafe(coro, pl._loop).result(5)
        try:
            live = _FakeBrowser(connected=True)
            pl._browser = live
            run(pl._drop_browser())
            self.assertIs(pl._browser, live)      # kept: still serving other renders
            self.assertFalse(live.closed)
            dead = _FakeBrowser(connected=False)
            pl._browser = dead
            run(pl._drop_browser())
            self.assertIsNone(pl._browser)         # dropped so the next render relaunches
            self.assertTrue(dead.closed)
        finally:
            pl._browser = None
            pl.close()


class _FakeCache:
    def __init__(self):
        self._d, self.rev = {}, 0

    def items(self):
        return list(self._d.items())

    def get(self, url):
        return self._d.get(url)

    def __len__(self):
        return len(self._d)

    def set(self, url, meta):
        self._d[url] = meta
        self.rev += 1


class RecommenderFreshness(unittest.TestCase):
    def test_all_labels_reflects_in_place_tag_updates(self):
        # Finding 10: the curator fills tags in place (len unchanged), so a len-only key served a stale
        # label picker. Keying on the cache revision fixes it.
        import recommender
        cache = _FakeCache()
        cache.set("u1", {"title": "t", "tags": ["python"]})
        rec = recommender.Recommender(None, cache, lambda a: None)
        self.assertEqual(rec.all_labels(), ["python"])
        cache.set("u1", {"title": "t", "tags": ["rust"]})  # same post, new tag; len unchanged, rev bumped
        self.assertEqual(rec.all_labels(), ["rust"])

    def test_post_home_recomputes_when_tags_are_filled_in(self):
        # Finding 10: an untagged post cached as home=None must re-home once the curator adds matching tags.
        import threading
        import recommender
        topics = [{"id": "tech", "name": "Tech",
                   "subtopics": [{"id": "py", "name": "Py", "tags": ["python"]}]}]

        class FakeStore:
            def __init__(self):
                self.data = {"articles": [], "topics": topics, "dismissed": {}}
                self.lock = threading.Lock()

        cache = _FakeCache()
        cache.set("u1", {"title": "A", "tags": [], "lang": "en"})
        rec = recommender.Recommender(FakeStore(), cache, lambda a: None)
        self.assertIsNone(rec._pool(None, None)["u1"][2])          # unsorted while untagged
        cache.set("u1", {"title": "A", "tags": ["python"], "lang": "en"})
        home = rec._pool(None, None)["u1"][2]
        self.assertIsNotNone(home)                                  # re-homed after tags arrive
        self.assertEqual(home[0], "tech")


if __name__ == "__main__":
    unittest.main()
