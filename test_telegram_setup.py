"""Local-only checks for Telegram setup guards; no TGFS client is loaded or contacted."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import launch_app
import sync_telegram
import restore_telegram
from telegram_setup import TelegramSetupError, missing_data, require_restored_data, tgfs_python


class TelegramSetupTests(unittest.TestCase):
    def test_new_tgfs_index_is_restored_before_use(self):
        class Index:
            def __init__(self):
                self.paths = {}

            def resolve(self, path):
                return SimpleNamespace(id=1, is_dir=True) if path == "/" else self.paths.get(path)

            def children(self, _root_id):
                return list(self.paths.values())

        class Storage:
            def __init__(self):
                self.index = Index()
                self.restored = []

            async def restore_index(self, secret):
                self.restored.append(secret)
                for path in restore_telegram.archive_roots(self):
                    self.index.paths[path] = SimpleNamespace(id=2, is_dir=True)
                return 42

        storage = Storage()
        self.assertEqual(asyncio.run(restore_telegram.ensure_archive_index(storage, "secret")), 42)
        self.assertEqual(storage.restored, ["secret"])

    def test_nonempty_tgfs_index_is_never_replaced(self):
        class Index:
            def resolve(self, path):
                if path == "/":
                    return SimpleNamespace(id=1, is_dir=True)
                return None

            def children(self, _root_id):
                return [SimpleNamespace(id=2, is_dir=True)]

        class Storage:
            index = Index()

            async def restore_index(self, _secret):
                self.fail("must preserve existing index")

        with self.assertRaisesRegex(TelegramSetupError, "not overwritten"):
            asyncio.run(restore_telegram.ensure_archive_index(Storage(), None))

    def test_existing_archive_trees_skip_index_overwrite(self):
        class Index:
            def resolve(self, path):
                if path == "/":
                    return SimpleNamespace(id=1, is_dir=True)
                if path in restore_telegram.archive_roots(None):
                    return SimpleNamespace(id=2, is_dir=True)
                return None

            def children(self, _root_id):
                return [SimpleNamespace(id=2, is_dir=True)]

        class Storage:
            index = Index()

            async def restore_index(self, _secret):
                raise AssertionError("initialized archive index must not be replaced")

        self.assertIsNone(asyncio.run(restore_telegram.ensure_archive_index(Storage(), None)))

    def test_missing_tgfs_checkout_has_setup_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(TelegramSetupError, "Clone/configure the Telegram File Storage"):
                tgfs_python(Path(tmp))

    def test_checkout_requires_its_own_virtual_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "tgfs").mkdir(parents=True)
            with self.assertRaisesRegex(TelegramSetupError, "Create the TGFS checkout's own .venv"):
                tgfs_python(root)

    def test_checkout_resolves_its_windows_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "tgfs").mkdir(parents=True)
            python = root / ".venv" / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            self.assertEqual(tgfs_python(root), python)

    def test_restore_validation_rejects_an_empty_or_partial_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(TelegramSetupError, "app was not started"):
                require_restored_data(root)
            self.assertTrue(missing_data(root))

    def test_sync_refuses_partial_data_before_loading_tgfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_root = sync_telegram.DATA_ROOT
            original_loader = sync_telegram.tgfs_modules
            sync_telegram.DATA_ROOT = Path(tmp)
            sync_telegram.tgfs_modules = lambda: self.fail("TGFS must not be loaded for partial local data")
            try:
                with self.assertRaisesRegex(SystemExit, "Refusing Telegram sync"):
                    asyncio.run(sync_telegram.sync())
            finally:
                sync_telegram.DATA_ROOT = original_root
                sync_telegram.tgfs_modules = original_loader

    def test_launcher_waits_for_the_expected_app_page(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = b"<title>Library of Babel - test</title>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        class LiveProcess:
            def poll(self):
                return None

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertTrue(launch_app.port_is_in_use("127.0.0.1", server.server_port))
            url = f"http://127.0.0.1:{server.server_port}/"
            self.assertTrue(launch_app.wait_until_ready(LiveProcess(), url, timeout=2, interval=0.01))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)
        self.assertEqual(launch_app.local_url("0.0.0.0", 1234), "http://127.0.0.1:1234/")
        self.assertEqual(launch_app.local_url("::1", 1234), "http://[::1]:1234/")

    def test_sync_checkpoints_index_periodically(self):
        # A long sync must snapshot the index during the run, not only at the end, so a crash orphans
        # at most BACKUP_EVERY uploads instead of the whole run. 5 uploads at BACKUP_EVERY=2 => snapshots
        # after #2 and #4, plus a final flush for #5 = 3 (before this it was a single end-of-run backup).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for tree in ("Medium-Library", "search-index", "notes", "Papers"):
                (root / tree).mkdir()
            (root / "Medium-Library" / "library.json").write_bytes(b"{}")
            (root / "search-index" / "medium.db").write_bytes(b"db")
            for i in range(3):
                (root / "Medium-Library" / f"x{i}").write_bytes(f"data{i}".encode())

            class FakeIndex:
                def resolve(self, _path):
                    return None  # nothing exists remotely yet, so every file uploads
                def mkdir(self, _path, parents=False, exist_ok=False):
                    pass

            class FakeStorage:
                def __init__(self):
                    self.index, self.puts, self.backups = FakeIndex(), 0, 0
                async def put(self, _local, _remote):
                    self.puts += 1
                async def backup_index(self):
                    self.backups += 1
                    return self.backups

            class FakeCM:
                def __init__(self, storage):
                    self.storage = storage
                async def __aenter__(self):
                    return self.storage
                async def __aexit__(self, *_a):
                    return False

            storage = FakeStorage()
            cfg = SimpleNamespace(encrypt=False)
            modules = (SimpleNamespace(load=lambda: cfg), lambda *a, **k: FakeCM(storage), lambda: None)
            saved = {k: getattr(sync_telegram, k) for k in
                     ("DATA_ROOT", "FINGERPRINT_FILE", "BACKUP_EVERY", "tgfs_modules")}
            sync_telegram.DATA_ROOT = root
            sync_telegram.FINGERPRINT_FILE = root / ".tgfs-fingerprints.json"
            sync_telegram.BACKUP_EVERY = 2
            sync_telegram.tgfs_modules = lambda: modules
            try:
                asyncio.run(sync_telegram.sync())
            finally:
                for key, value in saved.items():
                    setattr(sync_telegram, key, value)
            self.assertEqual(storage.puts, 5)
            self.assertEqual(storage.backups, 3)

    def test_complete_restore_passes_local_file_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in (
                "Medium-Library/library.json",
                "search-index/medium.db",
            ):
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            (root / "notes").mkdir()
            (root / "Papers").mkdir()
            require_restored_data(root)
            self.assertEqual(missing_data(root), [])


if __name__ == "__main__":
    unittest.main()
