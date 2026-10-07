"""Read-only research-paper metadata from Crossref, looked up by DOI."""
import hashlib
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request


UA = "LibraryOfBabel/1.0 (personal research library)"
CROSSREF = "https://api.crossref.org/works/"
# A DOI is "10." a registrant code, "/" and an opaque suffix. We accept a bare DOI, a doi.org URL,
# or a "doi:" prefix, and pull the 10.xxxx/… out of whichever form the reader pasted.
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.I)


def normalize_doi(raw):
    text = (raw or "").strip()
    match = DOI_RE.search(text)
    if not match:
        raise ValueError("Enter a DOI such as 10.1145/3292500.3330701 or a https://doi.org/… link.")
    # Trailing punctuation often rides along when a DOI is copied from prose; trim the common ones.
    return match.group(0).rstrip(".,;)]}>").lower()


def _request(url, timeout=18, limit=4 * 1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Crossref response was larger than expected")
    return json.loads(raw.decode("utf-8"))


def _strip_jats(text):
    """Crossref abstracts are JATS XML (<jats:p>…</jats:p>); keep the prose, drop the tags."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return _clean(text)


def _clean(text):
    """Decode HTML entities and tidy whitespace. Crossref sometimes double-encodes (&amp;amp;),
    so decode repeatedly until stable (bounded) rather than once."""
    if not text:
        return ""
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return re.sub(r"\s+", " ", text).strip()


def _people(message):
    names = []
    for person in message.get("author") or []:
        if person.get("name"):
            names.append(_clean(person["name"]))
            continue
        full = _clean(" ".join(p for p in (person.get("given"), person.get("family")) if p))
        if full:
            names.append(full)
    return names


def _date(*candidates):
    for part in candidates:
        parts = (part or {}).get("date-parts") or []
        if parts and parts[0]:
            fields = [int(x) for x in parts[0][:3] if isinstance(x, (int, float))]
            if fields:
                return "-".join(f"{n:02d}" if i else str(n) for i, n in enumerate(fields))
    return None


def _record_from_message(message, fallback_doi=None):
    """Map one Crossref work to a shelf record, or None when it has no usable DOI/title."""
    doi = (message.get("DOI") or fallback_doi or "").lower()
    if not doi:
        return None
    titles = message.get("title") or []
    published = _date(message.get("issued"), message.get("published"),
                      message.get("published-print"), message.get("published-online"))
    containers = message.get("container-title") or []
    return {
        "id": hashlib.sha256(doi.encode()).hexdigest()[:16],
        "doi": message.get("DOI") or doi,
        "title": _clean(titles[0] if titles else "") or "Untitled",
        "authors": _people(message),
        "abstract": _strip_jats(message.get("abstract")),
        "venue": _clean(containers[0] if containers else ""),
        "publisher": _clean(message.get("publisher")),
        "type": (message.get("type") or "").replace("-", " "),
        "year": int(published[:4]) if published else None,
        "published": published,
        "url": message.get("URL") or f"https://doi.org/{doi}",
        "volume": message.get("volume"),
        "issue": message.get("issue"),
        "page": message.get("page"),
        "subjects": message.get("subject") or [],
        "references_count": message.get("references-count"),
        "cited_by": message.get("is-referenced-by-count"),
    }


def enrich(doi):
    """Resolve a DOI to a normalized paper record, or raise for a bad DOI / missing work."""
    doi = normalize_doi(doi)
    try:
        payload = _request(CROSSREF + urllib.parse.quote(doi, safe="/"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise LookupError("Crossref has no record for this DOI. Check the identifier and try again.")
        raise RuntimeError(f"Crossref returned HTTP {exc.code}.")
    return _record_from_message(payload.get("message") or {}, fallback_doi=doi)


# Fields pulled for a search so the response stays small even at 100 results per query.
_SEARCH_SELECT = ("DOI,title,author,abstract,container-title,publisher,type,issued,published,"
                  "published-print,published-online,URL,volume,issue,page,subject,"
                  "references-count,is-referenced-by-count")


def search(query, rows=50):
    """Return up to `rows` normalized paper records matching a Crossref relevance query."""
    query = (query or "").strip()
    if not query:
        raise ValueError("Enter a topic to search, e.g. 'CVE software vulnerability'.")
    rows = max(1, min(int(rows), 100))
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(
        {"query": query, "rows": rows, "select": _SEARCH_SELECT})
    try:
        payload = _request(url, timeout=30, limit=16 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Crossref search returned HTTP {exc.code}.")
    items = (payload.get("message") or {}).get("items") or []
    records, seen = [], set()
    for message in items:
        record = _record_from_message(message)
        if record and record["id"] not in seen:
            seen.add(record["id"])
            records.append(record)
    return records
