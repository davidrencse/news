"""On-demand news briefing: 60 topical feeds -> 10 candidates -> 5 reading picks."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import copy
import math
import re
import threading
from urllib.parse import urlencode, urlsplit
import uuid
import xml.etree.ElementTree as ET

import polite


TOPIC_GROUPS = {
    "AI": ("artificial intelligence", "AI models", "AI regulation", "AI chips", "AI research", "robotics"),
    "Software": ("open source software", "software engineering", "cloud computing", "programming languages", "developer tools", "data centers"),
    "Security": ("cybersecurity", "ransomware", "data breach", "software vulnerability", "digital privacy", "critical infrastructure security"),
    "Devices": ("semiconductors", "smartphones", "personal computers", "consumer technology", "wearable technology", "quantum computing"),
    "Business": ("technology earnings", "technology startups", "antitrust technology", "global economy", "interest rates", "global trade"),
    "Science": ("scientific discovery", "physics research", "biotechnology", "medical research", "materials science", "ocean research"),
    "Energy": ("renewable energy", "battery technology", "electric vehicles", "climate science", "nuclear energy", "power grid"),
    "World": ("world news", "international diplomacy", "elections", "geopolitics", "humanitarian aid", "global supply chains"),
    "Space": ("space exploration", "NASA", "satellite technology", "astronomy", "rocket launch", "lunar mission"),
    "Society": ("public health", "education technology", "internet policy", "future of work", "transportation technology", "digital media"),
}
TOPICS = tuple((group, query) for group, queries in TOPIC_GROUPS.items() for query in queries)
STOP_WORDS = {"the", "and", "for", "with", "from", "that", "this", "into", "over", "after", "news", "new", "how", "what", "its", "are", "has"}


def words(text):
    return set(re.findall(r"[a-z0-9]{2,}", text.lower())) - STOP_WORDS


def feed_url(query, start, end):
    # Request calendar bounds, then enforce the exact click-time window on every item.
    search = f'{query} after:{start.date()} before:{(end + timedelta(days=1)).date()}'
    return "https://news.google.com/rss/search?" + urlencode({
        "q": search, "hl": "en-US", "gl": "US", "ceid": "US:en"})


def fetch_topic(group, query, start, end):
    raw = polite.get(feed_url(query, start, end), timeout=15)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("News feed exceeded 4 MiB")
    return parse_feed(raw, group, query, start, end)


def parse_feed(raw, group, query, start, end):
    root = ET.fromstring(raw)
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("The news source did not return an RSS feed")
    items = []
    for position, node in enumerate(root.findall("./channel/item")[:40]):
        title = (node.findtext("title") or "").strip()
        url = (node.findtext("link") or "").strip()
        source = (node.findtext("source") or "Unknown publisher").strip()
        try:
            published = parsedate_to_datetime(node.findtext("pubDate") or "")
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            published = published.astimezone(timezone.utc)
            parsed = urlsplit(url)
            if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
                continue
        except (ValueError, TypeError, OverflowError):
            continue
        if not title or not start <= published <= end:
            continue
        if title.endswith(" - " + source):
            title = title[:-(len(source) + 3)].strip()
        if not title:
            continue
        age_hours = (end - published).total_seconds() / 3600
        match = len(words(title) & words(query)) / max(1, len(words(query)))
        score = 4 * math.exp(-age_hours / 36) + 2 * match + 1 / (1 + position / 5)
        items.append({"title": title[:500], "url": url, "source": source[:200],
                      "published": published.isoformat(), "category": group,
                      "topics": [query], "score": score})
    return items


def select_briefing(candidates):
    """Deterministic, explainable ranking with headline deduplication and diversity."""
    merged, titles = {}, {}
    for candidate in candidates:
        item = dict(candidate, topics=list(candidate["topics"]))
        title_key = " ".join(sorted(words(item["title"])))
        key = item["url"] if item["url"] in merged else titles.get(title_key, item["url"])
        if key in merged:
            existing = merged[key]
            topics = sorted(set(existing["topics"] + item["topics"]))
            if item["score"] > existing["score"]:
                merged[key] = item
            merged[key]["topics"] = topics
        else:
            merged[key] = item
        titles[title_key] = key
    pool = sorted(merged.values(), key=lambda x: (-x["score"], x["url"]))

    def pick(rows, limit):
        remaining, chosen = list(rows), []
        while remaining and len(chosen) < limit:
            ranked = []
            for item in remaining:
                tokens = words(item["title"])
                duplicate = any(len(tokens & words(other["title"])) / max(1, len(tokens | words(other["title"]))) >= 0.65 for other in chosen)
                if duplicate:
                    continue
                category_count = sum(x["category"] == item["category"] for x in chosen)
                source_count = sum(x["source"].casefold() == item["source"].casefold() for x in chosen)
                ranked.append((item["score"] - 1.8 * category_count - 1.4 * source_count, item))
            if not ranked:
                break
            best = max(ranked, key=lambda x: x[0])[1]
            remaining.remove(best)
            chosen.append(best)
        return chosen

    shortlist = pick(pool, 10)
    picks = pick(shortlist, 5)
    for item in shortlist:
        item["score"] = round(item["score"], 3)
        item["reason"] = f"Recent coverage of {item['topics'][0]}; selected for freshness, relevance, and a mix of topics and publishers."
    return {"candidate_count": len(pool), "shortlist": shortlist, "articles": picks}


class BriefingService:
    """One bounded, shared scan at a time; no crawling until the user asks."""
    def __init__(self, fetcher=fetch_topic):
        self.fetcher = fetcher
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.job = None

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.job) if self.job else {"state": "idle"}

    def start(self, days=3, now=None):
        if days not in (1, 3, 7):
            raise ValueError("Choose 1, 3, or 7 days")
        with self.lock:
            if self.job and self.job["state"] == "running":
                return self.snapshot()  # repeat clicks join the existing scan
            if self.stopped.is_set():
                raise RuntimeError("Briefing service is shutting down")
            end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            start = end - timedelta(days=days)
            self.job = {"id": uuid.uuid4().hex, "state": "running", "days": days,
                        "started_at": end.isoformat(), "window_start": start.isoformat(),
                        "window_end": end.isoformat(), "topics_total": len(TOPICS),
                        "topics_scanned": 0, "topics_succeeded": 0, "topics_failed": 0,
                        "topics": [q for _, q in TOPICS], "errors": [], "shortlist": [],
                        "articles": [], "candidate_count": 0, "message": "Scanning recent news"}
            threading.Thread(target=self._run, args=(start, end), daemon=True, name="morning-briefing").start()
            return self.snapshot()

    def stop(self):
        self.stopped.set()

    def _run(self, start, end):
        candidates = []
        try:
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="briefing-feed") as pool:
                def fetch(group, query):
                    if self.stopped.is_set():
                        return []
                    return self.fetcher(group, query, start, end)
                pending = {pool.submit(fetch, group, query): query for group, query in TOPICS}
                for future in as_completed(pending):
                    if self.stopped.is_set():
                        for other in pending:
                            other.cancel()
                        break
                    try:
                        candidates.extend(future.result())
                        with self.lock:
                            self.job["topics_succeeded"] += 1
                    except Exception as exc:
                        with self.lock:
                            self.job["topics_failed"] += 1
                            self.job["errors"].append({"topic": pending[future], "error": str(exc)[:180]})
                    with self.lock:
                        self.job["topics_scanned"] += 1
            result = select_briefing(candidates)
            with self.lock:
                self.job.update(result)
                self.job["state"] = "cancelled" if self.stopped.is_set() else "complete" if result["articles"] else "error"
                self.job["finished_at"] = datetime.now(timezone.utc).isoformat()
                if self.stopped.is_set():
                    self.job["message"] = "Scan stopped when the server shut down. Generate a new briefing after restarting."
                elif not result["articles"]:
                    self.job["message"] = "No dated articles found in this window. Try a wider window or retry when the news source is available."
                elif len(result["articles"]) < 5:
                    self.job["message"] = "Fewer than five distinct recent stories were available. Showing only verified feed results."
                else:
                    self.job["message"] = "Your five reading picks are ready."
        except Exception as exc:
            with self.lock:
                self.job.update(state="error", message=f"Briefing failed: {str(exc)[:200]}")
