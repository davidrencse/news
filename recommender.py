"""Personal recommendations built from what you've read, narrowed to labels you pick.

Labels are Medium tags ("llm", "rust") plus the subtopic an article sits in ("hardware/chips").

1. Taste: every library article you engaged with contributes its labels, its title words and its
   author. Highlights and notes count most, opening an article counts, saving one by hand counts a
   little, and an article the curator added on its own counts for nothing - it is the app's opinion,
   not yours. Older reading fades, so the profile follows you when your interests move. Rare labels
   outweigh everywhere-labels like "technology" (IDF).
2. Candidates come from two places. The curator's page cache holds a few thousand posts with full
   metadata (tags, claps, member-only). The search index holds every post Medium published in the
   window you index - far more reach, but only what a URL reveals. Both are searched using your own
   subtopics as the queries, weighted by how much you read each one, so the pool is about you
   rather than about whatever the curator happened to fetch.
3. Score: how close a post sits to your taste (labels for cached posts, title words for everything),
   a nudge for authors you read, how much the post is worth reading at all (claps weighted by age,
   or Medium's own sitemap priority), freshness, and a subtraction for anything resembling what
   you've dismissed.
4. Shape: near-identical headlines and repeat authors move down the page, a few slots go to posts
   next to your interests rather than inside them, and the order rotates hourly so the page isn't
   the same list every time you open it.

Nothing here talks to Medium: recommending costs no requests.
"""
import json
import math
import random
import time
from collections import Counter
from datetime import datetime, timezone

import relevance
from curator import ENGLISH, fits, phrases_for, trend_score

# What engagement with a library article says about your taste.
W_NOTES = 3.0    # highlights or notes: you worked with it
W_READ = 1.5     # opened in the reader
W_FETCH = 1.0    # downloaded, maybe not read yet
W_SAVED = 0.4    # you saved it by hand but haven't opened it
W_AUTO = 0.0     # the curator added it: the app's guess, not evidence

PROFILE_HALF_LIFE = 150.0  # days; how fast old reading fades from the profile
PROFILE_WORDS = 120        # title words kept for nudging search toward your interests
PROFILE_FLOOR = 0.35       # ...but never below this, so early interests still count
GENERIC_DAMP = 0.3         # weight given to labels like "technology"
SEED_WEIGHT = 0.5          # your topic tree, used as taste until you've read enough
COLD_START = 8             # engaged articles below which the topic tree leads

W_LABELS = 1.0    # similarity of a post's tags to your labels
W_WORDS = 0.7     # ...of its title words to the words you read
W_AUTHOR = 0.25   # you have read this author before
W_QUALITY = 0.35  # claps weighted by age, or Medium's own priority when claps are unknown
W_FRESH = 0.30    # recency
W_DISLIKE = 0.8   # similarity to what you've dismissed, subtracted
CACHE_BONUS = 0.05  # a post the curator has actually read, so the card is complete

SEEDS = 8           # subtopics used as index queries per run
SEED_ROWS = 40      # candidates pulled per seed
SEED_DAYS = 120     # how far back index candidates may reach
INDEX_SHARE = 0.3   # share of a page reserved for posts the curator has never opened
MAX_PER_AUTHOR = 2
EXPLORE_EVERY = 7   # one in this many slots goes to something adjacent to your taste
ROTATION = 0.04     # hourly reshuffle, small enough that a strong match stays on top
MEMO_SECONDS = 60   # reuse a ranking this long unless the library or page cache changes


def _norm(label):
    return label.strip().lower().replace(" ", "-")


def _has_notes(notes_path):
    try:
        with open(notes_path, encoding="utf-8") as f:
            n = json.load(f)
        return bool(n.get("highlights") or (n.get("notes") or "").strip())
    except (OSError, ValueError):
        return False


class Taste:
    """What the library says you're interested in: labels, title words, authors, and the opposite."""

    def __init__(self):
        self.labels, self.words, self.authors = Counter(), Counter(), Counter()
        self.dislikes = Counter()
        self.example = {}   # label -> the article that most made it yours, for "because you read"
        self.engaged = 0

    @property
    def cold(self):
        return self.engaged < COLD_START

    def vectors(self, label_idf):
        """IDF-weighted vectors, plus their norms, ready for cosine comparison."""
        labels = {l: w * label_idf.get(l, 1.0) for l, w in self.labels.items()}
        dislikes = {l: w * label_idf.get(l, 1.0) for l, w in self.dislikes.items()}
        return (labels, relevance.norm(labels),
                dict(self.words), relevance.norm(self.words),
                dislikes, relevance.norm(dislikes))


class Recommender:
    def __init__(self, store, cache, notes_path, index=None):
        """store: app Store. cache: curator MetaCache (url -> post meta). notes_path(aid) -> path.
        index: MediumIndex, searched with your own subtopics to reach past the curator's cache."""
        self.store = store
        self.cache = cache
        self.notes_path = notes_path
        self.index = index
        self._memo = {}           # finished rankings, thrown away when the library changes
        self._cache = {}          # work that outlives a library change: taste words, index pool
        self._labels = (None, [])
        self._homes = (None, {})  # (topic-tree signature, url -> subtopic it belongs in)

    # ------------------------------------------------------------ taste
    def labels_of(self, url, topic=None, subtopic=None):
        m = self.cache.get(url) or {}
        labels = {_norm(t) for t in m.get("tags") or []}
        if topic and subtopic:
            labels.add(f"{topic}/{subtopic}")
        return labels

    @staticmethod
    def _engagement(article, has_notes):
        """How much this article counts, and how long ago it happened."""
        if has_notes:
            weight = W_NOTES
        elif article.get("read_at"):
            weight = W_READ
        elif article.get("fetched"):
            weight = W_FETCH
        elif article.get("source") != "auto":
            weight = W_SAVED
        else:
            weight = W_AUTO
        return weight, article.get("read_at") or article.get("added")

    def profile(self):
        """Build a Taste from the library. Cheap enough to run per request; memoised by caller."""
        with self.store.lock:
            arts = list(self.store.data["articles"])
            dismissed = dict(self.store.data.get("dismissed") or {})
            tree = [(t["id"], s) for t in self.store.data["topics"] for s in t["subtopics"]]

        taste = Taste()
        for a in arts:
            weight, when = self._engagement(a, _has_notes(self.notes_path(a["id"])))
            if not weight:
                continue
            taste.engaged += 1
            weight *= PROFILE_FLOOR + (1 - PROFILE_FLOOR) * relevance.freshness(when, half_life=PROFILE_HALF_LIFE)
            for l in self.labels_of(a["url"], a["topic"], a["subtopic"]):
                taste.labels[l] += weight * (GENERIC_DAMP if l in relevance.GENERIC_LABELS else 1.0)
                if weight > taste.example.get(l, (0, None))[0]:
                    taste.example[l] = (weight, a["title"])
            for w, n in relevance.title_vector(a["title"]).items():
                taste.words[w] += weight * n
            if a.get("author"):
                taste.authors[a["author"]] += weight

        for url in dismissed:
            for l in self.labels_of(url):
                taste.dislikes[l] += 1.0
            for w, n in relevance.title_vector((self.cache.get(url) or {}).get("title") or "").items():
                taste.words[w] -= n * W_SAVED  # dismissing is mild evidence against those words too

        if taste.cold:  # until there's reading to go on, the topic tree is the stated interest
            for tid, sub in tree:
                for tag in sub.get("tags") or []:
                    taste.labels[_norm(tag)] += SEED_WEIGHT
                taste.labels[f"{tid}/{sub['id']}"] += SEED_WEIGHT
        taste.words = Counter({w: v for w, v in taste.words.items() if v > 0})
        return taste

    def word_profile(self):
        """The title-word half of the profile, for nudging ambiguous searches.

        Every search asks for this, so it is kept until the library changes - and only the newest
        one is kept, because the curator bumps the version every couple of minutes.
        """
        version, words = self._cache.get("words", (None, {}))
        if version != self.store.version:
            words = dict(self.profile().words.most_common(PROFILE_WORDS))
            self._cache["words"] = (self.store.version, words)
        return words

    # ------------------------------------------------------------ candidates
    def _seeds(self, taste):
        """Your subtopics as index queries, strongest interest first."""
        with self.store.lock:
            subs = [(t["id"], s) for t in self.store.data["topics"] for s in t["subtopics"]
                    if t["id"] != "custom" or s.get("tags")]
        ranked = sorted(subs, key=lambda ts: -taste.labels.get(f"{ts[0]}/{ts[1]['id']}", 0.0))
        out = []
        for tid, sub in ranked[:SEEDS]:
            phrases = phrases_for(tid, sub)[:4]
            q = " ".join('"%s"' % p.replace('"', "") for p in phrases)
            out.append((q + f" since:{SEED_DAYS}d", tid, sub))
        return out

    def _from_index(self, taste):
        """Recent posts matching each of your subtopics, whether or not the curator has seen them.

        Held between runs because it depends only on the library and the index, not on the label or
        access filters the reader is flipping through.
        """
        seeds = self._seeds(taste)
        # Keyed by the seeds themselves, not by the library version: the curator saves the library
        # every couple of minutes, and rebuilding this each time would cost seconds of SQLite work
        # for the same answer. It is kept out of the ranking memo for that reason.
        key = (tuple(q for q, _, _ in seeds), int(time.time() // 3600))
        cached_key, found = self._cache.get("index-pool", (None, {}))
        if cached_key == key:
            return found
        found = {}
        for q, tid, sub in seeds:
            try:
                rows = self.index.search(q, SEED_ROWS, mode="any", cap=MAX_PER_AUTHOR)["items"]
            except Exception:
                continue  # a seed that won't parse shouldn't cost the whole page
            for r in rows:
                # The index knows no language, so the slug has to look like English, the way the
                # curator decides the same thing.
                if r["url"] in found or not ENGLISH.search(r["title"].lower()):
                    continue
                found[r["url"]] = (
                    {"title": r["title"], "author": r["author"], "published": r["published"],
                     "prio": r["prio"], "snippet": "", "image": None, "claps": None, "locked": None},
                    {f"{tid}/{sub['id']}"}, (tid, sub), False)
        self._cache["index-pool"] = (key, found)
        return found

    def _pool(self, taste, include_locked):
        """Candidate posts from the curator's cache and from the index, already de-duplicated."""
        with self.store.lock:
            have = {a["url"] for a in self.store.data["articles"]}
            have |= set(self.store.data.get("dismissed") or {})
            subs = [(t["id"], s, phrases_for(t["id"], s)) for t in self.store.data["topics"]
                    for s in t["subtopics"] if t["id"] != "custom" or s.get("tags")]

        # Which subtopic a post belongs in depends on the topic tree, not on the reader's filters,
        # so it is worked out once per post and kept until the tree itself changes.
        sig = tuple((tid, s["id"], tuple(s.get("tags") or [])) for tid, s, _ in subs)
        if self._homes[0] != sig:
            self._homes = (sig, {})
        homes = self._homes[1]

        pool = {}
        for url, m in self.cache.items():
            if not m or url in have or not m.get("title") or m.get("lang") not in ("en", None) or m.get("response"):
                continue
            if include_locked is not None and m.get("locked") is not include_locked:
                continue
            if url not in homes:
                homes[url] = next(((tid, s) for tid, s, ph in subs if fits(m, tid, s, ph)), None)
            home = homes[url]
            pool[url] = (m, self.labels_of(url, *(home and (home[0], home[1]["id"]) or (None, None))), home, True)

        # Index rows say nothing about member-only status, so they only take part when the reader
        # hasn't asked for one kind: putting them on a Free page would be guessing.
        if self.index and include_locked is None:
            for url, cand in self._from_index(taste).items():
                if url not in have and url not in pool:
                    pool[url] = cand
        return pool

    # ------------------------------------------------------------ ranking
    def recommend(self, want=(), limit=50, include_locked=None):
        """Posts ranked for you. want: labels a post must carry at least one of (empty = anything).
        include_locked: None = both, True = member-only only, False = free only."""
        want = {_norm(w) for w in want if w.strip()}
        key = (frozenset(want), limit, include_locked, self.store.version, len(self.cache),
               int(time.time() // 3600))
        hit = self._memo.get(key)
        if hit and time.time() - hit[0] < MEMO_SECONDS:
            return hit[1]
        res = self._rank(want, limit, include_locked)
        # Rankings for an older library (or an earlier hour) can never be served again.
        self._memo = {k: v for k, v in self._memo.items() if k[3:] == key[3:]}
        self._memo[key] = (time.time(), res)
        return res

    def _rank(self, want, limit, include_locked):
        taste = self.profile()
        pool = self._pool(taste, include_locked)
        pool = {u: v for u, v in pool.items() if not want or (v[1] & want)}

        label_idf = relevance.idf([labels for _, labels, _, _ in pool.values()])
        tlabels, tlnorm, twords, twnorm, dislikes, dnorm = taste.vectors(label_idf)
        amax = max(taste.authors.values(), default=1.0) or 1.0
        trends = [trend_score(m.get("claps"), m.get("published")) for m, _, _, cached in pool.values() if cached]
        tmax = math.log1p(max(trends, default=1.0) or 1.0) or 1.0
        rotate = random.Random(int(time.time() // 3600))

        scored = []
        for url, (m, labels, home, cached) in pool.items():
            lvec = {l: label_idf.get(l, 1.0) for l in labels}
            label_sim = relevance.cosine(tlabels, lvec, norm_a=tlnorm)
            word_sim = relevance.cosine(twords, relevance.title_vector(m.get("title") or ""), norm_a=twnorm)
            author = taste.authors.get(m.get("author"), 0.0) / amax
            if cached:
                quality = math.log1p(trend_score(m.get("claps"), m.get("published"))) / tmax
            else:
                quality = 0.6 * relevance.clamp(((m.get("prio") or 0.2) - 0.1) / 0.9)  # weaker evidence
            dislike = relevance.cosine(dislikes, lvec, norm_a=dnorm)
            score = (W_LABELS * label_sim
                     + W_WORDS * word_sim
                     + W_AUTHOR * author
                     + W_QUALITY * min(1.0, quality)
                     + W_FRESH * relevance.freshness(m.get("published"))
                     + (CACHE_BONUS if cached else 0.0)
                     - W_DISLIKE * dislike
                     + ROTATION * rotate.random())
            shared = sorted((labels & tlabels.keys()) - relevance.GENERIC_LABELS,
                            key=lambda l: -tlabels[l])
            scored.append({
                "url": url, "score": score, "meta": m, "labels": labels, "home": home, "cached": cached,
                "shared": shared, "tags_shared": [l for l in shared if "/" not in l],
                "author_hit": author > 0, "affinity": max(label_sim, word_sim),
            })
        scored.sort(key=lambda it: -it["score"])

        picks = self._shape(scored, limit)
        return {
            "engaged": taste.engaged,
            "cold": taste.cold,
            "sources": {"cache": sum(1 for p in picks if p["cached"]), "index": sum(1 for p in picks if not p["cached"])},
            "top_labels": [l for l, _ in taste.labels.most_common(40)
                           if "/" not in l and l not in relevance.GENERIC_LABELS][:12],
            "items": [self._item(p, taste) for p in picks],
        }

    def _shape(self, scored, limit):
        """Turn a ranked list into a page worth reading: varied, and not only the obvious picks.

        Three things fight for the page. Posts that match your taste earn most of it. A share is
        reserved for posts the curator has never opened, because otherwise the richer cached cards
        always win and recommendations never reach past what the app already knows. And every so
        often a slot goes to a post sitting in one of your subtopics under labels you have never
        read, which is the only way a profile built from your own history can widen.
        """
        # "Next to your taste": in a subtopic you read, but under tags you never have. Only posts
        # the curator has opened can qualify - a post known solely from its URL has no tags at all,
        # which would make every one of them look like uncharted territory.
        edge = [it for it in scored
                if it["cached"] and it["home"] and not it["tags_shared"]][:limit // EXPLORE_EVERY]
        spent = {it["url"] for it in edge}
        rest = [it for it in scored if it["affinity"] > 0 and it["url"] not in spent]
        ordered = relevance.diversify(
            rest, group=lambda it: it["meta"].get("author"), cap=MAX_PER_AUTHOR,
            title=lambda it: it["meta"].get("title") or "")
        known = [it for it in ordered if it["cached"]]
        # Index candidates arrive grouped by the subtopic that found them, so without a second
        # pass the reserved slots all go to whichever subtopic matched best.
        fresh = relevance.diversify([it for it in ordered if not it["cached"]],
                                    group=lambda it: it["home"] and it["home"][1]["id"], cap=2,
                                    title=lambda it: it["meta"].get("title") or "")

        reserved = min(len(fresh), int(limit * INDEX_SHARE))
        out = known[:limit - reserved]
        for i, it in enumerate(fresh[:reserved]):  # spread through the page, not bunched at the end
            out.insert(min(len(out), (i + 1) * max(1, limit // (reserved + 1))), it)
        for pos in range(EXPLORE_EVERY - 1, limit, EXPLORE_EVERY):
            if not edge:
                break
            pick = edge.pop(0)
            pick["explore"] = True
            out.insert(min(pos, len(out)), pick)
        return out[:limit]

    @staticmethod
    def _why_words(title, taste, limit=3):
        """The words in this title you read most, for a post that shares no labels with you.

        Posts the curator hasn't opened have no tags at all, and a tagged post can still match on
        its wording alone - either way the card would otherwise sit there with nothing to say.
        """
        weighted = {}
        for w in relevance.words(title):
            weight = taste.words.get(relevance.stem(w), 0.0)
            if weight > 0 and w not in relevance.STOPWORDS and len(w) > 2:
                weighted[w] = weight
        return sorted(weighted, key=lambda w: -weighted[w])[:limit]

    def _item(self, pick, taste):
        """One card: the post, why it is here, and where saving it would put it."""
        m, home = pick["meta"], pick["home"]
        because = pick["tags_shared"][:3]
        example = next((taste.example[l] for l in pick["shared"] if l in taste.example), None)
        return dict(m, url=pick["url"], score=round(pick["score"], 4),
                    labels=sorted(l for l in pick["labels"] if "/" not in l),
                    because=because, like=example and example[1],
                    because_words=[] if because else self._why_words(m.get("title") or "", taste),
                    same_author=pick["author_hit"], explore=pick.get("explore", False),
                    known=pick["cached"], topic=home and home[0], subtopic=home and home[1]["id"])

    # ------------------------------------------------------------ feedback
    def dismiss(self, url, undo=False):
        """Remember that a post was waved away, so its labels stop pulling the ranking."""
        with self.store.lock:
            seen = self.store.data.setdefault("dismissed", {})
            if undo:
                seen.pop(url, None)
            else:
                seen[url] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.store.save()
        self._memo = {}  # rankings only: the index pool and taste words survive a dismissal
        return not undo

    def all_labels(self, limit=200):
        """The most common labels among candidate posts, for the label picker."""
        if self._labels[0] != len(self.cache):
            c = Counter(_norm(t) for _, m in self.cache.items() if m for t in m.get("tags") or [])
            self._labels = (len(self.cache), [l for l, _ in c.most_common(limit)])
        return self._labels[1]
