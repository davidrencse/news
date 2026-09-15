"""Turn a Medium post into a clean article: straight from Medium when the story is free, and tell the
pipeline to use Freedium when it's paywalled or Medium refuses.

A Medium post page embeds the story as structured paragraphs (Apollo state -> bodyModel). For a free
story that is the complete text, so the app renders it into its own clean HTML. A member-only story
(isLocked) only embeds a preview, and Medium sometimes refuses the request outright; `prepare()`
routes those to Freedium.
"""
import html
import json
import re
import urllib.error
from datetime import datetime, timezone

import polite

IMAGE_URL = "https://miro.medium.com/v2/resize:fit:1400/{}"
THUMB_URL = "https://miro.medium.com/v2/resize:fill:320:214/{}"


# ------------------------------------------------------------ parsing

def deref(state, value):
    if isinstance(value, dict) and "__ref" in value:
        return state.get(value["__ref"], {})
    return value if value is not None else {}


def parse_post(page, url):
    """(state, post) from a post page, or (None, None) if the embedded data isn't there."""
    i = page.find("window.__APOLLO_STATE__")
    if i < 0:
        return None, None
    try:
        state, _ = json.JSONDecoder().raw_decode(page[page.find("{", i):])
    except ValueError:
        return None, None
    pid = url.rstrip("/").rsplit("/", 1)[-1].rsplit("-", 1)[-1]
    post = state.get(f"Post:{pid}")
    if not post:
        posts = [v for k, v in state.items() if k.startswith("Post:") and "clapCount" in v]
        post = posts[0] if len(posts) == 1 else None
    return state, post


def meta_of(state, post):
    creator = deref(state, post.get("creator"))
    tags = []
    for t in post.get("tags") or []:
        ref = t.get("__ref", "") if isinstance(t, dict) else ""
        tags.append(state.get(ref, {}).get("id") or ref.split(":", 1)[-1])
    image = (post.get("previewImage") or {}).get("id")
    ts = post.get("firstPublishedAt")
    subtitle = ((post.get("extendedPreviewContent") or {}).get("subtitle") or "").strip()
    return {
        "title": (post.get("title") or "").strip(),
        "subtitle": subtitle,
        "snippet": subtitle,
        "author": creator.get("name") or "",
        "published": datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat(timespec="seconds") if ts else None,
        "claps": int(post.get("clapCount") or 0),
        "reading_time": round(post.get("readingTime") or 0),
        "lang": post.get("detectedLanguage"),
        "locked": bool(post.get("isLocked")),
        "response": bool(post.get("inResponseToPostResult")),
        "tags": [t for t in tags if t],
        "image": THUMB_URL.format(image) if image else None,
    }


def body_of(state, post):
    key = next((k for k in post if k.startswith("content(")), None)
    model = (post.get(key) or {}).get("bodyModel") or {}
    paragraphs = [deref(state, p) for p in model.get("paragraphs") or []]
    sections = {s.get("startIndex") for s in model.get("sections") or [] if isinstance(s, dict)}
    return paragraphs, sections


# ------------------------------------------------------------ routing

def prepare(url):
    """{"route": "medium", "meta", "body"} for a free story, else {"route": "freedium", "reason"}.
    Raises LookupError when the story doesn't exist."""
    medium = polite.status().get("medium.com", {})
    if medium.get("waiting_s", 0) > 10:
        return {"route": "freedium", "reason": "Medium has asked the app to slow down"}
    try:
        page = polite.get_text(url, priority=True)  # someone is waiting for this article
    except urllib.error.HTTPError as e:
        if e.code == 402:
            return {"route": "freedium", "reason": "paywalled (HTTP 402)"}
        if e.code in (404, 410):
            raise LookupError(f"Medium says this story doesn't exist (HTTP {e.code}).") from e
        return {"route": "freedium", "reason": f"Medium refused the direct download (HTTP {e.code})"}
    except (urllib.error.URLError, TimeoutError) as e:
        return {"route": "freedium", "reason": f"Medium was unreachable ({e})"}

    state, post = parse_post(page, url)
    if not post:
        return {"route": "freedium", "reason": "couldn't read the story from Medium's page"}
    if post.get("isLocked"):
        return {"route": "freedium", "reason": "member-only story"}
    paragraphs, sections = body_of(state, post)
    if not paragraphs:
        return {"route": "freedium", "reason": "Medium's page had no story text"}
    meta = meta_of(state, post)
    return {"route": "medium", "meta": meta, "body": render_body(state, paragraphs, sections, meta)}


# ------------------------------------------------------------ rendering

WRAP = {"STRONG": (1, "<strong>", "</strong>"), "EM": (2, "<em>", "</em>"), "CODE": (3, "<code>", "</code>")}


def inline(state, text, markups):
    """Text with Medium's markups (offsets are UTF-16 code units, like JavaScript strings)."""
    units = (text or "").encode("utf-16-le")
    n = len(units) // 2
    spans = []
    for m in markups or []:
        m = deref(state, m)
        start, end = max(0, int(m.get("start") or 0)), min(n, int(m.get("end") or 0))
        if end <= start:
            continue
        if m.get("type") == "A":
            href = m.get("href") or (f"https://medium.com/u/{m['userId']}" if m.get("userId") else "")
            if re.match(r"^https?://", href):
                spans.append((start, end, 0, f'<a href="{html.escape(href)}">', "</a>"))
        elif m.get("type") in WRAP:
            spans.append((start, end, *WRAP[m["type"]]))
    cuts = sorted({0, n, *(x for s in spans for x in s[:2])})
    out = []
    for a, b in zip(cuts, cuts[1:]):
        piece = html.escape(units[2 * a:2 * b].decode("utf-16-le", "replace"))
        active = sorted((s for s in spans if s[0] <= a and s[1] >= b), key=lambda s: s[2])
        out.append("".join(s[3] for s in active) + piece + "".join(s[4] for s in reversed(active)))
    return "".join(out).replace("\n", "<br>")


def _same(a, b):
    squash = lambda s: re.sub(r"\W+", "", (s or "").lower())
    return bool(squash(a)) and squash(a) == squash(b)


def _plain(fragment):
    return html.unescape(re.sub(r"<[^>]+>", "", fragment or "")).strip()


def render_body(state, paragraphs, sections, meta):
    out, open_list = [], None
    for i, p in enumerate(paragraphs):
        kind, text = p.get("type"), p.get("text") or ""
        # Medium repeats the title and subtitle as the first paragraphs; the header already shows them
        if i < 3 and kind in ("H2", "H3", "H4") and (_same(text, meta["title"]) or _same(text, meta["subtitle"])):
            continue
        if open_list and kind not in ("OLI", "ULI"):
            out.append(f"</{open_list}>")
            open_list = None
        if i in sections and i > 0 and out:
            out.append('<hr class="section">')
        body = inline(state, text, p.get("markups"))
        if kind in ("H2", "H3"):
            out.append(f"<h2>{body}</h2>")
        elif kind == "H4":
            out.append(f"<h3>{body}</h3>")
        elif kind == "IMG":
            image = deref(state, p.get("metadata"))
            if image.get("id"):
                caption = f"<figcaption>{body}</figcaption>" if text else ""
                out.append(f'<figure><img src="{html.escape(IMAGE_URL.format(image["id"]))}" '
                           f'alt="{html.escape(image.get("alt") or "")}">{caption}</figure>')
        elif kind == "PRE":
            lang = (deref(state, p.get("codeBlockMetadata")) or {}).get("lang") or ""
            attr = f' data-lang="{html.escape(lang)}"' if lang else ""
            out.append(f"<pre{attr}><code>{html.escape(text)}</code></pre>")
        elif kind == "BQ":
            out.append(f"<blockquote>{body}</blockquote>")
        elif kind == "PQ":
            out.append(f'<blockquote class="pull">{body}</blockquote>')
        elif kind in ("OLI", "ULI"):
            tag = "ol" if kind == "OLI" else "ul"
            if open_list != tag:
                if open_list:
                    out.append(f"</{open_list}>")
                out.append(f"<{tag}>")
                open_list = tag
            out.append(f"<li>{body}</li>")
        elif kind == "MIXTAPE_EMBED":
            href = (deref(state, p.get("mixtapeMetadata")) or {}).get("href") or ""
            link = f' <a href="{html.escape(href)}">{html.escape(href)}</a>' if re.match(r"^https?://", href) else ""
            out.append(f'<div class="card">{body}{link}</div>')
        elif kind == "IFRAME":
            res = deref(state, (deref(state, p.get("iframe")) or {}).get("mediaResource"))
            src = res.get("iframeSrc") or res.get("href") or ""
            title = html.escape(res.get("title") or text or "Embedded content")
            link = f' <a href="{html.escape(src)}">{html.escape(src)}</a>' if re.match(r"^https?://", src) else ""
            out.append(f'<div class="card">&#9654; {title} (embedded content, open online){link}</div>')
        elif text:
            out.append(f"<p>{body}</p>")
    if open_list:
        out.append(f"</{open_list}>")
    return "\n".join(out)


def render_header(meta, url, body_html, route):
    """Source line, title, subtitle and byline shown above the story (both routes)."""
    date = meta.get("date_text") or ""
    if meta.get("published"):
        try:
            d = datetime.fromisoformat(meta["published"])
            date = f"{d:%b} {d.day}, {d.year}"
        except ValueError:
            pass
    byline = " · ".join(x for x in (
        f"<b>{html.escape(meta['author'])}</b>" if meta.get("author") else "",
        html.escape(date),
        f"{meta['reading_time']} min read" if meta.get("reading_time") else "",
        f"{meta['claps']:,} claps" if meta.get("claps") else "",
    ) if x)
    subtitle = (meta.get("subtitle") or "").strip()
    squash = lambda s: re.sub(r"\W+", "", s.lower())  # ignore spacing, punctuation and inline tags
    cut = squash(subtitle.rstrip("…."))
    opening = [squash(_plain(p)) for p in re.findall(r"<p>(.*?)</p>", body_html or "", re.S)[:3]]
    if cut and any(p.startswith(cut) for p in opening):
        subtitle = ""  # the subtitle is just the opening paragraph cut short
    via = "downloaded directly from Medium" if route == "medium" else "via Freedium"
    return (f'<div class="source">Source: <a href="{html.escape(url)}">{html.escape(url)}</a> · {via}</div>\n'
            f'<header><h1>{html.escape(meta.get("title") or "")}</h1>'
            + (f'<p class="subtitle">{html.escape(subtitle)}</p>' if subtitle else "")
            + (f'<div class="byline">{byline}</div>' if byline else "")
            + "</header>")
