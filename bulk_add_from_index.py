"""Quickly add Medium links from the local index to the offline library.

Run with the app stopped: .venv/Scripts/python bulk_add_from_index.py 50000
Topic-matched links are preferred; remaining links go into General Reading. Only links and sitemap
metadata are added. Article bodies are fetched when opened.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit



ROOT = Path(__file__).resolve().parent
LIBRARY = ROOT / "Medium-Library" / "library.json"
INDEX = ROOT / "search-index" / "medium.db"
# These broad labels occur in many unrelated headlines. Specific tags still match.
BROAD = {
    "news", "research", "technology", "business", "science", "engineering",
    "energy", "strategy", "performance", "space", "cloud", "markets",
    "training", "agents", "inference", "theory", "tutorials",
}

# Extra, specific headline terms for subtopics that have few literal tag matches.
# These are used only after exact topic-tag matches have been exhausted.
FALLBACK = {
    ("cybersecurity", "blue-teaming"): ["siem", "security operations", "threat detection"],
    ("cybersecurity", "malware"): ["ransomware", "spyware", "trojan"],
    ("cybersecurity", "web-security"): ["web application security", "sql injection", "cross site scripting"],
    ("cybersecurity", "network-security"): ["zero trust", "network defense"],
    ("cybersecurity", "tutorials"): ["phishing", "pentest", "pentesting"],
    ("artificial-intelligence", "llms"): ["language model", "transformer model", "chatgpt"],
    ("artificial-intelligence", "machine-learning"): ["pytorch", "tensorflow", "scikit learn"],
    ("artificial-intelligence", "deep-learning"): ["neural network", "generative ai", "diffusion model"],
    ("artificial-intelligence", "computer-vision"): ["image recognition", "object detection"],
    ("artificial-intelligence", "rag"): ["vector search", "embedding model"],
    ("computer-science", "algorithms"): ["leetcode", "dynamic programming", "graph algorithm"],
    ("computer-science", "data-structures"): ["binary tree", "hash table", "linked list"],
    ("computer-science", "databases"): ["sql", "mysql", "mongodb", "redis", "sqlite"],
    ("computer-science", "networking"): ["tcp", "dns", "http protocol", "network protocol"],
    ("computer-science", "programming-languages"): ["python", "javascript", "typescript", "java programming"],
    ("software-engineering", "backend"): ["node js", "django", "fastapi", "spring boot", "microservices"],
    ("software-engineering", "frontend"): ["next js", "vue js", "angular js", "web development"],
    ("software-engineering", "devops"): ["terraform", "github actions", "ci cd", "continuous deployment"],
    ("software-engineering", "cloud"): ["serverless", "amazon web services", "azure cloud"],
    ("software-engineering", "system-design"): ["system architecture", "scalability", "distributed architecture"],
    ("engineering", "embedded-systems"): ["arduino", "esp32", "raspberry pi", "firmware"],
    ("engineering", "electronics"): ["circuit board", "pcb design"],
    ("engineering", "aerospace"): ["rocket", "aircraft", "spacex"],
    ("hardware", "fabrication"): ["asml", "wafer", "foundry"],
    ("hardware", "chips"): ["microchip", "integrated circuit"],
    ("industry", "logistics"): ["shipping logistics", "freight", "warehousing"],
    ("industry", "manufacturing"): ["factory automation", "industrial automation"],
    ("geopolitics", "trade"): ["trade war", "global trade", "trade policy"],
    ("geopolitics", "technology-policy"): ["ai regulation", "tech regulation", "antitrust"],
    ("business", "venture-capital"): ["seed funding", "series a", "fundraising"],
    ("business", "markets"): ["stocks", "stock exchange", "wall street"],
    ("science", "physics"): ["quantum mechanics", "particle physics"],
    ("science", "space"): ["nasa", "exoplanet", "black hole"],
    ("science", "materials-science"): ["graphene", "superconductor", "metamaterial"],
}

# Broader single-topic terms. They rank below the literal tags and specific
# aliases above, but still name a recognizable subject in the headline.
EXPANDED = {
    ("artificial-intelligence", "news"): ["ai", "gpt", "chatbot", "generative", "gemini", "copilot"],
    ("artificial-intelligence", "research"): ["arxiv", "transformer", "diffusion", "synthetic data"],
    ("computer-science", "programming-languages"): [
        "coding", "programming", "programmer", "developer", "java", "kotlin", "swift", "php", "ruby"],
    ("computer-science", "databases"): ["data engineering", "data warehouse", "big data", "data pipeline"],
    ("software-engineering", "backend"): ["api", "microservice", "web server", "web framework"],
    ("software-engineering", "frontend"): ["website", "web design", "css", "html", "user interface"],
    ("software-engineering", "developer-tools"): ["software", "github", "open source", "ide", "debugging"],
    ("software-engineering", "system-design"): ["scalability", "distributed", "architecture"],
    ("cybersecurity", "news"): ["cyber", "hacker", "data breach", "security breach"],
    ("cybersecurity", "tutorials"): ["security", "hacking", "password", "authentication"],
    ("business", "startups"): ["entrepreneur", "entrepreneurship", "founder"],
    ("business", "strategy"): ["business", "marketing", "sales", "revenue", "customer", "leadership"],
    ("business", "markets"): ["finance", "trading", "crypto", "bitcoin", "investment"],
    ("business", "economics"): ["economy", "inflation", "economic"],
    ("geopolitics", "geopolitics-news"): ["politics", "election", "government", "war", "diplomacy"],
    ("geopolitics", "united-states"): ["trump", "biden", "congress", "white house"],
    ("science", "research"): ["scientist", "scientific", "biology", "chemistry", "experiment"],
    ("science", "space"): ["cosmos", "universe", "galaxy", "satellite"],
    ("science", "physics"): ["quantum", "particle"],
    ("industry", "energy"): ["renewables", "electricity", "power grid", "oil industry"],
    ("industry", "logistics"): ["freight", "supply chain", "shipping industry"],
    ("hardware", "hardware-news"): ["laptop", "smartphone", "computer hardware", "iphone"],
    ("engineering", "robotics"): ["robot", "robotics", "automation"],
}


def canonical(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or "." not in parts.netloc:
        return None
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def title_from_url(url):
    slug = unquote(urlsplit(url).path.rstrip("/").split("/")[-1])
    slug = re.sub(r"-?[0-9a-f]{8,14}$", "", slug, flags=re.I)
    return re.sub(r"\s+", " ", slug.replace("-", " ")).strip().capitalize()


def topic_rules(topics):
    rules = []
    for topic in topics:
        for sub in topic.get("subtopics", []):
            for tag in sub.get("tags", []):
                phrase = tag.replace("-", " ").lower().strip()
                if len(phrase) < 3 or phrase in BROAD:
                    continue
                words = re.findall(r"[a-z0-9]+", phrase)
                if words:
                    rules.append((" ".join(words), topic["id"], sub["id"]))
    return sorted(set(rules), key=lambda x: (-len(x[0].split()), -len(x[0]), x))


def extra_rules(topics, phrases_by_subtopic):
    valid = {(topic["id"], sub["id"])
             for topic in topics for sub in topic.get("subtopics", [])}
    rules = []
    for pair, phrases in phrases_by_subtopic.items():
        if pair not in valid:
            continue
        for phrase in phrases:
            normalized = " ".join(re.findall(r"[a-z0-9]+", phrase.lower()))
            rules.append((normalized, *pair))
    return sorted(set(rules), key=lambda x: (-len(x[0].split()), -len(x[0]), x))


def rule_lookup(rules):
    """Index phrases by words so each headline needs only a few dictionary lookups."""
    lookup = {}
    for rank, (phrase, topic, subtopic) in enumerate(rules):
        lookup.setdefault(tuple(phrase.split()), (rank, topic, subtopic))
    return lookup, sorted({len(words) for words in lookup}, reverse=True)


def match_title(title, lookup, lengths):
    if len(title.split()) < 4:
        return None
    words = re.findall(r"[a-z0-9]+", title.lower())
    for length in lengths:
        matches = (lookup.get(tuple(words[i:i + length]))
                   for i in range(len(words) - length + 1))
        best = min((match for match in matches if match is not None), default=None)
        if best is not None:
            return best[1], best[2]
    return None


def main(count):
    if count <= 0:
        raise SystemExit("Count must be positive")
    with LIBRARY.open(encoding="utf-8") as f:
        data = json.load(f)
    current = data["articles"]
    existing_urls = {a["url"] for a in current}
    existing_ids = {a["id"] for a in current}
    rules = topic_rules(data["topics"])
    extras = extra_rules(data["topics"], FALLBACK)
    expanded = extra_rules(data["topics"], EXPANDED)
    query = " OR ".join('"' + phrase + '"' for phrase in sorted({r[0] for r in rules + extras + expanded}))
    lookup, lengths = rule_lookup(rules)
    extra_lookup, extra_lengths = rule_lookup(extras)
    expanded_lookup, expanded_lengths = rule_lookup(expanded)
    by_subtopic = {}
    seen = set(existing_urls)
    db = sqlite3.connect(f"file:{INDEX.as_posix()}?mode=ro", uri=True)
    try:
        rows = db.execute("""SELECT posts.url, posts.day, posts.prio
                             FROM posts_fts JOIN posts ON posts.id = posts_fts.rowid
                             WHERE posts_fts MATCH ?""", (query,))
        for raw_url, day, priority in rows:
            url = canonical(raw_url)
            if not url or url in seen:
                continue
            title = title_from_url(url)
            match = match_title(title, lookup, lengths)
            exact = match is not None
            if not exact:
                match = match_title(title, extra_lookup, extra_lengths)
            related = match is not None and not exact
            if match is None:
                match = match_title(title, expanded_lookup, expanded_lengths)
            if not match:
                continue
            aid = hashlib.sha1(url.encode()).hexdigest()[:12]
            if aid in existing_ids:
                continue
            seen.add(url)
            tier = 2 if exact else 1 if related else 0
            by_subtopic.setdefault(match, []).append((tier, priority or 0, day or "", url, title, aid))
        total = sum(map(len, by_subtopic.values()))
        if total < count:
            # The full sitemap index contains many useful posts that don't match the current topic
            # vocabulary. Keep those searchable in one explicit catch-all shelf rather than silently
            # stopping below the requested count or misfiling them under an unrelated topic.
            fallback_key = ("general-reading", "unsorted")
            broad_rows = db.execute("SELECT url, day, prio FROM posts ORDER BY prio DESC, day DESC, id DESC")
            for raw_url, day, priority in broad_rows:
                if total >= count:
                    break
                url = canonical(raw_url)
                if not url or url in seen:
                    continue
                title = title_from_url(url)
                match = match_title(title, lookup, lengths)
                exact = match is not None
                if not exact:
                    match = match_title(title, extra_lookup, extra_lengths)
                related = match is not None and not exact
                if match is None:
                    match = match_title(title, expanded_lookup, expanded_lengths)
                if match is None:
                    match = fallback_key
                aid = hashlib.sha1(url.encode()).hexdigest()[:12]
                if aid in existing_ids:
                    seen.add(url)
                    continue
                seen.add(url)
                tier = 2 if exact else 1 if related else 0
                by_subtopic.setdefault(match, []).append((tier, priority or 0, day or "", url, title, aid))
                total += 1
    finally:
        db.close()

    if total < count:
        raise SystemExit(f"Only {total:,} new indexed links found; requested {count:,}. Library unchanged.")
    for candidates in by_subtopic.values():
        candidates.sort(reverse=True)
    # Give each subtopic a fair first share, then fill the remainder by Medium priority and date.
    share = count // max(len(by_subtopic), 1)
    chosen = []
    remainder = []
    for key, candidates in by_subtopic.items():
        chosen.extend((key, row) for row in candidates[:share])
        remainder.extend((key, row) for row in candidates[share:])
    remainder.sort(key=lambda item: item[1], reverse=True)
    chosen.extend(remainder[:count - len(chosen)])
    assert len(chosen) == count

    if ("general-reading", "unsorted") in by_subtopic and not any(t.get("id") == "general-reading" for t in data["topics"]):
        data["topics"].append({"id": "general-reading", "name": "General Reading",
                               "subtopics": [{"id": "unsorted", "name": "Unsorted articles", "tags": [], "custom": True}]})

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added = [{
        "id": row[5], "url": row[3], "title": row[4], "author": "", "snippet": "",
        "image": None, "published": row[2] or None, "claps": None,
        "topic": key[0], "subtopic": key[1], "added": stamp,
        "pdf": None, "fetched": None, "source": "bulk",
    } for key, row in chosen]
    data["articles"] = added + current
    backup = LIBRARY.with_name(f"library.json.before-bulk-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(LIBRARY, backup)
    scratch = LIBRARY.with_name(f"library.json.{os.getpid()}.tmp")
    try:
        with scratch.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(scratch, LIBRARY)
    finally:
        scratch.unlink(missing_ok=True)
    print(f"Added {count:,} links across {len(Counter(key for key, _ in chosen))} subtopics.")
    tiers = Counter(row[0] for _, row in chosen)
    print(f"Exact topic matches: {tiers[2]:,}; related headline matches: {tiers[1]:,}; broader topic matches: {tiers[0]:,}.")
    print(f"General Reading: {sum(1 for key, _ in chosen if key == ('general-reading', 'unsorted')):,} links.")
    print(f"Library: {len(current):,} -> {len(data['articles']):,} articles. Backup: {backup}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("count", type=int, help="exact number of new links to add")
    main(parser.parse_args().count)
