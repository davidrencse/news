"""Offline checks for pipeline tuning, source cooldowns, and backup fingerprints."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import polite
from performance import setting


class PerformanceTests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(setting("TEST_WORKERS", 4, 1, 16), 4)
        with patch.dict(os.environ, {"TEST_WORKERS": "8", "TEST_GAP": "0.5"}):
            self.assertEqual(setting("TEST_WORKERS", 4, 1, 16), 8)
            self.assertEqual(setting("TEST_GAP", 1.0, 0.1, 60), 0.5)

    def test_invalid_counts_are_rejected(self):
        for raw in ("0", "-1", "17", "2.5", "many", ""):
            with self.subTest(raw=raw), patch.dict(os.environ, {"TEST_WORKERS": raw}):
                with self.assertRaisesRegex(ValueError, "TEST_WORKERS"):
                    setting("TEST_WORKERS", 4, 1, 16)

    def test_nonfinite_intervals_are_rejected(self):
        for raw in ("nan", "inf", "-inf"):
            with self.subTest(raw=raw), patch.dict(os.environ, {"TEST_GAP": raw}):
                with self.assertRaisesRegex(ValueError, "TEST_GAP"):
                    setting("TEST_GAP", 1.0, 0.1, 60)

class CooldownTests(unittest.TestCase):
    """Source cooldowns: a server's Retry-After is honoured exactly, and a cooldown announced while a
    waiter sleeps is respected before it dispatches (see docs/efficiency-audit.md finding 1)."""

    def test_server_retry_after_is_honoured_beyond_max_gap(self):
        lim = polite.HostLimiter(1.0)
        before = polite.time.time()
        lim.pushed_back(429, retry_after=1200)  # longer than MAX_GAP (600)
        self.assertGreaterEqual(lim.pause_until - before, 1200 - 1)

    def test_fallback_pause_without_retry_after_is_capped(self):
        lim = polite.HostLimiter(1.0)
        lim.gap = polite.MAX_GAP
        before = polite.time.time()
        lim.pushed_back(429)  # no Retry-After: our own guess stays capped
        self.assertLessEqual(lim.pause_until - before, polite.MAX_GAP + 1)

    def test_wait_rechecks_cooldown_announced_while_sleeping(self):
        lim = polite.HostLimiter(0.0)  # no base gap, so only pause_until gates dispatch
        clock = {"now": 1000.0}
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock["now"] += seconds
            if len(sleeps) == 1:  # a pushback lands during the first sleep
                lim.pause_until = clock["now"] + 5

        with patch.object(polite.time, "time", lambda: clock["now"]), \
             patch.object(polite.time, "sleep", fake_sleep):
            lim.next_at = clock["now"] + 2  # reserved slot two seconds out
            lim.wait()
        self.assertGreaterEqual(len(sleeps), 2)  # slot, then again for the later cooldown
        self.assertGreaterEqual(clock["now"], 1007)


class BackupFingerprintTests(unittest.TestCase):
    """Unchanged files must not be re-hashed on every sync; changed files must be (finding 7)."""

    def test_unchanged_file_reuses_cached_digest(self):
        import sync_telegram
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "a.bin"
            path.write_bytes(b"hello world")
            old, new = {}, {}
            size1, digest1, rehashed1 = sync_telegram.file_digest(path, "a.bin", old, new)
            self.assertTrue(rehashed1)  # cold: must hash
            size2, digest2, rehashed2 = sync_telegram.file_digest(path, "a.bin", new, {})
            self.assertFalse(rehashed2)  # unchanged size+mtime: reuse
            self.assertEqual(digest1, digest2)
            self.assertEqual(size1, size2)

    def test_modified_file_is_rehashed(self):
        import sync_telegram
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "a.bin"
            path.write_bytes(b"hello world")
            _, digest1, _ = sync_telegram.file_digest(path, "a.bin", {}, (cache := {}))
            os.utime(path, ns=(1, 1))  # force a different mtime
            path.write_bytes(b"changed contents now")
            _, digest2, rehashed = sync_telegram.file_digest(path, "a.bin", cache, {})
            self.assertTrue(rehashed)
            self.assertNotEqual(digest1, digest2)

    def test_round_trip_through_disk_cache(self):
        import sync_telegram
        with tempfile.TemporaryDirectory() as folder:
            cache_path = Path(folder) / "fp.json"
            sync_telegram.save_fingerprints({"x": {"sig": [1, 2], "sha256": "abc"}}, cache_path)
            self.assertEqual(sync_telegram.load_fingerprints(cache_path),
                             {"x": {"sig": [1, 2], "sha256": "abc"}})


if __name__ == "__main__":
    unittest.main()
