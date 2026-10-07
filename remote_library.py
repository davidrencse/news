"""In-memory library view with durable Telegram overrides; no local content files."""
import hashlib
import json
import threading

from fastapi import HTTPException

_MISSING = object()


def _canon(article):
    """Order-independent fingerprint of an article's persistable fields (ignores transient 'fetching').
    Identical data always hashes identically, so flush() can skip unchanged rows without ever dropping
    a real edit — the only risk a hash carries is a false 'same', which requires a SHA-1 collision."""
    payload = {k: v for k, v in article.items() if k != 'fetching'}
    return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                   separators=(',', ':')).encode()).digest()


def remote_store_type(base, remote):
    class RemoteStore(base):
        def __init__(self, path):
            self.path = path
            self.lock = threading.RLock()
            self._write_lock = threading.Lock()
            self.version = 0
            self._dirty = False
            self._candidates = {}
            self._removed = set()
            self._overrides = {}
            # Missing or corrupt remote data is fatal. Never replace it with an empty library.
            self.data = json.loads(remote.read('Medium-Library/library.json'))
            if not isinstance(self.data, dict) or not isinstance(self.data.get('articles'), list):
                raise ValueError('Invalid Telegram library')
            try:
                overlay = json.loads(remote.read('Medium-Library/remote-state.json'))
            except FileNotFoundError:
                overlay = {'metadata': {}, 'articles': {}}
            if not isinstance(overlay.get('metadata'), dict) or not isinstance(overlay.get('articles'), dict):
                raise ValueError('Invalid Telegram library overrides')
            self._overrides = overlay['articles']
            self.data.update({k: v for k, v in overlay['metadata'].items() if k != 'articles'})
            original = self.data['articles']
            # Fingerprint the immutable library.json base so flush() can persist only rows that truly
            # changed, instead of re-writing every row ever touched into remote-state.json (which bloats
            # the overlay toward the whole library and re-uploads it to Telegram on every edit).
            self._base_hash = {a['id']: _canon(a) for a in original}
            overrides = self._overrides
            seen = set()
            articles = []
            for a in original:
                aid = a['id']
                value = overrides.get(aid, a)
                if value is not None:
                    articles.append(dict(value))
                seen.add(aid)
            articles[:0] = [dict(a) for aid, a in overrides.items() if aid not in seen and a is not None]
            self.data['articles'] = articles
            for a in articles:
                a.pop('fetching', None)
            self.merge_default_topics()
            self.reindex()
            # No autonomous writer: successful mutating API responses wait for Telegram commit.

        def article(self, aid):
            with self.lock:
                a = super().article(aid)
                if a is not None:
                    self._candidates[aid] = a
                return a

        def add(self, a):
            with self.lock:
                self._candidates[a['id']] = a
                self._removed.discard(a['id'])
                super().add(a)

        def discard(self, a):
            with self.lock:
                # Base discard calls save, so collect the tombstone before it does.
                if self._by_id.get(a['id']) is not a:
                    return False
                self._removed.add(a['id'])
                self._candidates.pop(a['id'], None)
                return super().discard(a)

        def save(self):
            with self.lock:
                self.version += 1
                self._dirty = True

        def flush(self):
            with self._write_lock, self.lock:
                if not self._dirty:
                    self._candidates.clear()
                    return
                overrides = dict(self._overrides)
                for aid, a in self._candidates.items():
                    if self._by_id.get(aid) is not a:
                        continue  # row was replaced/removed since it was touched
                    prev = overrides.get(aid, _MISSING)
                    if prev is _MISSING:
                        baseline = self._base_hash.get(aid)   # never persisted: compare to library.json
                    elif prev is None:
                        baseline = None                       # tombstoned: any live row is a change
                    else:
                        baseline = _canon(prev)               # compare to the persisted override
                    if _canon(a) != baseline:
                        overrides[aid] = {k: v for k, v in a.items() if k != 'fetching'}
                overrides.update({aid: None for aid in self._removed})
                state = {'metadata': {k: v for k, v in self.data.items() if k != 'articles'},
                         'articles': overrides}
                remote.write('Medium-Library/remote-state.json',
                             json.dumps(state, ensure_ascii=False, separators=(',', ':')).encode())
                self._overrides = overrides
                self._candidates.clear()
                self._removed.clear()
                self._dirty = False

        def stop(self):
            self.flush()

    return RemoteStore


class RemotePapers:
    def __init__(self, remote):
        self.remote, self.lock = remote, threading.RLock()
        try:
            records = json.loads(remote.read('Papers/papers.json'))
        except FileNotFoundError:
            records = []
        if not isinstance(records, list):
            raise ValueError('Invalid Telegram paper shelf')
        self.records = {a['id']: a for a in records}

    def all(self):
        with self.lock:
            return sorted((dict(a) for a in self.records.values()),
                          key=lambda a: a.get('saved_at') or a.get('published') or '', reverse=True)

    def persist(self):
        self.remote.write('Papers/papers.json', json.dumps(list(self.records.values()), ensure_ascii=False).encode())

    def upsert(self, record):
        with self.lock:
            old = dict(self.records)
            self.records[record['id']] = record
            try:
                self.persist()
            except Exception:
                self.records = old
                raise
            return dict(record)

    def upsert_many(self, records):
        """Add/replace many records with a single durable write; rolls back on failure."""
        with self.lock:
            old = dict(self.records)
            for record in records:
                self.records[record['id']] = record
            try:
                self.persist()
            except Exception:
                self.records = old
                raise
            return len(records)


class RemoteIndex:
    path = ''

    def start(self):
        pass

    def stop(self):
        pass

    def status(self):
        return dict(posts=0, days_indexed=0, days_setting=0, paused=True, current=None,
                    pending=0, error='Local indexing is disabled in Telegram storage mode.',
                    oldest=None, newest=None, size_mb=0, crawling=None, pending_days=0,
                    location='Telegram (no local content cache)', free_gb=0)

    def search(self, *args, **kwargs):
        return dict(items=[], more=False, parsed='', mode='all')

    def configure(self, *args, **kwargs):
        raise HTTPException(409, 'Local indexing is disabled in Telegram storage mode.')


def remote_curator_type(base):
    class MemoryCache:
        def get(self, url):
            return None

        def items(self):
            return []

        def __len__(self):
            return 0

    class RemoteCurator(base):
        def __init__(self, index, store, add_article, remove_article):
            self.index, self.store = index, store
            self.cache = MemoryCache()
            self.current = self.error = self.next_at = self.backfill_left = None

        def start(self):
            pass

        def stop(self):
            pass

        def status(self):
            return dict(super().status(), enabled=False,
                        error='Automatic curation is disabled in Telegram storage mode.')

        def set_enabled(self, on):
            if on:
                raise HTTPException(409, 'Automatic curation requires a local search index.')

        def set_goal(self, *args):
            raise HTTPException(409, 'Bulk imports are disabled in Telegram storage mode.')

    return RemoteCurator
