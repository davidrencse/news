"""Medium link -> Freedium mirror -> headless Chromium -> clean PDF on disk.

Playwright runs on its own thread + event loop so it works regardless of which
event loop the web server uses (Windows selector loops can't spawn subprocesses).
"""
import asyncio
import os
import threading

from playwright.async_api import async_playwright

import polite

FREEDIUM_BASE = os.environ.get("FREEDIUM_BASE", "https://freedium-mirror.cfd").rstrip("/")
MAX_PARALLEL = 2

# Runs inside the Freedium page: isolate the <article>, strip UI chrome, return metadata.
CLEAN_JS = r"""
(sourceUrl) => {
  const art = document.querySelector('article');
  if (!art) return null;
  document.documentElement.classList.remove('dark');

  const text = el => (el ? el.innerText.trim() : '');
  const header = art.querySelector('header');
  const title = text(art.querySelector('h1')) || document.title.replace(/\s*-\s*Freedium$/, '');
  let author = '', date = '', subtitle = '';
  if (header) {
    const by = [...header.querySelectorAll('div')].find(d => /^By\s/.test(text(d)));
    if (by) author = text(by).split('\n')[0].replace(/^By\s+/, '').trim();
    const ps = [...header.querySelectorAll(':scope > p')];
    date = text(ps[0]);
    subtitle = text(ps[1]);
  }

  // Remove Freedium's toolbar, "Download article"/contents box and any leftover controls.
  art.querySelectorAll('nav').forEach(n => {
    let top = n;
    while (top.parentElement && top.parentElement !== art) top = top.parentElement;
    top.remove();
  });
  art.querySelectorAll('section').forEach(s => { if (/Download article/.test(s.innerText)) s.remove(); });
  art.querySelectorAll('button, [role="button"], dialog').forEach(e => e.remove());
  art.classList.remove('shadow-lg', 'rounded-lg', 'overflow-hidden');

  const src = document.createElement('div');
  src.className = 'ml-source';
  src.textContent = 'Source: ' + sourceUrl;
  art.prepend(src);

  art.querySelectorAll('img').forEach(i => { i.loading = 'eager'; i.decoding = 'sync'; });
  document.body.appendChild(art);

  const style = document.createElement('style');
  style.textContent = `
    html, body { background: #fff !important; margin: 0 !important; }
    body > :not(article) { display: none !important; }
    article { box-shadow: none !important; border-radius: 0 !important; max-width: none !important; margin: 0 !important; }
    .ml-source { font: 11px/1.4 system-ui, sans-serif; color: #666; padding: 0 24px 10px; word-break: break-all; }
    img, picture, figure, pre, blockquote, table { break-inside: avoid; }
    img { max-width: 100% !important; height: auto; }
    h1, h2, h3, h4 { break-after: avoid; }
    pre { white-space: pre-wrap !important; word-break: break-word; }
  `;
  document.head.appendChild(style);
  return { title, author, date, subtitle };
}
"""

WAIT_IMAGES_JS = """
() => Promise.all([...document.images].map(img => img.complete ? null :
  new Promise(r => { img.onload = img.onerror = r; setTimeout(r, 15000); })))
"""


class PipelineError(Exception):
    pass


class PdfPipeline:
    def __init__(self):
        self._loop = asyncio.ProactorEventLoop() if os.name == "nt" else asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="playwright")
        self._thread.start()
        self._pw = None
        self._browser = None
        self._sem = None

    def freedium_url(self, medium_url: str) -> str:
        return f"{FREEDIUM_BASE}/{medium_url}"

    async def render(self, medium_url: str, out_path: str, on_stage=lambda s: None) -> dict:
        """Awaitable from any event loop; the work itself runs on the Playwright thread."""
        fut = asyncio.run_coroutine_threadsafe(self._render(medium_url, out_path, on_stage), self._loop)
        return await asyncio.wrap_future(fut)

    async def _ensure_browser(self):
        if self._browser is None or not self._browser.is_connected():
            if self._pw is None:
                self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch()
            self._sem = self._sem or asyncio.Semaphore(MAX_PARALLEL)

    async def _render(self, medium_url, out_path, on_stage):
        await self._ensure_browser()
        async with self._sem:
            ctx = await self._browser.new_context(viewport={"width": 900, "height": 1200}, color_scheme="light")
            page = await ctx.new_page()
            try:
                on_stage("freedium")
                await asyncio.to_thread(polite.limiter(FREEDIUM_BASE).wait)  # space out hits on the free mirror
                try:
                    await page.goto(self.freedium_url(medium_url), wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_selector("article h1", timeout=45000)
                except Exception as e:
                    raise PipelineError(f"Freedium did not return the article ({type(e).__name__}). "
                                        "The mirror may be down or the link unsupported.") from e
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass  # long-polling pages never go idle; the article is already there

                on_stage("download")
                meta = await page.evaluate(CLEAN_JS, medium_url)
                if not meta:
                    raise PipelineError("Could not find the article body on the Freedium page.")
                await page.evaluate(WAIT_IMAGES_JS)
                await page.emulate_media(media="screen")

                on_stage("pdf")
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                tmp = out_path + ".part"
                await page.pdf(path=tmp, format="A4", print_background=True,
                               margin={"top": "14mm", "bottom": "14mm", "left": "10mm", "right": "10mm"})
                os.replace(tmp, out_path)
                return meta
            finally:
                await ctx.close()

    def close(self):
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
