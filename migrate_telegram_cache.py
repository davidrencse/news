"""Preserve legacy caches in Telegram and emit a deletion receipt; never deletes local data.

Run with TGFS's Python. Receipts contain hashes and Telegram message IDs, not content.
Existing files with identical content can satisfy the receipt without duplicate uploads.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


async def migrate(sources, receipt):
    project = Path(os.environ.get('TGFS_PROJECT', Path.home() / 'Desktop' / 'telegram-file-storage-system'))
    sys.path.insert(0, str(project / 'src'))
    from tgfs.config import Config
    from tgfs.session import open_storage
    from tgfs.system import keyring_get
    cfg = Config.load()
    secret = keyring_get() if cfg.encrypt else None
    if cfg.encrypt and not secret:
        raise RuntimeError('Missing encryption key')
    records = []
    async with open_storage(cfg, lambda _: secret or '', with_cipher=cfg.encrypt, shared=True) as storage:
        known = {}

        def walk(node):
            if node.is_dir:
                for child in storage.index.children(node.id):
                    walk(child)
            else:
                known[(node.size, node.sha256)] = node

        walk(storage.index.require('/'))
        for source in sources:
            source = Path(source).resolve(strict=True)
            if source.is_symlink() or source.parent == source:
                raise ValueError('Refusing a linked source or drive root')
            for local in sorted(source.rglob('*')):
                if local.is_symlink():
                    raise ValueError(f'Refusing linked data: {local}')
                if not local.is_file():
                    continue
                before = local.stat()
                sha = digest(local)
                node = known.get((before.st_size, sha))
                if node is None:
                    remote = '/newsletter-retired-local-copies/' + source.name + '/' + local.relative_to(source).as_posix()
                    storage.index.mkdir(str(Path(remote).parent).replace('\\', '/'), parents=True, exist_ok=True)
                    print(f'Archiving unique file: {source.name}/{local.relative_to(source)}', flush=True)
                    node = await storage.put(local, remote)
                    if node.sha256 != sha or node.size != before.st_size:
                        raise RuntimeError('Source changed during upload')
                    # Each successful upload is recoverable before continuing to the next file.
                    await storage.backup_index()
                    known[(node.size, node.sha256)] = node
                after = local.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise RuntimeError('Local data changed during migration')
                chunks = storage.index.chunks(node.id)
                if sum(c.size for c in chunks) != node.size:
                    raise RuntimeError('Incomplete Telegram chunk index')
                records.append(dict(root=str(source), path=str(local), size=before.st_size,
                                    sha256=sha, remote=storage.index.path_of(node.id),
                                    messages=[c.message_id for c in chunks]))
        mids = sorted({mid for r in records for mid in r['messages']})
        # The hash was computed during upload. Confirm that all indexed blobs still exist in Telegram.
        client = storage.backend.client
        chat = await storage.backend._peer()
        for start in range(0, len(mids), 100):
            batch = mids[start:start + 100]
            messages = await client.get_messages(chat, ids=batch)
            present = {m.id for m in messages if m is not None and getattr(m, 'document', None)}
            if present != set(batch):
                raise RuntimeError('Telegram has missing file chunks; local data must be retained')
        snapshot = await storage.backup_index()
        Path(receipt).write_text(json.dumps(dict(index_snapshot=snapshot, files=records), indent=2), encoding='utf-8')
        print(f'Verified {len(records)} files, {sum(r["size"] for r in records):,} bytes; index snapshot {snapshot}.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', action='append', required=True)
    parser.add_argument('--receipt', required=True)
    args = parser.parse_args()
    asyncio.run(migrate(args.source, args.receipt))
