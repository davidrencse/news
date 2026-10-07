"""Restore the newsletter data cache from its encrypted TGFS archive."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from telegram_setup import TREES, TelegramSetupError, require_restored_data


TGFS_PROJECT = Path(os.environ.get("TGFS_PROJECT", Path.home() / "Desktop" / "telegram-file-storage-system"))
DATA_ROOT = Path(os.environ.get("MEDIUM_LIBRARY_DATA", r"E:\storage\newsletter-active"))
REMOTE_ROOT = os.environ.get("TGFS_NEWSLETTER_PATH", "/newsletter-archive-2026-10-02")


def archive_roots(storage) -> list[str]:
    return [f"{REMOTE_ROOT.rstrip('/')}/{tree}" for tree in TREES]


async def ensure_archive_index(storage, secret: str | None) -> int | None:
    """Recover metadata only for a genuinely empty TGFS index; never replace initialized state."""
    root = storage.index.resolve("/")
    if root is None:
        raise TelegramSetupError("TGFS local index has no root node; it was left unchanged.")
    paths = archive_roots(storage)
    nodes = [storage.index.resolve(path) for path in paths]
    if all(node is not None and node.is_dir for node in nodes):
        return None
    if storage.index.children(root.id):
        raise TelegramSetupError(
            f"The TGFS local index is non-empty but does not contain all configured archive trees under "
            f"{REMOTE_ROOT}. It was not overwritten. Check TGFS_NEWSLETTER_PATH or back up and inspect "
            "the TGFS index before using TGFS's index-restore command."
        )
    try:
        return await storage.restore_index(secret)
    except Exception as exc:
        raise TelegramSetupError(
            f"Could not restore the TGFS metadata index ({type(exc).__name__}). Confirm the TGFS "
            "account can access the archive and that an index snapshot exists."
        ) from exc


def tgfs_modules():
    """Load TGFS only after giving a clone/setup-specific error for missing prerequisites."""
    source = TGFS_PROJECT / "src"
    if not (source / "tgfs").is_dir():
        raise TelegramSetupError(
            f"TGFS checkout not found at {TGFS_PROJECT}. Clone/configure the Telegram File Storage "
            "System separately, then set TGFS_PROJECT to that checkout."
        )
    sys.path.insert(0, str(source))
    try:
        from telethon import TelegramClient
        import tgfs.session as session
        from tgfs.config import Config
        from tgfs.system import keyring_get
    except ModuleNotFoundError as exc:
        raise TelegramSetupError(
            f"TGFS dependency {exc.name!r} is unavailable. Install the TGFS checkout's requirements "
            "in its own .venv and run this script with that environment's Python."
        ) from exc

    def make_client(cfg, shared: bool = False):
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
    return session, Config, keyring_get


async def restore() -> None:
    if os.environ.get("MEDIUM_LIBRARY_STORAGE", "telegram") != "local":
        raise TelegramSetupError(
            "Local restore is disabled in Telegram storage mode. The app reads Telegram directly. "
            "For an intentional recovery export only, set MEDIUM_LIBRARY_STORAGE=local."
        )
    session, Config, keyring_get = tgfs_modules()
    cfg = Config.load()
    secret = keyring_get() if cfg.encrypt else None
    if cfg.encrypt and not secret:
        raise TelegramSetupError(
            "TGFS encryption is enabled but its keyring has no saved passphrase. Restore the TGFS "
            "encryption key through TGFS setup before retrying; no key or session is stored here."
        )
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    async with session.open_storage(cfg, lambda _first_time: secret or "", with_cipher=cfg.encrypt) as storage:
        # A new TGFS checkout has an empty local SQLite index. Recover metadata only then;
        # an initialized index missing these paths is preserved and reported instead.
        message_id = await ensure_archive_index(storage, secret)
        if message_id is None:
            print("Using the existing TGFS archive index.", flush=True)
        else:
            print(f"Restored TGFS archive index snapshot (message {message_id}).", flush=True)
        for tree in TREES:
            print(f"Restoring {tree} from Telegram...", flush=True)
            count = await storage.get_tree(f"{REMOTE_ROOT}/{tree}", DATA_ROOT)
            print(f"Restored {count} files from {tree}.", flush=True)
    require_restored_data(DATA_ROOT)
    print(f"Verified required files in {DATA_ROOT}.", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(restore())
    except TelegramSetupError as exc:
        raise SystemExit(f"Telegram restore setup error: {exc}") from exc
