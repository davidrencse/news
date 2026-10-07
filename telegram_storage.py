"""Telegram content IO over a pipe to TGFS's own Python; never stages content on disk."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import struct
import subprocess
import sys
import threading
import time

from telegram_setup import tgfs_python


def safe_relative(path):
    if not path or path.startswith(('/', '\\')) or '\\' in path:
        raise ValueError('Invalid archive path')
    parts = path.split('/')
    if any(p in ('', '.', '..') or ':' in p for p in parts):
        raise ValueError('Invalid archive path')
    return '/'.join(parts)


def read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        block = stream.read(min(size - len(data), 4 * 1024 * 1024))
        if not block:
            raise EOFError('Telegram storage process closed its pipe')
        data.extend(block)
    return bytes(data)


def send(stream, header, data=b''):
    raw = json.dumps(dict(header, size=len(data))).encode()
    stream.write(struct.pack('!I', len(raw)))
    stream.write(raw)
    if data:
        stream.write(data)
    stream.flush()


def receive(stream):
    size = struct.unpack('!I', read_exact(stream, 4))[0]
    if size > 1024 * 1024:
        raise ValueError('Invalid storage response')
    header = json.loads(read_exact(stream, size))
    return header, read_exact(stream, header['size'])


def _acquire_instance_lock(project):
    """Fail fast instead of deadlocking when a second instance targets the same Telegram session.

    Two app processes sharing one Telegram session hang on each other (TGFS allows a single login),
    and launch_app.py's port check does not cover a bare `python app.py`. We take an OS-level
    exclusive lock keyed to the TGFS project; the OS drops it when the holder exits (even on a crash),
    so there is no stale lock file to clear. Returns the held handle, which the caller keeps open for
    the process lifetime, or raises RuntimeError if another instance already holds it."""
    import tempfile
    key = hashlib.sha1(str(Path(project).resolve()).encode()).hexdigest()[:16]
    path = Path(tempfile.gettempdir()) / f'medium-library-tgfs-{key}.lock'
    handle = open(path, 'a+')
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError(
            'Another Library of Babel instance is already using this Telegram session '
            f'({project}). Stop it fully before starting a new one: two instances share one '
            'Telegram login and will hang on each other.') from exc
    return handle


class RemoteFiles:
    def __init__(self):
        project = Path(os.environ.get('TGFS_PROJECT', Path.home() / 'Desktop' / 'telegram-file-storage-system'))
        # Hold the single-instance lock before spawning the worker, so a duplicate start fails
        # immediately with a clear message rather than silently hanging on the shared session.
        self._instance_lock = _acquire_instance_lock(project)
        self.lock = threading.RLock()
        self.process = subprocess.Popen(
            [str(tgfs_python(project)), '-B', str(Path(__file__).resolve()), '--worker'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

    def request(self, operation, path, data=b''):
        path = safe_relative(path)
        with self.lock:
            send(self.process.stdin, {'operation': operation, 'path': path}, data)
            header, result = receive(self.process.stdout)
            if header.get('missing'):
                raise FileNotFoundError(path)
            if header.get('error'):
                raise RuntimeError(header['error'])
            return result

    def read(self, path):
        return self.request('read', path)

    def write(self, path, data):
        self.request('write', path, data)

    def exists(self, path):
        return json.loads(self.request('exists', path))

    def exists_many(self, paths):
        """One pipe round-trip for many existence checks. The pipe is strictly serial, so a library
        page that would otherwise fire one `exists` per downloaded row collapses to a single call.
        Returns a list of bools aligned with `paths`."""
        paths = [safe_relative(p) for p in paths]
        with self.lock:
            send(self.process.stdin, {'operation': 'exists_many', 'path': 'batch'}, json.dumps(paths).encode())
            header, result = receive(self.process.stdout)
            if header.get('error'):
                raise RuntimeError(header['error'])
            return json.loads(result)

    def list(self, path):
        return json.loads(self.request('list', path))

    def close(self):
        with self.lock:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=10)
            finally:
                # Release the single-instance lock so a clean restart can start right away.
                try:
                    self._instance_lock.close()
                except Exception:
                    pass


async def read_remote(storage, path):
    node = storage.index.resolve(path)
    if node is None or node.is_dir:
        raise FileNotFoundError(path)
    out, digest = io.BytesIO(), hashlib.sha256()
    for chunk in storage.index.chunks(node.id):
        block = await storage.read_chunk(chunk)
        digest.update(block)
        out.write(block)
    if out.tell() != node.size or digest.hexdigest() != node.sha256:
        raise ValueError('Telegram file checksum mismatch')
    return out.getvalue()


async def write_remote(storage, path, data):
    """Publish content and the recoverable remote index before deleting old chunks."""
    storage.index.mkdir(str(PurePosixPath(path).parent), parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    old = storage.index.resolve(path)
    if old and old.sha256 == digest and old.size == len(data):
        # Also retries a previous attempt whose content committed but index backup failed.
        await storage.backup_index()
        return
    chunks, committed = [], False
    try:
        for start in range(0, len(data), storage.chunk_size):
            chunks.append(await storage._upload_chunk(len(chunks), data[start:start + storage.chunk_size]))
        _, orphaned = storage.index.put_file(path, len(data), digest, chunks, mtime=time.time())
        committed = True
        await storage.backup_index()
    except BaseException:
        if not committed:
            await storage._delete_messages([c.message_id for c in chunks])
        raise
    # Failure to collect old chunks must not turn an already durable save into a failed save.
    try:
        await storage._delete_messages(orphaned)
    except Exception:
        pass


async def worker():
    project = Path(os.environ.get('TGFS_PROJECT', Path.home() / 'Desktop' / 'telegram-file-storage-system'))
    sys.path.insert(0, str(project / 'src'))
    from tgfs.config import Config
    from tgfs.session import open_storage
    from tgfs.system import keyring_get
    cfg = Config.load()
    secret = keyring_get() if cfg.encrypt else None
    if cfg.encrypt and not secret:
        raise RuntimeError('TGFS encryption key is unavailable; local fallback is disabled')
    root = os.environ.get('TGFS_NEWSLETTER_PATH', '/newsletter-archive-2026-10-02').rstrip('/')
    async with open_storage(cfg, lambda _: secret or '', with_cipher=cfg.encrypt, shared=True) as storage:
        while True:
            try:
                header, data = await asyncio.to_thread(receive, sys.stdin.buffer)
            except EOFError:
                break
            try:
                path = root + '/' + safe_relative(header['path'])
                operation = header['operation']
                if operation == 'read':
                    result = await read_remote(storage, path)
                elif operation == 'write':
                    await write_remote(storage, path, data)
                    result = b''
                elif operation == 'exists':
                    node = storage.index.resolve(path)
                    result = json.dumps(bool(node and not node.is_dir)).encode()
                elif operation == 'exists_many':
                    out = []
                    for p in json.loads(data):
                        try:
                            node = storage.index.resolve(root + '/' + safe_relative(p))
                            out.append(bool(node and not node.is_dir))
                        except ValueError:
                            out.append(False)
                    result = json.dumps(out).encode()
                elif operation == 'list':
                    node = storage.index.resolve(path)
                    result = json.dumps([{'name': n.name, 'mtime': n.mtime, 'directory': n.is_dir}
                                         for n in storage.index.children(node.id)] if node else []).encode()
                else:
                    raise ValueError('Unknown storage operation')
                send(sys.stdout.buffer, {}, result)
            except FileNotFoundError:
                send(sys.stdout.buffer, {'missing': True})
            except Exception as exc:
                send(sys.stdout.buffer, {'error': f'{type(exc).__name__}: {exc}'})


if __name__ == '__main__':
    asyncio.run(worker())
