"""Article pipeline: Medium link -> clean article -> local copy -> continuous reader + single-page PDF.

1. medium_render.prepare() checks the story on Medium. Free stories are rendered from Medium's own page;
   member-only ones (or ones Medium refuses) are loaded through Freedium and cleaned into the same HTML.
2. Images are downloaded next to the article so it reads offline.
3. content.html (the clean article) is written for the in-app reader, then printed with the same styles,
   math and code highlighting into article.pdf: one continuous page with no page breaks.

Playwright runs on its own thread + event loop so it works regardless of which
event loop the web server uses (Windows selector loops can't spawn subprocesses).
"""
import asyncio
import html
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

import medium_render
import polite

FREEDIUM_BASE = os.environ.get("FREEDIUM_BASE", "https://freedium-mirror.cfd").rstrip("/")
# Mirrors tried in order when the one above doesn't return the article. Freedium mirrors come and go,
# so a single dead host must not be the end of the paywalled route. Setting FREEDIUM_MIRRORS (a
# comma-separated list) replaces the built-in fallback rather than adding to it.
_EXTRA = [m.strip().rstrip("/") for m in os.environ.get("FREEDIUM_MIRRORS", "").split(",") if m.strip()]
FREEDIUM_MIRRORS = list(dict.fromkeys([FREEDIUM_BASE] + (_EXTRA or ["https://freedium.cfd"])))
CHROMIUM_PATH = os.environ.get("CHROMIUM_PATH")  # use a Chromium already on this machine
MAX_PARALLEL = 2
PAGE_WIDTH_PX = 794  # A4 width; the PDF is one page this wide and as tall as the article

# Runs inside the Freedium page: metadata plus the story rebuilt from a small set of safe tags.
SANITIZE_JS = r"""
() => {
  const art = document.querySelector('article');
  if (!art) return null;
  const text = el => (el ? el.innerText.trim() : '');
  const header = art.querySelector('header');
  const meta = { title: text(art.querySelector('h1')) || document.title.replace(/\s*-\s*Freedium$/, ''),
                 author: '', date_text: '', subtitle: '' };
  if (header) {
    const by = [...header.querySelectorAll('div')].find(d => /^By\s/.test(text(d)));
    if (by) meta.author = text(by).split('\n')[0].replace(/^By\s+/, '').trim();
    const ps = [...header.querySelectorAll(':scope > p')];
    meta.date_text = text(ps[0]);
    meta.subtitle = text(ps[1]);
  }
  const esc = s => s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const http = u => (/^https?:\/\//i.test(u || '') ? u : '');
  const RENAME = { B: 'strong', I: 'em', H1: 'h2', H2: 'h2', H3: 'h2', H4: 'h3', H5: 'h3', H6: 'h3' };
  const KEEP = new Set(['P', 'BLOCKQUOTE', 'UL', 'OL', 'LI', 'EM', 'STRONG', 'CODE', 'SUP', 'SUB', 'S', 'DEL',
                        'FIGURE', 'FIGCAPTION', 'TABLE', 'THEAD', 'TBODY', 'TR', 'TD', 'TH']);
  const DROP = new Set(['SCRIPT', 'STYLE', 'BUTTON', 'SVG', 'NOSCRIPT', 'FORM', 'INPUT', 'NAV', 'DIALOG', 'SELECT']);
  function walk(node) {
    if (node.nodeType === 3) return esc(node.nodeValue);
    if (node.nodeType !== 1) return '';
    const tag = node.tagName;
    if (DROP.has(tag)) return '';
    const inner = () => [...node.childNodes].map(walk).join('');
    if (tag === 'PRE') {
      const cls = `${node.className} ${node.querySelector('code')?.className || ''}`.match(/(?:language|lang)-([\w+#-]+)/);
      return `<pre${cls ? ` data-lang="${esc(cls[1])}"` : ''}><code>${esc(node.innerText.replace(/\n$/, ''))}</code></pre>`;
    }
    if (tag === 'IMG') {
      const srcset = (node.getAttribute('srcset') || '').split(',').pop().trim().split(' ')[0];
      const src = http(node.currentSrc) || http(node.getAttribute('src')) || http(srcset);
      return src ? `<img src="${esc(src)}" alt="${esc(node.alt || '')}">` : '';
    }
    if (tag === 'PICTURE') { const img = node.querySelector('img'); return img ? walk(img) : ''; }
    if (tag === 'A') { const href = http(node.getAttribute('href')); return href ? `<a href="${esc(href)}">${inner()}</a>` : inner(); }
    if (tag === 'IFRAME') {
      const src = http(node.src);
      return src ? `<div class="card">&#9654; Embedded content (open online) <a href="${esc(src)}">${esc(src)}</a></div>` : '';
    }
    if (tag === 'BR') return '<br>';
    if (tag === 'HR') return '<hr class="section">';
    if (RENAME[tag]) return `<${RENAME[tag]}>${inner()}</${RENAME[tag]}>`;
    if (KEEP.has(tag)) return `<${tag.toLowerCase()}>${inner()}</${tag.toLowerCase()}>`;
    const content = inner();  // unknown wrappers (div, span, section…) are unwrapped
    return /^(DIV|SECTION|ARTICLE|MAIN|HEADER|FOOTER)$/.test(tag) && content.trim() ? `${content}\n` : content;
  }
  meta.body = walk(art.querySelector('.prose') || art).trim();
  return meta;
}
"""

IMG_SRC = re.compile(r'<img src="(https?://[^"]+)"')
MAGIC = ((b"\x89PNG", ".png"), (b"\xff\xd8", ".jpg"), (b"GIF8", ".gif"), (b"RIFF", ".webp"), (b"<svg", ".svg"), (b"<?xml", ".svg"))


class PipelineError(Exception):
    pass


def localize_images(content, out_dir):
    """Download every image once, in parallel, and point the article at the local copies
    (the online URL is kept for any image that fails)."""
    urls = list(dict.fromkeys(html.unescape(u) for u in IMG_SRC.findall(content)))

    def fetch(item):
        i, url = item
        try:
            data = polite.get(url, timeout=30, priority=True)
            ext = next((e for magic, e in MAGIC if data.startswith(magic)), None) \
                or os.path.splitext(urlsplit(url).path)[1][:5] or ".img"
            os.makedirs(os.path.join(out_dir, "images"), exist_ok=True)
            with open(os.path.join(out_dir, "images", f"{i:03d}{ext}"), "wb") as f:
                f.write(data)
        except Exception:
            return url, None  # a missing image is not worth failing the article for; keep the online URL
        return url, f"images/{i:03d}{ext}"

    with ThreadPoolExecutor(6) as pool:
        local = {url: name for url, name in pool.map(fetch, enumerate(urls, 1)) if name}
    return IMG_SRC.sub(lambda m: f'<img src="{local.get(html.unescape(m.group(1)), m.group(1))}"', content)


class PdfPipeline:
    def __init__(self, assets_dir):
        self.assets_dir = assets_dir  # the app's static/ folder (article.css, article.js, vendor/)
        self._loop = asyncio.ProactorEventLoop() if os.name == "nt" else asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="playwright")
        self._thread.start()
        self._pw = None
        self._browser = None
        self._sem = None
        self._closed = False
        # asyncio primitives belong to the loop that created them, so these are built on the Playwright
        # loop rather than wherever the first render happens to come from.
        self._launching = asyncio.run_coroutine_threadsafe(self._make_lock(), self._loop).result(10)

    @staticmethod
    async def _make_lock():
        return asyncio.Lock()

    def freedium_url(self, medium_url: str, base=None) -> str:
        return f"{(base or FREEDIUM_BASE).rstrip('/')}/{medium_url}"

    async def render(self, medium_url: str, out_dir: str, on_stage=lambda stage, **info: None) -> dict:
        """Awaitable from any event loop; the work itself runs on the Playwright thread.
        Writes content.html, images/ and article.pdf into out_dir and returns the article's metadata.

        Retried once on a browser-level failure: Chromium can be killed by the OS or die on a bad page,
        and the second attempt gets a freshly launched one rather than failing in the user's face.
        """
        if self._closed or not self._thread.is_alive():
            raise PipelineError("The PDF engine is not running. Restart the app.")
        for attempt in (1, 2):
            fut = asyncio.run_coroutine_threadsafe(self._render(medium_url, out_dir, on_stage), self._loop)
            try:
                return await asyncio.wrap_future(fut)
            except PipelineError:
                raise  # the article itself is the problem; retrying changes nothing
            except Exception as e:
                if attempt == 2:
                    raise PipelineError(f"The PDF engine failed twice: {type(e).__name__}: {e}") from e
                await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(self._drop_browser(), self._loop))

    async def _drop_browser(self):
        """Throw away the current browser so the next render launches a fresh one."""
        browser, self._browser = self._browser, None
        if browser:
            try:
                await browser.close()
            except Exception:
                pass  # it is already gone; that is why we are here

    async def _ensure_browser(self):
        # One launch at a time: without this, simultaneous downloads each start their own Chromium
        # and all but the last are orphaned for the life of the process.
        async with self._launching:
            if self._browser is not None and self._browser.is_connected():
                return
            if self._pw is None:
                self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                **({"executable_path": CHROMIUM_PATH} if CHROMIUM_PATH else {}))
            self._sem = self._sem or asyncio.Semaphore(MAX_PARALLEL)

    async def _render(self, medium_url, out_dir, on_stage):
        await self._ensure_browser()
        async with self._sem:
            ctx = await self._browser.new_context(viewport={"width": PAGE_WIDTH_PX, "height": 1123}, color_scheme="light")
            page = await ctx.new_page()
            try:
                # Free stories come straight from Medium; only paywalled or refused ones go through Freedium.
                on_stage("check")
                try:
                    route = await asyncio.to_thread(medium_render.prepare, medium_url)
                except LookupError as e:
                    raise PipelineError(str(e)) from e
                if route["route"] == "medium":
                    on_stage("medium", route="medium", reason="free story")
                    meta, body = route["meta"], route["body"]
                else:
                    on_stage("freedium", route="freedium", reason=route["reason"],
                             freedium_url=self.freedium_url(medium_url))
                    meta = await self._from_freedium(page, medium_url, on_stage)
                    body = meta.pop("body")
                content = medium_render.render_header(meta, medium_url, body, route["route"]) + "\n" + body

                on_stage("images")
                os.makedirs(out_dir, exist_ok=True)
                content = await asyncio.to_thread(localize_images, content, out_dir)
                with open(os.path.join(out_dir, "content.html"), "w", encoding="utf-8") as f:
                    f.write(content)

                on_stage("pdf")
                await self._print(page, content, out_dir)
                return dict(meta, route=route["route"], reason=route.get("reason", "free story"))
            finally:
                await ctx.close()

    async def _from_freedium(self, page, medium_url, on_stage=None):
        """Try each mirror in turn; a dead or empty one must not end the paywalled route."""
        problems = []
        for base in FREEDIUM_MIRRORS:
            url = self.freedium_url(medium_url, base)
            if on_stage:
                on_stage("freedium", freedium_url=url)
            await asyncio.to_thread(polite.limiter(base).wait, True)  # space out hits on the free mirror
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_selector("article h1", timeout=45000)
            except Exception as e:
                problems.append(f"{urlsplit(base).netloc}: {type(e).__name__}")
                continue
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass  # long-polling pages never go idle; the article is already there
            try:
                meta = await page.evaluate(SANITIZE_JS)
            except Exception as e:
                problems.append(f"{urlsplit(base).netloc}: {type(e).__name__}")
                continue
            if meta and (meta.get("body") or "").strip():
                return meta
            problems.append(f"{urlsplit(base).netloc}: no article body")
        raise PipelineError("No Freedium mirror returned the article (" + "; ".join(problems) + "). "
                            "The mirrors may be down, or the link unsupported.")

    def _shell(self, content):
        asset = lambda rel: Path(self.assets_dir, *rel.split("/")).as_uri()
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<link rel="stylesheet" href="{asset('vendor/katex/katex.min.css')}">
<link rel="stylesheet" href="{asset('article.css')}">
<script src="{asset('vendor/katex/katex.min.js')}"></script>
<script src="{asset('vendor/katex/auto-render.min.js')}"></script>
<script src="{asset('vendor/hljs/highlight.min.js')}"></script>
<script src="{asset('article.js')}"></script>
</head><body class="print"><article class="doc">{content}</article>
<script>
  const doc = document.querySelector('.doc');
  ArticleDoc.render(doc);
  ArticleDoc.ready(doc).then(() => {{ window.__docReady = true; }});
</script></body></html>"""

    async def _print(self, page, content, out_dir):
        """One continuous PDF page as tall as the article."""
        shell = os.path.join(out_dir, "print.html")
        with open(shell, "w", encoding="utf-8") as f:
            f.write(self._shell(content))
        try:
            await page.goto(Path(shell).as_uri(), wait_until="load", timeout=60000)
            try:
                await page.wait_for_function("window.__docReady === true", timeout=40000)
            except Exception:
                pass  # print what has loaded
            await page.emulate_media(media="screen")
            height = await page.evaluate("Math.ceil(document.documentElement.scrollHeight)")
            tmp = os.path.join(out_dir, "article.pdf.part")
            await page.pdf(path=tmp, width=f"{PAGE_WIDTH_PX}px", height=f"{height + 4}px", print_background=True,
                           margin={"top": "0", "right": "0", "bottom": "0", "left": "0"}, page_ranges="1")
            os.replace(tmp, os.path.join(out_dir, "article.pdf"))
        finally:
            if os.path.exists(shell):
                os.remove(shell)

    def close(self):
        self._closed = True

        async def _close():
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        try:
            asyncio.run_coroutine_threadsafe(_close(), self._loop).result(timeout=10)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
