"""Bounded startup settings for local processing capacity and background cadence."""
import os


def setting(name, default, minimum, maximum):
    """Reject invalid configuration rather than silently ignoring a tuning mistake."""
    raw = os.environ.get(name, str(default))
    try:
        value = type(default)(raw)
    except (ValueError, TypeError):
        raise ValueError(f"{name} must be a number between {minimum} and {maximum}; got {raw!r}") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}; got {raw!r}")
    return value


ARTICLE_WORKERS = setting("ARTICLE_WORKERS", 4, 1, 16)
IMAGE_WORKERS = setting("IMAGE_WORKERS", 12, 1, 32)
# Admission bound for article downloads: a burst of distinct requests must not create unbounded tasks,
# job records, and retained article references. Only MAX_PARALLEL (= ARTICLE_WORKERS) render at once;
# the rest wait, so this caps the waiting queue, not the render rate.
MAX_ACTIVE_FETCHES = setting("MAX_ACTIVE_FETCHES", 64, ARTICLE_WORKERS, 2000)
CURATOR_PAGES_PER_CYCLE = setting("CURATOR_PAGES_PER_CYCLE", 10, 1, 50)
CURATOR_ADDS_PER_CYCLE = setting("CURATOR_ADDS_PER_CYCLE", 6, 1, 50)
CURATOR_CYCLE_SECONDS = setting("CURATOR_CYCLE_SECONDS", 60.0, 1, 3600)
CURATOR_FAST_CYCLE_SECONDS = setting("CURATOR_FAST_CYCLE_SECONDS", 1.0, 0.1, 60)
INDEX_REQUEST_GAP = setting("INDEX_REQUEST_GAP", 0.25, 0.1, 60)
