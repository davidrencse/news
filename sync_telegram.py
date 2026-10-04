"""Upload changed newsletter data to the TGFS archive after the app stops."""

from __future__ import annotations

import asyncio
import hashlib
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
    async with open_storage(cfg, lambda _first_time: secret or "", with_cipher=cfg.encrypt) as storage:
        for tree in TREES:
            local_tree = DATA_ROOT / tree
            for local in sorted(local_tree.rglob("*")):
                if not local.is_file() or local.is_symlink() or local.name.endswith((".tgfs-part", "-shm", "-wal")):
                    continue
                rel = PurePosixPath(tree, *local.relative_to(local_tree).parts)
                remote = f"{REMOTE_ROOT.rstrip('/')}/{rel.as_posix()}"
                size = local.stat().st_size
                digest = sha256(local)
                node = storage.index.resolve(remote)
                if node is not None and not node.is_dir and node.size == size and node.sha256 == digest:
                    skipped += 1
                    continue
                parent = str(PurePosixPath(remote).parent)
                storage.index.mkdir(parent, parents=True, exist_ok=True)
                await storage.put(local, remote)
                changed += 1
                print(f"Uploaded: {rel.as_posix()}", flush=True)
        if changed:
            message_id = await storage.backup_index()
            print(f"Updated encrypted Telegram index snapshot (message {message_id}).", flush=True)
    print(f"Telegram sync complete: {changed} uploaded, {skipped} unchanged.", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(sync())
    except KeyboardInterrupt:
        raise SystemExit("Telegram sync interrupted; local files are unchanged.")
