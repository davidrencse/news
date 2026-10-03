"""Restore the newsletter data cache from its encrypted TGFS archive."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


TGFS_PROJECT = Path(os.environ.get("TGFS_PROJECT", Path.home() / "Desktop" / "telegram-file-storage-system"))
sys.path.insert(0, str(TGFS_PROJECT / "src"))

from telethon import TelegramClient  # noqa: E402
import tgfs.session as session  # noqa: E402
from tgfs.config import Config  # noqa: E402
from tgfs.system import keyring_get  # noqa: E402


def make_client(cfg: Config, shared: bool = False):
    return TelegramClient(
        str(cfg.session_path),
        cfg.api_id,
        cfg.api_hash,
        timeout=120,
        request_retries=10,
        connection_retries=10,
        retry_delay=2,
        flood_sleep_threshold=300,
        device_model="tgfs",
    )


session.make_client = make_client
DATA_ROOT = Path(os.environ.get("MEDIUM_LIBRARY_DATA", r"E:\storage\newsletter-active"))
REMOTE_ROOT = os.environ.get("TGFS_NEWSLETTER_PATH", "/newsletter-archive-2026-10-02")
TREES = ("Medium-Library", "notes", "CVEs", "search-index")


async def restore() -> None:
    cfg = Config.load()
    secret = keyring_get() if cfg.encrypt else None
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    async with session.open_storage(cfg, lambda _first_time: secret or "", with_cipher=cfg.encrypt) as storage:
        for tree in TREES:
            print(f"Restoring {tree} from Telegram...", flush=True)
            count = await storage.get_tree(f"{REMOTE_ROOT}/{tree}", DATA_ROOT)
            print(f"Restored {count} files from {tree}.", flush=True)


if __name__ == "__main__":
    asyncio.run(restore())
