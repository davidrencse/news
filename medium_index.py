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
import relevance

SITEMAP_INDEX = "https://medium.com/sitemap/sitemap.xml"
REQUEST_GAP = 1.5          # seconds between sitemap downloads
INDEX_REFRESH = 6 * 3600   # re-read the list of sitemaps this often
RECHECK_DAYS = 3           # the newest days are re-crawled when Medium updates them
MIN_FREE_GB = 2            # stop growing the index when its drive gets this full
MIN_WORDS = 2              # one-word slugs are mostly short replies ("trust", "thanks")

# Searching: SQLite picks a wide band of candidates, relevance.py re-scores it (see search()).
TITLE_WEIGHT = 10.0        # bm25 column weights: the slug title carries the meaning...
AUTHOR_WEIGHT = 2.0        # ...the author name is a weaker signal unless asked for by name
RERANK_WINDOW = 400        # candidates re-scored beyond the requested page
MAX_CANDIDATES = 1200      # ...and never more than this, so deep paging stays fast
AUTHOR_CAP = 2             # posts per author before the rest move down the page

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
                if e.code not in (429, 503):
                    self._give_up_after_retries(day, url, lastmod)
                self._sleep(300 if e.code in (429, 503) else 60)
                continue
            except Exception as e:
                self.state["error"] = f"{type(e).__name__}: {e}"
                self._give_up_after_retries(day, url, lastmod)
                self._sleep(60)
                continue
            self._sleep(REQUEST_GAP)

    def _give_up_after_retries(self, day, url, lastmod, limit=3):
        """The newest pending day is always retried first, so one day that fails every time would stall
        the whole crawl. After a few tries it is recorded as crawled with no posts and the crawl moves on."""
        fails = self.__dict__.setdefault("_fails", {})  # day -> consecutive failures
        fails[day] = fails.get(day, 0) + 1
        if fails[day] >= limit:
            fails.pop(day)
            with self._db() as c:
                c.execute("INSERT OR REPLACE INTO sitemaps VALUES (?, ?, ?, ?)",
                          (day, lastmod, 0, datetime.now(timezone.utc).isoformat(timespec="seconds")))

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
                try:
                    p = float(prio.group(1)) if prio else 0.2
                except ValueError:  # "." or "1.0.0" matches the pattern but isn't a number
                    p = 0.2
                cur = c.execute("INSERT OR IGNORE INTO posts(url, day, prio) VALUES (?, ?, ?)", (loc, day, p))
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
    def _match(self, expr, query, limit):
        """Candidate rows for one FTS expression, cheapest-first by bm25 with the title weighted up."""
        sql = ["SELECT p.url, p.day, p.prio, bm25(posts_fts, ?, ?) AS bm "
               "FROM posts_fts JOIN posts p ON p.id = posts_fts.rowid WHERE posts_fts MATCH ?"]
        params = [TITLE_WEIGHT, AUTHOR_WEIGHT, expr]
        if query.since:
            sql.append("AND p.day >= ?")
            params.append(query.since)
        if query.before:
            sql.append("AND p.day <= ?")
            params.append(query.before)
        sql.append("ORDER BY bm LIMIT ?")
        params.append(limit)
        with self._db() as c:
            try:
                rows = c.execute(" ".join(sql), params).fetchall()
            except sqlite3.OperationalError:
                return []  # a MATCH expression FTS5 won't parse: treat as no results, not a crash
        return [{"url": u, "title": slug_title(u), "author": author_of(u),
                 "published": day, "prio": prio, "bm": bm} for u, day, prio, bm in rows]

    def search(self, q, limit=20, offset=0, mode=None, taste=None, cap=AUTHOR_CAP):
        """Search the index. Returns {items, mode, more, parsed, scanned}.

        Retrieval and ranking are separate jobs. SQLite finds a wide band of candidates by bm25 -
        which costs about the same for 20 rows as for 500, because the sort dominates - and
        relevance.py then re-scores that band on how much of the query each title really contains,
        whether the words sit together, how fresh the post is and how Medium rates it.

        When a query is too strict to fill a page the net widens in steps, and because reranking
        puts full matches above partial ones, a widened search can only add results below the ones
        it already had. Paging re-ranks the same widened band and slices it, so page 2 continues
        page 1 instead of restarting.

        mode forces one step instead of widening ("any" is how recommendations ask for posts
        matching any of a subtopic's phrases). taste is an optional word -> weight map of what the
        reader is interested in, which nudges genuinely ambiguous queries their way.
        """
        query = relevance.parse_query(q)
        if query.empty:
            return {"items": [], "mode": "all", "more": False, "parsed": "", "scanned": 0}

        # The window grows in whole steps rather than with the offset, so consecutive pages rank
        # the same band of candidates and page 2 carries on where page 1 stopped.
        steps = -(-(offset + limit + 1) // RERANK_WINDOW)
        want = min(steps * RERANK_WINDOW, MAX_CANDIDATES)
        multi = len(query.terms) + len(query.phrases) > 1
        rows, used = [], mode or "all"
        for step in (mode,) if mode else ("all", "prefix", "any"):
            if not mode and step == "prefix" and not query.terms:
                continue
            if not mode and step == "any" and not multi:
                break
            expr = relevance.fts_expression(query, step)
            if not expr:
                break
            found = self._match(expr, query, want)
            if len(found) >= len(rows):
                rows, used = found, step
            if len(rows) > offset + limit:
                break  # enough to fill this page and know there is another

        ranked = relevance.rank(query, rows, taste=taste)
        ranked = relevance.diversify(ranked, group=lambda r: r["author"], cap=cap)
        page = ranked[offset:offset + limit]
        return {"items": page, "mode": used, "more": len(ranked) > offset + limit,
                "parsed": query.describe(), "scanned": len(rows)}
