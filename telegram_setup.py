"""Shared checks for the optional Telegram File Storage System integration.

These checks only inspect local paths. They never connect to Telegram.
"""

from __future__ import annotations

from pathlib import Path


TREES = ("Medium-Library", "notes", "CVEs", "search-index")
REQUIRED_FILES = (
    Path("Medium-Library/library.json"),
    Path("CVEs/cvelistV5.sqlite3"),
    Path("search-index/medium.db"),
)


class TelegramSetupError(RuntimeError):
    """The local TGFS checkout or restored data is not ready to use."""


def tgfs_python(project: Path) -> Path:
    """Return the TGFS Windows interpreter after validating its checkout layout."""
    project = Path(project).expanduser()
    if not (project / "src" / "tgfs").is_dir():
        raise TelegramSetupError(
            f"TGFS checkout not found at {project}. Clone/configure the Telegram File Storage "
            "System separately, then set TGFS_PROJECT to that checkout. It must contain src\\tgfs."
        )
    interpreter = project / ".venv" / "Scripts" / "python.exe"
    if not interpreter.is_file():
        raise TelegramSetupError(
            f"TGFS Python environment not found at {interpreter}. Create the TGFS checkout's "
            "own .venv and install its requirements before using Telegram restore/sync."
        )
    return interpreter


def missing_data(data_root: Path) -> list[Path]:
    """List required restored files and data trees that are absent locally."""
    root = Path(data_root).expanduser()
    missing = [rel for rel in REQUIRED_FILES if not (root / rel).is_file()]
    missing.extend(Path(tree) for tree in TREES if not (root / tree).is_dir())
    return missing


def require_restored_data(data_root: Path) -> None:
    missing = missing_data(data_root)
    if missing:
        paths = ", ".join(str(Path(data_root) / item) for item in missing)
        raise TelegramSetupError(
            "Telegram restore did not produce the complete newsletter data set. "
            f"Missing: {paths}. Check TGFS_NEWSLETTER_PATH and its archive contents; "
            "the app was not started to avoid opening an empty or partial library."
        )
