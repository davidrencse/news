"""Upload changed newsletter data to the TGFS archive after the app stops."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path, PurePosixPath

from telegram_setup import TREES, TelegramSetupError, require_restored_data, tgfs_python


def tgfs_project() -> Path:
    configured = os.environ.get("TGFS_PROJECT")
    if configured:
        return Path(configured)
    return Path.home() / "Desktop" / "telegram-file-storage-system"


TGFS_PROJECT = tgfs_project()
DATA_ROOT = Path(os.environ.get("MEDIUM_LIBRARY_DATA", r"E:\storage\newsletter-active"))
REMOTE_ROOT = os.environ.get("TGFS_NEWSLETTER_PATH", "/newsletter-archive-2026-10-02")
# Snapshot the encrypted index this often during a long sync. Each uploaded chunk is only recoverable
# once it is in a committed snapshot, so an end-of-run-only backup leaves a whole run's uploads orphaned
# if the sync crashes partway. Checkpointing bounds that loss to the last BACKUP_EVERY uploads.
BACKUP_EVERY = int(os.environ.get("TGFS_BACKUP_EVERY", "50"))


def tgfs_modules():
    # Validate local data first; an incomplete cache must not trigger remote setup or IO.
    tgfs_python(TGFS_PROJECT)
    sys.path.insert(0, str(TGFS_PROJECT / "src"))
    try:
        from tgfs.config import Config
        from tgfs.session import open_storage
        from tgfs.system import keyring_get
    except ModuleNotFoundError as exc:
        raise TelegramSetupError(
            f"TGFS dependency {exc.name!r} is unavailable. Install TGFS requirements in its own "
            "virtual environment and run sync with that Python."
        ) from exc
    return Config, open_storage, keyring_get


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# Hashing every file on every sync re-reads the whole library from disk even when nothing changed.
# A small on-disk cache keyed by (size, mtime) lets unchanged files skip the re-hash — the same trick
# rsync uses. Set TGFS_VERIFY_ALL=1 to force a full re-hash. (docs/efficiency-audit.md finding 7)
FINGERPRINT_FILE = DATA_ROOT / ".tgfs-fingerprints.json"


def load_fingerprints(path: Path = FINGERPRINT_FILE) -> dict:
    try:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_fingerprints(fingerprints: dict, path: Path = FINGERPRINT_FILE) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(fingerprints, stream)
        os.replace(tmp, path)
    except OSError:
        pass  # the cache is an optimisation; a backup must not fail because it can't be written


def file_digest(local: Path, key: str, old: dict, new: dict) -> tuple[int, str, bool]:
    """Return (size, sha256, rehashed). Reuse the cached digest when size and mtime are unchanged since
    the last sync; always record the current fingerprint in `new`, so files gone from disk are pruned."""
    stat = local.stat()
    signature = [stat.st_size, stat.st_mtime_ns]
    cached = old.get(key)
    if cached and cached.get("sig") == signature and cached.get("sha256"):
        digest, rehashed = cached["sha256"], False
    else:
        digest, rehashed = sha256(local), True
    new[key] = {"sig": signature, "sha256": digest}
    return stat.st_size, digest, rehashed


async def sync() -> None:
    try:
        require_restored_data(DATA_ROOT)
    except TelegramSetupError as exc:
        raise SystemExit(f"Refusing Telegram sync: {exc}") from exc
    Config, open_storage, keyring_get = tgfs_modules()
    cfg = Config.load()
    secret = keyring_get() if cfg.encrypt else None
    changed = 0
    skipped = 0
    old_fingerprints = {} if os.environ.get("TGFS_VERIFY_ALL") else load_fingerprints()
    new_fingerprints: dict = {}
    async with open_storage(cfg, lambda _first_time: secret or "", with_cipher=cfg.encrypt) as storage:
        pending = 0  # uploads not yet captured in a committed index snapshot
        for tree in TREES:
            local_tree = DATA_ROOT / tree
            for local in sorted(local_tree.rglob("*")):
                if not local.is_file() or local.is_symlink() or local.name.endswith((".tgfs-part", "-shm", "-wal")):
                    continue
                rel = PurePosixPath(tree, *local.relative_to(local_tree).parts)
                remote = f"{REMOTE_ROOT.rstrip('/')}/{rel.as_posix()}"
                size, digest, _ = file_digest(local, rel.as_posix(), old_fingerprints, new_fingerprints)
                node = storage.index.resolve(remote)
                if node is not None and not node.is_dir and node.size == size and node.sha256 == digest:
                    skipped += 1
                    continue
                parent = str(PurePosixPath(remote).parent)
                storage.index.mkdir(parent, parents=True, exist_ok=True)
                await storage.put(local, remote)
                changed += 1
                pending += 1
                print(f"Uploaded: {rel.as_posix()}", flush=True)
                if pending >= BACKUP_EVERY:
                    message_id = await storage.backup_index()
                    pending = 0
                    print(f"Checkpointed Telegram index snapshot (message {message_id}).", flush=True)
        if pending:  # commit the uploads made since the last checkpoint
            message_id = await storage.backup_index()
            print(f"Updated encrypted Telegram index snapshot (message {message_id}).", flush=True)
    save_fingerprints(new_fingerprints)
    print(f"Telegram sync complete: {changed} uploaded, {skipped} unchanged.", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(sync())
    except KeyboardInterrupt:
        raise SystemExit("Telegram sync interrupted; local files are unchanged.")
