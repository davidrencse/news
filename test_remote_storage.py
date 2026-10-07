"""Offline remote-mode regression tests. Fake Telegram; no personal files or network."""
import asyncio
import builtins
import hashlib
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import telegram_storage


class MemoryRemote:
    def __init__(self):
        self.files = {'Medium-Library/library.json': json.dumps({'articles': [], 'topics': []}).encode()}
        self.fail = False

    def read(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write(self, path, data):
        if self.fail:
            raise RuntimeError('Simulated Telegram failure')
        self.files[path] = data

    def exists(self, path):
        return path in self.files

    def exists_many(self, paths):
        return [p in self.files for p in paths]

    def list(self, path):
        return [{'name': p[len(path) + 1:], 'mtime': 0, 'directory': False}
                for p in self.files if p.startswith(path + '/')]

    def close(self):
        pass


class RemoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remote = MemoryRemote()
        # Import under the real default mode, and fail if any content directory is created.
        with patch.dict(os.environ, {'MEDIUM_LIBRARY_STORAGE': 'telegram'}), \
                patch.object(telegram_storage, 'RemoteFiles', return_value=cls.remote), \
                patch('os.makedirs', side_effect=AssertionError('Unexpected disk directory')):
            import app
        cls.app = app
        from fastapi.testclient import TestClient
        cls.client = TestClient(app.app)

    def setUp(self):
        self.remote.fail = False

    def test_round_trip_edits_and_no_disk_writes(self):
        original_open = builtins.open

        def readonly_open(file, mode='r', *args, **kwargs):
            if any(c in mode for c in 'wax+'):
                raise AssertionError('Unexpected local content write')
            return original_open(file, mode, *args, **kwargs)

        with patch('builtins.open', side_effect=readonly_open), \
                patch('os.makedirs', side_effect=AssertionError('Unexpected disk directory')):
            response = self.client.post('/api/articles', json={'url': 'https://medium.com/test/remote-123456789abc',
                                                              'topic': 'custom', 'subtopic': 'inbox'})
            # Use an actual built-in subtopic if inbox is not part of this project's topic tree.
            if response.status_code == 404:
                t = self.app.store.data['topics'][0]
                response = self.client.post('/api/articles', json={'url': 'https://medium.com/test/remote-123456789abc',
                                                                  'topic': t['id'], 'subtopic': t['subtopics'][0]['id']})
            self.assertEqual(response.status_code, 200, response.text)
            aid = response.json()['id']
            self.assertEqual(self.client.post(f'/api/articles/{aid}/read').status_code, 200)
            self.assertEqual(self.client.put(f'/api/articles/{aid}/notes', json={'notes': 'Keep remotely'}).status_code, 200)
            self.assertEqual(self.client.get(f'/api/articles/{aid}/notes').json()['notes'], 'Keep remotely')
            self.assertEqual(len(self.client.get('/api/notebook').json()['entries']), 1)
            t = self.app.store.data['topics'][0]
            self.assertEqual(self.client.patch(f'/api/articles/{aid}', json={'topic': t['id'], 'subtopic': t['subtopics'][0]['id']}).status_code, 200)
            cls = type(self.app.store)
            restored = cls('unused')
            self.assertEqual(restored.article(aid)['notes_count'], 1)
            self.assertIn('read_at', restored.article(aid))
            self.assertEqual(self.client.delete(f'/api/articles/{aid}').status_code, 200)
            self.assertIsNone(cls('unused').article(aid))

    def test_failed_save_is_not_acknowledged_and_retries(self):
        self.remote.fail = True
        response = self.client.post('/api/topics', json={'name': 'Retry remote save', 'parent': 'custom'})
        self.assertEqual(response.status_code, 503)
        self.assertTrue(self.app.store._dirty)
        self.remote.fail = False
        self.app.store.flush()
        restored = type(self.app.store)('unused')
        self.assertIsNotNone(restored.subtopic('custom', 'retry-remote-save'))

    def test_imported_paper_is_persisted_to_telegram_and_durable(self):
        import paper_sources
        record = {'id': 'abc123def4567890', 'doi': '10.1145/3292500.3330701', 'title': 'A probe paper',
                  'authors': ['Ada Lovelace'], 'abstract': 'probe abstract', 'venue': 'Journal of Tests',
                  'year': 2024, 'published': '2024-05', 'url': 'https://doi.org/10.1145/3292500.3330701',
                  'subjects': [], 'references_count': 0, 'cited_by': 0, 'raw': {}}
        with patch.object(paper_sources, 'enrich', return_value=dict(record)), \
                patch('os.makedirs', side_effect=AssertionError('Unexpected disk directory')):
            response = self.client.post('/api/papers/import', json={'doi': '10.1145/3292500.3330701'})
        self.assertEqual(response.status_code, 200, response.text)
        # The paper shelf is a Telegram file like every other durable store, not a local-only cache.
        self.assertIn('Papers/papers.json', self.remote.files)
        stored = json.loads(self.remote.files['Papers/papers.json'])
        self.assertIn('abc123def4567890', [x['id'] for x in stored])
        # A fresh reader of the same archive sees the imported paper: it survives a restart.
        reloaded = type(self.app.paper_store)(self.remote)
        self.assertIn('abc123def4567890', [x['id'] for x in reloaded.all()])
        self.assertIn('abc123def4567890', [x['id'] for x in self.client.get('/api/papers').json()['items']])
        self.assertEqual(self.client.delete('/api/papers/abc123def4567890').status_code, 200)
        self.assertNotIn('abc123def4567890', [x['id'] for x in json.loads(self.remote.files['Papers/papers.json'])])

    def test_search_import_writes_whole_batch_to_telegram_once(self):
        import paper_sources
        batch = [{'id': f'batch{n:012d}', 'doi': f'10.1/{n}', 'title': f'Paper {n}', 'authors': [],
                  'abstract': '', 'venue': 'V', 'year': 2021, 'published': '2021', 'subjects': [],
                  'url': f'https://doi.org/10.1/{n}', 'raw': {}} for n in range(5)]
        with patch.object(paper_sources, 'search', return_value=[dict(x) for x in batch]), \
                patch.object(self.remote, 'write', wraps=self.remote.write) as write:
            response = self.client.post('/api/papers/search_import', json={'query': 'cve', 'rows': 5})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['added'], 5)
        # One durable write for the whole batch, not one per record.
        self.assertEqual(write.call_count, 1)
        stored = {x['id'] for x in json.loads(self.remote.files['Papers/papers.json'])}
        self.assertTrue({x['id'] for x in batch} <= stored)
        reloaded = type(self.app.paper_store)(self.remote)
        self.assertTrue({x['id'] for x in batch} <= {x['id'] for x in reloaded.all()})
        for x in batch:
            self.client.delete(f"/api/papers/{x['id']}")

    def test_failed_paper_save_is_not_acknowledged_and_leaves_shelf_unchanged(self):
        record = {'id': 'fail000000000000', 'doi': '10.0000/unsaved', 'title': 'Unsaved',
                  'authors': [], 'abstract': '', 'venue': '', 'year': 2024, 'published': '2024',
                  'url': 'https://doi.org/10.0000/unsaved', 'subjects': [], 'raw': {}}
        self.remote.fail = True
        # A Telegram write failure must raise, not silently "save" into memory only, and must roll back.
        with self.assertRaises(RuntimeError):
            self.app.paper_store.upsert(dict(record))
        self.assertNotIn('fail000000000000', [x['id'] for x in self.app.paper_store.all()])
        persisted = json.loads(self.remote.files.get('Papers/papers.json', b'[]'))
        self.assertNotIn('fail000000000000', [x['id'] for x in persisted])

    def test_no_local_index_or_browser_content_cache(self):
        self.assertEqual(self.client.post('/api/index', json={'paused': False}).status_code, 409)
        response = self.client.get('/sw.js')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertNotIn('caches.open', response.text)
        self.assertIn('caches.delete', response.text)
        self.assertIsNone(self.app.pipeline)
        self.assertEqual(self.client.get('/api/index').status_code, 200)

    def test_article_files_only_and_traversal_rejected(self):
        self.remote.files['Medium-Library/story/content.html'] = b'<p>Hello</p>'
        response = self.client.get('/files/story/content.html')
        self.assertEqual(response.text, '<p>Hello</p>')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(self.client.get('/files/library.json').status_code, 404)
        for path in ('../notes/a.json', '/etc/passwd', 'x/../a', 'x\\a', 'x//a', 'C:/a'):
            with self.assertRaises(ValueError):
                telegram_storage.safe_relative(path)

    def test_missing_library_fails_closed(self):
        data = self.remote.files.pop('Medium-Library/library.json')
        try:
            with self.assertRaises(FileNotFoundError):
                type(self.app.store)('unused')
        finally:
            self.remote.files['Medium-Library/library.json'] = data

    def test_download_failure_preserves_each_source_cause(self):
        import medium_render
        import pipeline
        import socket
        import ssl
        import urllib.error
        failures = [urllib.error.URLError(ssl.SSLEOFError(8, 'EOF')),
                    urllib.error.URLError(socket.gaierror(11001, 'getaddrinfo failed'))]
        with patch.object(medium_render, 'prepare', return_value={
                'route': 'freedium', 'reason': 'Medium refused the direct download (HTTP 403)'}), \
                patch.object(pipeline, 'FREEDIUM_MIRRORS', ['https://one.test', 'https://two.test']), \
                patch.object(self.app, 'fetch_news_article', side_effect=failures):
            with self.assertRaises(pipeline.PipelineError) as caught:
                self.app.remote_article_content({'url': 'https://medium.com/test/story'})
        message = str(caught.exception)
        for detail in ('HTTP 403', 'one.test', 'TLS', 'two.test', 'DNS'):
            self.assertIn(detail, message)

    def test_source_failure_never_starts_a_telegram_write(self):
        article = {'id': 'auditfailure', 'url': 'https://medium.com/test/failure'}
        job = {'started': 0}
        with patch.object(self.app, 'remote_article_content', side_effect=RuntimeError('Source unavailable')), \
                patch.object(self.remote, 'write') as write:
            asyncio.run(self.app.run_job(job, article))
        self.assertEqual(job['status'], 'error')
        self.assertEqual(job['error'], 'Source unavailable')
        write.assert_not_called()

    def test_download_success_persists_document_images_and_metadata(self):
        t = self.app.store.data['topics'][0]
        response = self.client.post('/api/articles', json={
            'url': 'https://medium.com/test/audit-success-abcdef123456',
            'topic': t['id'], 'subtopic': t['subtopics'][0]['id']})
        self.assertEqual(response.status_code, 200, response.text)
        aid = response.json()['id']
        article = self.app.store.article(aid)
        job = {'started': 0}
        content = '<p>Offline source fixture</p><img src="https://images.test/example.png">'
        try:
            with patch.object(self.app, 'remote_article_content', return_value=(
                    {'title': 'Audit fixture', 'route': 'medium'}, content)), \
                    patch.object(self.app.polite, 'get', return_value=b'\x89PNG-test-image'), \
                    patch('builtins.open', side_effect=AssertionError('Unexpected content disk access')):
                asyncio.run(self.app.run_job(job, article))
            self.assertEqual(job['status'], 'done', job)
            restored = type(self.app.store)('unused').article(aid)
            self.assertTrue(restored['fetched'])
            doc = self.remote.read('Medium-Library/' + restored['doc'])
            self.assertIn(b'images/000.png', doc)
            folder = restored['doc'].rsplit('/', 1)[0]
            self.assertEqual(self.remote.read('Medium-Library/' + folder + '/images/000.png'), b'\x89PNG-test-image')
        finally:
            self.client.delete(f'/api/articles/{aid}')

    def test_remote_existence_check_is_cached_not_re_piped(self):
        # public()/the library endpoints call downloaded() per row; in Telegram mode each uncached
        # check is a serialized TGFS pipe round-trip. Repeated checks within EXISTS_TTL must be cached.
        rel = 'remote-articles/cachetest/0001/content.html'
        self.remote.files['Medium-Library/' + rel] = b'<p>hi</p>'
        self.app._exists_cache.clear()
        calls = []
        real = self.remote.exists
        with patch.object(self.remote, 'exists', side_effect=lambda p: (calls.append(p), real(p))[1]):
            self.assertTrue(self.app.downloaded(rel))
            self.assertTrue(self.app.downloaded(rel))
            self.assertTrue(self.app.downloaded(rel))
        self.assertEqual(len(calls), 1, 'remote exists should hit the pipe once, then serve from cache')

    def test_warm_downloaded_cache_batches_existence_in_one_call(self):
        # A library page should prime existence for all its downloaded rows in one pipe round-trip,
        # not one per row. warm_downloaded_cache() must populate _exists_cache so public() does no I/O.
        arts = []
        for i in range(5):
            rel = f'remote-articles/batch{i}/0001/content.html'
            self.remote.files['Medium-Library/' + rel] = b'<p>x</p>'
            arts.append({'id': f'batch{i}', 'doc': rel, 'pdf': None})
        self.app._exists_cache.clear()
        calls = []
        real = self.remote.exists_many
        single = []
        with patch.object(self.remote, 'exists_many', side_effect=lambda ps: (calls.append(list(ps)), real(ps))[1]), \
                patch.object(self.remote, 'exists', side_effect=lambda p: single.append(p)):
            self.app.warm_downloaded_cache(arts)
            rows = [self.app.public(a) for a in arts]
        self.assertEqual(len(calls), 1, 'should batch all existence checks into one call')
        self.assertEqual(len(calls[0]), 5)
        self.assertEqual(single, [], 'public() must hit the warmed cache, not per-row exists()')
        self.assertTrue(all(r['doc_url'] for r in rows))

    def test_flush_persists_only_changed_rows_not_reads(self):
        # flush() must write only rows that actually changed. A row merely read (added to _candidates)
        # must not be copied into remote-state.json, or the overlay bloats toward the whole library and
        # is re-uploaded to Telegram on every edit. Edits must still persist and round-trip.
        import copy
        import remote_library
        base_cls = type(self.app.store).__mro__[1]  # the Store base under the remote override wrapper
        topics = copy.deepcopy(self.app.store.data['topics'])
        tid, sid = topics[0]['id'], topics[0]['subtopics'][0]['id']
        row = lambda i: {'id': f'row{i}', 'url': f'https://medium.com/x/{i}', 'title': f'T{i}',
                         'topic': tid, 'subtopic': sid, 'locked': True, 'added': '2026-01-01'}
        mem = MemoryRemote()
        mem.files['Medium-Library/library.json'] = json.dumps(
            {'articles': [row(1), row(2), row(3)], 'topics': topics}).encode()
        Store = remote_library.remote_store_type(base_cls, mem)
        store = Store('unused')
        self.assertIsNotNone(store.article('row2'))      # read only
        self.assertIsNotNone(store.article('row3'))      # read only
        edited = store.article('row1'); edited['notes_count'] = 3; store.save()
        store.flush()
        overlay = json.loads(mem.files['Medium-Library/remote-state.json'])['articles']
        self.assertEqual(set(overlay), {'row1'}, 'only the edited row belongs in the overlay')
        reloaded = Store('unused')
        self.assertEqual(reloaded.article('row1')['notes_count'], 3)   # edit survives a restart
        self.assertIsNone(reloaded.article('row2').get('notes_count'))  # untouched rows intact
        # A delete persists as a tombstone and a later unchanged flush adds nothing else.
        store.discard(store.article('row2')); store.flush()
        overlay = json.loads(mem.files['Medium-Library/remote-state.json'])['articles']
        self.assertEqual(overlay.get('row2'), None)
        self.assertIsNone(Store('unused').article('row2'))

    def test_pipe_binary_round_trip(self):
        stream = io.BytesIO()
        telegram_storage.send(stream, {'operation': 'read'}, b'\x00\xff\nhello')
        stream.seek(0)
        header, data = telegram_storage.receive(stream)
        self.assertEqual(header['operation'], 'read')
        self.assertEqual(data, b'\x00\xff\nhello')


class CommitTests(unittest.TestCase):
    def test_failed_index_backup_keeps_old_chunks_and_retry_is_durable(self):
        class Storage:
            chunk_size = 2

            def __init__(self):
                self.node = SimpleNamespace(size=3, sha256='old')
                self.deleted = []
                self.uploads = 0
                self.fail_backup = True
                self.index = SimpleNamespace(mkdir=lambda *a, **k: None, resolve=lambda p: self.node,
                                             put_file=self.put_file)

            def put_file(self, path, size, sha, chunks, mtime):
                self.node = SimpleNamespace(size=size, sha256=sha)
                return self.node, [99]

            async def _upload_chunk(self, idx, data):
                self.uploads += 1
                return SimpleNamespace(message_id=self.uploads)

            async def backup_index(self):
                if self.fail_backup:
                    raise RuntimeError('Index backup failed')

            async def _delete_messages(self, messages):
                self.deleted.extend(messages)

        storage = Storage()
        with self.assertRaises(RuntimeError):
            asyncio.run(telegram_storage.write_remote(storage, '/archive/test', b'new'))
        self.assertEqual(storage.deleted, [])
        storage.fail_backup = False
        asyncio.run(telegram_storage.write_remote(storage, '/archive/test', b'new'))
        self.assertEqual(storage.uploads, 2)
        self.assertEqual(storage.node.sha256, hashlib.sha256(b'new').hexdigest())

    def test_second_instance_lock_fails_fast_instead_of_hanging(self):
        import tempfile
        proj = os.path.join(tempfile.gettempdir(), 'tgfs-lock-test-project')
        first = telegram_storage._acquire_instance_lock(proj)
        try:
            # A duplicate start on the same Telegram session must raise, not deadlock.
            with self.assertRaisesRegex(RuntimeError, 'already using this Telegram session'):
                telegram_storage._acquire_instance_lock(proj)
        finally:
            first.close()
        # Once the first instance releases, a clean restart can take the lock again.
        telegram_storage._acquire_instance_lock(proj).close()

    def test_corrupt_remote_content_is_rejected(self):
        node = SimpleNamespace(id=1, size=3, sha256=hashlib.sha256(b'good').hexdigest(), is_dir=False)
        async def read(_):
            return b'bad'
        storage = SimpleNamespace(index=SimpleNamespace(resolve=lambda p: node, chunks=lambda n: [1]), read_chunk=read)
        with self.assertRaisesRegex(ValueError, 'checksum'):
            asyncio.run(telegram_storage.read_remote(storage, '/archive/test'))

    def test_restore_is_disabled_by_default(self):
        import restore_telegram
        with patch.dict(os.environ, {'MEDIUM_LIBRARY_STORAGE': 'telegram'}):
            with self.assertRaisesRegex(restore_telegram.TelegramSetupError, 'disabled'):
                asyncio.run(restore_telegram.restore())


if __name__ == '__main__':
    unittest.main()
