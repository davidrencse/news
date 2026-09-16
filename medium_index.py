"""Local search engine for all of Medium.

Medium publishes a sitemap listing every post, one file per day, for search engines
(medium.com/sitemap/sitemap.xml, allowed by robots.txt). A polite background crawler reads
those daily sitemaps, newest first, into a SQLite FTS5 index. Searching Medium then becomes a
local query: no API key, and no scraping of Medium's search or anyone's search engine.

Post titles come from the URL slug, which Medium builds from the title
("from-risk-to-profits-decoding-net-income-margin-c8e3a94ff392").
"""
import os
import re
import shutil
import sqlite3
import threading
import time
import urllib.error
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

import polite

SITEMAP_INDEX = "https://medium.com/sitemap/sitemap.xml"
REQUEST_GAP = 1.5          # seconds between sitemap downloads
INDEX_REFRESH = 6 * 3600   # re-read the list of sitemaps this often
RECHECK_DAYS = 3           # the newest days are re-crawled when Medium updates them
MIN_FREE_GB = 2            # stop growing the index when its drive gets this full
MIN_WORDS = 2              # one-word slugs are mostly short replies ("trust", "thanks")
PRIORITY_WEIGHT = 3.0      # sitemap <priority> (0.1-1.0) nudges ranking toward posts Medium rates higher

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts(id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL, day TEXT, prio REAL);
CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
    title, author, content='', tokenize='porter unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS sitemaps(day TEXT PRIMARY KEY, lastmod TEXT, posts INTEGER, crawled_at TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""


def slug_title(url):
    last = unquote(urlsplit(url).path.rstrip("/").split("/")[-1])
    words = re.sub(r"-?[0-9a-f]{8,12}$", "", last).replace("-", " ").strip()
    return words[:1].upper() + words[1:]


def author_of(url):
    p = urlsplit(url)
    if p.netloc != "medium.com":
        return p.netloc.split(".")[0]
    first = unquote(p.path.strip("/").split("/")[0])
    return "" if first == "p" else first


def _get(url):
    return polite.get_text(url, timeout=60)  # shares Medium's rate limiter with the rest of the app


class MediumIndex:
    def __init__(self, path, default_days=365):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._db() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
        self.days = int(self._setting("days", default_days))
        self.paused = self._setting("paused", "0") == "1"
        self.state = {"current": None, "pending": None, "error": None}
        self._sitemaps = []        # [(day, url, lastmod)] newest first
        self._sitemaps_at = 0.0
        self._wake = threading.Event()
        self._stopping = False

    # ------------------------------------------------------------ storage
    @contextmanager
    def _db(self):
        c = sqlite3.connect(self.path, timeout=30)
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def _setting(self, key, default):
        with self._db() as c:
            row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def _save_setting(self, key, value):
        with self._db() as c:
            c.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, str(value)))

    # ------------------------------------------------------------ crawler
    def start(self):
        threading.Thread(target=self._guarded_run, daemon=True, name="medium-index").start()

    def _guarded_run(self):
        """_run already handles the errors it expects. Anything else restarts the crawler rather than
        leaving the index frozen for the rest of the session."""
        while not self._stopping:
            try:
                self._run()
                return
            except Exception as e:
                self.state["error"] = f"Indexer restarted after {type(e).__name__}: {e}"
                time.sleep(30)

    def stop(self):
        self._stopping = True
        self._wake.set()

    def _sleep(self, seconds):
        self._wake.wait(seconds)
        self._wake.clear()

    def _run(self):
        while not self._stopping:
            if self.paused:
                self.state.update(current=None)
                self._sleep(3600)
                continue
            free_gb = shutil.disk_usage(os.path.dirname(self.path)).free / 1e9
            if free_gb < MIN_FREE_GB:
                self.state.update(current=None, error=f"Indexing paused: only {free_gb:.1f} GB free on this drive")
                self._sleep(1800)
                continue
            try:
                todo = self._pending()
            except Exception as e:
                self.state.update(current=None, error=f"Could not read Medium's sitemap list: {e}")
                self._sleep(300)
                continue
            self.state["pending"] = len(todo)
            if not todo:
                self.state.update(current=None, error=None)
                self._sleep(1800)
                continue
            day, url, lastmod = todo[0]
            self.state["current"] = day
            try:
                self.crawl_day(day, url, lastmod)
                self.state["error"] = None
            except urllib.error.HTTPError as e:
                self.state["error"] = f"Medium answered HTTP {e.code}; waiting before retrying"
                self._sleep(300 if e.code in (429, 503) else 60)
                continue
            except Exception as e:
                self.state["error"] = f"{type(e).__name__}: {e}"
                self._sleep(60)
                continue
            self._sleep(REQUEST_GAP)

    def _pending(self):
        if not self._sitemaps or time.time() - self._sitemaps_at > INDEX_REFRESH:
            xml = _get(SITEMAP_INDEX)
            found = re.findall(r"<loc>(https://medium\.com/sitemap/posts/\d{4}/posts-(\d{4}-\d{2}-\d{2})\.xml)</loc>"
                               r"\s*<lastmod>([^<]*)</lastmod>", xml)
            self._sitemaps = sorted(((d, u, lm) for u, d, lm in found), reverse=True)
            self._sitemaps_at = time.time()
        today = date.today()
        oldest = (today - timedelta(days=self.days)).isoformat()
        recheck = (today - timedelta(days=RECHECK_DAYS)).isoformat()
        with self._db() as c:
            done = dict(c.execute("SELECT day, lastmod FROM sitemaps"))
        return [(d, u, lm) for d, u, lm in self._sitemaps
                if oldest <= d <= today.isoformat()  # Medium lists a few bogus future dates
                and (d not in done or (d >= recheck and done[d] != lm))]

    def crawl_day(self, day, url, lastmod=""):
        xml = _get(url)
        kept = 0
        with self._db() as c:
            for block in re.findall(r"<url>(.*?)</url>", xml, re.S):
                loc = re.search(r"<loc>([^<]+)</loc>", block)
                if not loc:
                    continue
                loc = loc.group(1).strip()
                title = slug_title(loc)
                if len(title.split()) < MIN_WORDS:
                    continue
                prio = re.search(r"<priority>([\d.]+)</priority>", block)
                kept += 1
                cur = c.execute("INSERT OR IGNORE INTO posts(url, day, prio) VALUES (?, ?, ?)",
                                (loc, day, float(prio.group(1)) if prio else 0.2))
                if cur.rowcount:
                    c.execute("INSERT INTO posts_fts(rowid, title, author) VALUES (?, ?, ?)",
                              (cur.lastrowid, title, author_of(loc)))
            c.execute("INSERT OR REPLACE INTO sitemaps VALUES (?, ?, ?, ?)",
                      (day, lastmod, kept, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        return kept

    # ------------------------------------------------------------ settings + status
    def set_days(self, days):
        self.days = max(7, min(int(days), 12000))
        self._save_setting("days", self.days)
        self._sitemaps_at = 0
        self._wake.set()

    def set_paused(self, paused):
        self.paused = bool(paused)
        self._save_setting("paused", "1" if paused else "0")
        self._wake.set()

    def status(self):
        with self._db() as c:
            posts = c.execute("SELECT max(id) FROM posts").fetchone()[0] or 0  # rowids are never skipped
            days_done, oldest, newest = c.execute("SELECT count(*), min(day), max(day) FROM sitemaps").fetchone()
        size = sum(os.path.getsize(p) for p in (self.path, self.path + "-wal") if os.path.exists(p))
        return {"posts": posts, "days_indexed": days_done, "oldest": oldest, "newest": newest,
                "days_setting": self.days, "paused": self.paused, "crawling": self.state["current"],
                "pending_days": self.state["pending"], "error": self.state["error"], "size_mb": round(size / 1e6, 1),
                "location": os.path.dirname(os.path.dirname(self.path)),
                "free_gb": round(shutil.disk_usage(os.path.dirname(self.path)).free / 1e9, 1)}

    # ------------------------------------------------------------ search
    def search(self, q, limit=20, offset=0):
        """Every word must match (stemmed). Falls back to any word when nothing matches all of them."""
        terms = re.findall(r"\w+", q.lower())
        if not terms:
            return [], "all"
        sql = ("SELECT posts.url, posts.day FROM posts_fts JOIN posts ON posts.id = posts_fts.rowid "
               f"WHERE posts_fts MATCH ? ORDER BY posts_fts.rank - posts.prio * {PRIORITY_WEIGHT} LIMIT ? OFFSET ?")
        quoted = [f'"{t}"' for t in terms]
        with self._db() as c:
            rows = c.execute(sql, (" AND ".join(quoted), limit, offset)).fetchall()
            mode = "all"
            if not rows and offset == 0 and len(terms) > 1:
                rows = c.execute(sql, (" OR ".join(quoted), limit, offset)).fetchall()
                mode = "any"
        return [{"url": u, "title": slug_title(u), "author": author_of(u), "published": d} for u, d in rows], mode
