"""Relevance scoring shared by Medium-wide search and personal recommendations.

Search and recommendations ask the same question from different directions: given a bag of words
standing for an interest (a typed query, or everything you have read), how well does a post match
it, and which posts should appear together so a page isn't ten versions of one story.

This module holds that shared judgement and nothing else: no database, no network, no app state.
`medium_index.py` feeds it rows from SQLite, `recommender.py` feeds it post metadata, and both get
the same notion of "close". Being I/O-free also means it can be exercised directly.

Post titles in the index come from the URL slug, so scoring here works on short, punctuation-free
text. Features are each normalised to 0..1 and combined with the weights below, which is what makes
the weights readable: doubling W_PHRASE really does make adjacency twice as important.
"""
import math
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone

# ---------------------------------------------------------------- words

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "does", "for", "from",
    "how", "i", "in", "into", "is", "it", "its", "my", "of", "on", "or", "our", "s", "so", "t",
    "that", "the", "their", "them", "then", "there", "these", "this", "to", "up", "use", "using",
    "vs", "was", "we", "what", "when", "which", "who", "why", "will", "with", "you", "your",
}

# Labels so broad that carrying them says almost nothing about a post.
GENERIC_LABELS = {
    "technology", "tech", "programming", "software-development", "software-engineering", "coding",
    "business", "science", "artificial-intelligence", "ai", "writing", "life", "self-improvement",
    "productivity", "startup", "data-science", "machine-learning", "news", "education", "learning",
}

_WORD = re.compile(r"[^\W_]+", re.UNICODE)  # letters and digits in any script, never "_"
_SUFFIXES = (
    "ational", "izations", "ization", "iveness", "fulness", "ousness", "ations", "ation",
    "ements", "ement", "ments", "ment", "ences", "ence", "ances", "ance", "ingly", "edly",
    "ities", "ity", "ives", "ive", "izes", "ized", "izer", "ize", "ises", "ised", "ise",
    "ings", "ing", "ies", "ied", "ers", "est", "ful", "ness", "ous", "ally", "ly",
    "ions", "ion", "als", "er", "ed", "es", "s",
)
MIN_STEM = 3
_DOUBLE_KEEP = ("ll", "ss", "zz", "ff")


def words(text):
    """Lowercase word tokens; the only tokenizer used anywhere in this module.

    Accents are stripped to match how the index is tokenized (remove_diacritics), so searching
    for "cafe" and for "café" reach the same posts, and any script tokenizes, not just Latin.
    """
    folded = unicodedata.normalize("NFKD", (text or "").lower())
    if not folded.isascii():
        folded = "".join(c for c in folded if not unicodedata.combining(c))
    return _WORD.findall(folded)


def stem(word):
    """A light suffix stripper, close enough to SQLite's porter tokenizer for ranking.

    Retrieval is done by SQLite, so this only decides how much credit a match earns. What matters
    is that both sides of a comparison go through the same function: "transformers" and
    "transformer" must land together, while "transformer" still beats "transformation" because the
    exact-match feature can tell them apart.
    """
    if len(word) <= MIN_STEM or word.isdigit():
        return word
    for suf in _SUFFIXES:
        if not word.endswith(suf) or len(word) - len(suf) < MIN_STEM:
            continue
        if suf == "s" and word.endswith(("ss", "us", "is")):
            break  # "class", "status", "analysis" are not plurals
        base = word[: -len(suf)]
        if suf in ("ies", "ied"):
            base += "y"  # companies -> company, studied -> study
        elif suf in ("ing", "ings", "ed") and len(base) > MIN_STEM \
                and base[-1] == base[-2] and not base.endswith(_DOUBLE_KEEP):
            base = base[:-1]  # running -> run, stopped -> stop
        return base
    return word


def stems(text):
    return [stem(w) for w in words(text)]


def match_level(term, token):
    """2 = the same word, 1 = the same word under stemming, 0 = unrelated."""
    if term == token:
        return 2
    if stem(term) == stem(token):
        return 1
    short, long = sorted((term, token), key=len)
    if short and long.startswith(short):
        if not short.isascii():
            return 1  # Japanese and Chinese aren't spaced, so a whole phrase is one token
        if len(short) >= 5 and len(long) - len(short) <= 2:
            return 1  # kubernete/kubernetes, where two stemmers can disagree
    return 0


# ---------------------------------------------------------------- queries

_TOKEN = re.compile(r'-?(?:[a-z]+:)?"[^"]*"|\S+', re.I)
_REL_DATE = re.compile(r"^(\d+)\s*([dwmy])$", re.I)
_DATE_FIELDS = {"since": "since", "after": "since", "from": "since",
                "before": "before", "until": "before"}
_AUTHOR_FIELDS = {"author", "by", "writer"}


def _as_day(value, today=None):
    """"2026-08-01", "2026-08", "30d", "6m" -> an ISO day, or None if it isn't a date."""
    today = today or date.today()
    value = value.strip().strip('"')
    rel = _REL_DATE.match(value)
    if rel:
        n, unit = int(rel.group(1)), rel.group(2).lower()
        return (today - timedelta(days=n * {"d": 1, "w": 7, "m": 30, "y": 365}[unit])).isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    if re.fullmatch(r"\d{4}-\d{2}", value):
        return value + "-01"
    if re.fullmatch(r"\d{4}", value):
        return value + "-01-01"
    return None


class Query:
    """A parsed search query.

    terms/phrases are what a post must (or should) contain. `soft` holds the query's own stopwords:
    dropped from retrieval so "how to build a rag pipeline" isn't dominated by "how to a", but kept
    for scoring so a title that does say them still edges ahead.
    """

    def __init__(self, raw):
        self.raw = raw
        self.terms, self.soft, self.phrases, self.exclude = [], [], [], []
        self.author = None
        self.since = self.before = None

    @property
    def content(self):
        """Every word a post is expected to carry, phrases flattened."""
        return self.terms + [w for p in self.phrases for w in p]

    @property
    def empty(self):
        return not (self.terms or self.phrases or self.author)

    @property
    def filtered(self):
        return bool(self.author or self.since or self.before or self.exclude)

    def describe(self):
        """A plain-English echo of the filters, so the UI can show what it understood."""
        bits = []
        if self.phrases:
            bits.append("exactly " + " and ".join('"%s"' % " ".join(p) for p in self.phrases))
        if self.author:
            bits.append("by %s" % self.author)
        if self.since:
            bits.append("since %s" % self.since)
        if self.before:
            bits.append("before %s" % self.before)
        if self.exclude:
            bits.append("without " + ", ".join(self.exclude))
        return "; ".join(bits)


def parse_query(raw, today=None):
    """Turn what someone typed into a Query.

    Understood: "quoted phrases", -excluded, author:name (also by: and writer:), since:/after:/from:
    and before:/until: with an ISO date or a relative span like 30d or 6m. Anything else is a word.
    """
    q = Query((raw or "").strip())
    for tok in _TOKEN.findall(q.raw):
        neg = tok.startswith("-")
        tok = tok[1:] if neg else tok
        field, _, value = tok.partition(":")
        field = field.lower()
        if value and field in _AUTHOR_FIELDS:
            q.author = value.strip('"').strip() or None
            continue
        if value and field in _DATE_FIELDS:
            day = _as_day(value, today)
            if day:
                setattr(q, _DATE_FIELDS[field], day)
                continue
        if tok.startswith('"') and tok.endswith('"') and len(tok) > 2:
            inner = words(tok[1:-1])
            if neg:
                q.exclude += inner
            elif len(inner) > 1:
                q.phrases.append(inner)
            else:
                q.terms += inner
            continue
        for w in words(tok):
            if neg:
                q.exclude.append(w)
            elif w in STOPWORDS:
                q.soft.append(w)
            else:
                q.terms.append(w)
    if not q.terms and not q.phrases and q.soft:
        q.terms, q.soft = q.soft, []  # a query of only small words still has to search for them
    seen = set()
    q.terms = [t for t in q.terms if not (t in seen or seen.add(t))]
    return q


def fts_expression(query, mode="all"):
    """The FTS5 MATCH expression for a Query.

    all    - every term and phrase must appear (what someone usually means)
    prefix - the same, with the last word left open, so "kubernete" still finds "kubernetes"
    any    - at least one term; the ranker then sorts by how many of them actually matched
    """
    parts = ['"%s"' % " ".join(p) for p in query.phrases]
    terms = ['"%s"' % t for t in query.terms]
    if mode == "prefix" and terms:
        terms[-1] += "*"
    parts += terms
    if not parts:
        expr = ""
    elif mode == "any" and len(parts) > 1:
        expr = "(%s)" % " OR ".join(parts)
    else:
        expr = " AND ".join(parts)
    if query.author:
        author = 'author:"%s"' % " ".join(words(query.author))
        expr = "%s AND %s" % (author, expr) if expr else author
    if query.exclude and expr:
        expr += " NOT (%s)" % " OR ".join('"%s"' % w for w in query.exclude)
    return expr


# ---------------------------------------------------------------- scoring

W_LEX = 1.00      # SQLite's bm25, normalised against the best candidate for this query
W_COVER = 2.20    # share of the query's words the post actually contains - the dominant feature
W_EXACT = 0.50    # ...matched as typed rather than only after stemming
W_PHRASE = 0.90   # the words sit together, in order
W_HEAD = 0.25     # the match starts at the front of the title
W_SOFT = 0.10     # the query's own small words ("how to") show up too
W_AUTHOR = 0.60   # the query names this author
W_FRESH = 0.45    # recency
W_PRIO = 0.30     # Medium's own sitemap <priority>
W_BREVITY = 0.15  # concise titles over keyword-stuffed ones
W_TASTE = 0.35    # overlap with what this reader reads - a tie-break, never a filter

TASTE_SHOWN = 0.35       # similarity worth telling the reader about

FRESH_HALF_LIFE = 240.0  # days; mild, so an evergreen match still beats a fresh near-miss
IDEAL_TITLE = 10         # title words past this start to look stuffed
TITLE_SPREAD = 16.0
STEM_CREDIT = 0.85       # a stem-only match is worth this much of an exact one


def clamp(x):
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def freshness(published, today=None, half_life=FRESH_HALF_LIFE):
    """1.0 for something published today, halving every `half_life` days."""
    if not published:
        return 0.0
    try:
        day = datetime.fromisoformat(published)
    except (TypeError, ValueError):
        return 0.0
    if day.tzinfo is None:
        day = day.replace(tzinfo=timezone.utc)
    now = today or datetime.now(timezone.utc)
    return 0.5 ** (max(0.0, (now - day).total_seconds() / 86400.0) / half_life)


def _positions(terms, tokens):
    """Where each query term first appears, and how exactly it matched."""
    hits = {}
    for i, tok in enumerate(tokens):
        for t in terms:
            lvl = match_level(t, tok)
            if lvl and (t not in hits or lvl > hits[t][1]):
                hits[t] = (i, lvl)
    return hits


def _phrase_score(hits, query):
    """1.0 when the query's words sit next to each other in order, fading as they scatter."""
    if len(hits) < 2:
        return 1.0 if query.phrases else 0.0
    order = [hits[t][0] for t in query.content if t in hits]
    span = max(order) - min(order) + 1
    ascending = all(b > a for a, b in zip(order, order[1:]))
    return (len(order) / span) * (1.0 if ascending else 0.8)


def title_vector(title):
    """A title as a sparse word vector, for comparing against a reader's interests."""
    vec = {}
    for w in words(title):
        if w not in STOPWORDS:
            vec[stem(w)] = vec.get(stem(w), 0.0) + 1.0
    return vec


def score_post(query, title, author, published, prio, lex, today=None, taste=None, taste_norm=None):
    """Score one candidate. Returns (score, detail); detail explains the outcome to the UI."""
    tokens = words(title)
    content = query.content
    hits = _positions(content, tokens)
    ahits = _positions(content, words(author)) if author else {}

    if content:
        levels = {t: max(hits.get(t, (0, 0))[1], ahits.get(t, (0, 0))[1]) for t in content}
        cover = sum(1.0 if l == 2 else STEM_CREDIT for l in levels.values() if l) / len(content)
        exact = sum(1 for l in levels.values() if l == 2) / len(content)
    else:
        cover = exact = 1.0

    soft = (sum(1 for w in query.soft if any(match_level(w, tok) for tok in tokens)) / len(query.soft)
            if query.soft else 0.0)
    head = 1.0 - clamp(min((p for p, _ in hits.values()), default=8) / 8.0) if hits else 0.0
    author_hit = 0.0
    if query.author:
        author_hit = 1.0
    elif ahits and content and len(ahits) == len(set(content)):
        author_hit = 0.7  # the whole query is someone's name

    fresh = freshness(published, today)
    taste_sim = cosine(taste, title_vector(title), norm_a=taste_norm) if taste else 0.0
    score = (W_TASTE * taste_sim
             + W_LEX * lex
             + W_COVER * cover
             + W_EXACT * exact * cover
             + W_PHRASE * _phrase_score(hits, query) * cover
             + W_HEAD * head
             + W_SOFT * soft
             + W_AUTHOR * author_hit
             + W_FRESH * fresh
             + W_PRIO * clamp((prio - 0.1) / 0.9)
             + W_BREVITY * (1.0 - clamp((len(tokens) - IDEAL_TITLE) / TITLE_SPREAD)))
    detail = {"cover": round(cover, 3), "fresh": round(fresh, 3), "taste": round(taste_sim, 3),
              "missing": [t for t in dict.fromkeys(content) if not hits.get(t) and not ahits.get(t)]}
    return score, detail


def rank(query, rows, today=None, taste=None):
    """Score every candidate row and sort. Rows carry title, author, published, prio and bm."""
    best = min((r.get("bm") or 0.0) for r in rows) if rows else 0.0
    taste_norm = norm(taste) if taste else None
    out = []
    for r in rows:
        lex = (r.get("bm") or 0.0) / best if best else 0.0
        s, detail = score_post(query, r.get("title", ""), r.get("author", ""),
                               r.get("published"), r.get("prio") or 0.2, lex, today, taste, taste_norm)
        out.append(dict(r, score=round(s, 4), missing=detail["missing"], cover=detail["cover"],
                        yours=detail["taste"] >= TASTE_SHOWN))
    # Sorted stably in two passes so equal scores fall back to the newer post, and identical
    # scores and dates still land in the same order every time (paging depends on that).
    out.sort(key=lambda r: (r.get("published") or "", r.get("url", "")), reverse=True)
    out.sort(key=lambda r: -r["score"])
    return out


# ---------------------------------------------------------------- shaping a list

def title_key(title):
    """A signature that collapses restatements of the same headline."""
    ws = [stem(w) for w in words(title) if w not in STOPWORDS]
    return " ".join(sorted(set(ws))) or (title or "").strip().lower()


def diversify(items, group=None, cap=2, key=title_key, title=lambda it: it.get("title", "")):
    """Keep a list varied without throwing anything away.

    Restatements of the same headline, and a third, fourth, fifth post from the same author, move
    to the back of the list rather than out of it, so a deliberate search for one writer still
    shows everything they wrote.
    """
    kept, spill, seen, per_group = [], [], set(), {}
    for it in items:
        sig = key(title(it))
        owner = group(it) if group else None
        if sig in seen or (owner and per_group.get(owner, 0) >= cap):
            spill.append(it)
            continue
        seen.add(sig)
        if owner:
            per_group[owner] = per_group.get(owner, 0) + 1
        kept.append(it)
    return kept + spill


def idf(label_sets, total=None):
    """Inverse document frequency over label sets, so everywhere-labels count for less."""
    label_sets = list(label_sets)
    df = {}
    for labels in label_sets:
        for l in set(labels):
            df[l] = df.get(l, 0) + 1
    n = max(1, len(label_sets) if total is None else total)
    return {l: math.log((n + 1) / (c + 1)) + 1.0 for l, c in df.items()}


def norm(vec):
    return math.sqrt(sum(v * v for v in vec.values()))


def cosine(a, b, norm_a=None):
    """Cosine similarity of two sparse weight maps."""
    if not a or not b:
        return 0.0
    na, nb = norm_a or norm(a), norm(b)
    if not na or not nb:
        return 0.0
    small, large = (a, b) if len(a) < len(b) else (b, a)
    return sum(v * large[k] for k, v in small.items() if k in large) / (na * nb)
