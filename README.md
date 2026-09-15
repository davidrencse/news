# Medium Library

A local, curated library of Medium articles. Articles are organized by topic and subtopic and saved as clean PDFs.

```
click article → check the story on Medium
    free story                     → rendered from Medium's own page ─┐
    member-only / paywalled (402)  → freedium-mirror.cfd              ├→ headless Chromium → Medium-Library/<topic>/<subtopic>/<title>.pdf
    Medium refuses the request     → freedium-mirror.cfd             ─┘
```

## Run

Double-click `run.bat`, or:

```bash
.venv\Scripts\python app.py
```

Then open http://127.0.0.1:8765. On first run, `run.bat` creates `.venv`, installs `requirements.txt`, and downloads Chromium for Playwright.

## Using it

- **Search bar**: type anything and press Enter to search all of Medium. Results are links only; nothing downloads until you click **Read as PDF** (or the card). **Save link** stores the article without downloading it. Pick where results get saved with **Save to**. Matches already in your library appear first. Pasting a Medium URL into the search bar adds it directly.
- **Sidebar**: topics and subtopics (`Medium-Library/` layout). Click a topic to see everything in it. Click a subtopic to open it.
- **Discover tab**: shows the latest stories from the subtopic's Medium tags (`medium.com/feed/tag/<tag>`). Click **Read as PDF** to save the story and run it through the pipeline, or click **Save for later**. Use **edit** to change which tags a subtopic pulls from.
- **Paste a link**: paste any Medium article URL in the top bar, choose where to save it, then click **Add & read**.
- **Library tab**: your saved articles. Articles that already have a PDF open instantly from disk. Click **Re-download** in the reader to get a fresh copy.
- **Custom topics**: click **+ New topic** to create one under `custom/`, or click **+ Add subtopic** inside any topic. Only custom subtopics can be deleted.

## Reading, highlights, and notes

When you open an article, the app saves it into its own folder: `Medium-Library/<topic>/<subtopic>/<title>/`.

- `content.html` is a clean copy of the article. The reader shows it as one continuous scroll.
- `images/` holds the article's images, downloaded once so the article works offline.
- `article.pdf` is the same article as a single continuous PDF page, with no page breaks. Very long articles open fine in browsers but can exceed Adobe Acrobat's 200-inch page limit.

**Math and code:** LaTeX renders with KaTeX. It recognizes `$$…$$`, `\[…\]`, `\(…\)`, and `$…$` when the contents look like TeX, so prices like "$5" stay plain text. Code blocks get syntax highlighting. Both libraries are stored in `static/vendor`, so they work offline.

**Highlights:** select text to open a small toolbar. Pick a color or press **H**. Click a highlight to change its color, add a note, or delete it.

**Notes:** the **Notes** panel lists every highlight in reading order and has a free-form notes box for the article. **Copy** exports your highlights and notes as Markdown.

**Summarize:** select text, then click **✦ Summarize** or press **S**. ChatGPT opens with your study-notes prompt plus the selected text, and the prompt is also copied to your clipboard. For long selections, paste it with Ctrl+V. To change the prompt, click **Edit the summarize prompt** in the notes panel.

Highlights and notes are saved in `notes/<article id>.json`, so they survive re-downloading or moving the article. Articles downloaded before the continuous reader existed are upgraded automatically the first time you open them. The **PDF** button opens the single-page PDF without the browser's toolbar or page thumbnails.

## Files

| Path | What |
|---|---|
| `app.py` | FastAPI server: library API, Discover (RSS), job runner |
| `medium_render.py` | Checks each story on Medium. It renders free stories from the content embedded in the post page into clean HTML, and sends member-only stories (or ones Medium refuses) to Freedium |
| `pipeline.py` | Playwright → PDF. Prints the Medium-rendered story, or loads the Freedium page and strips its toolbar and pop-ups |
| `topics.py` | Default topic tree and Medium tags for each subtopic |
| `static/` | The UI (plain HTML, CSS, and JS) |
| `Medium-Library/` | Your PDFs, plus `library.json` (the index of topics and articles) |
| `selftest.py` | Offline self-test of the library API, search index, renderer and curator |

## Checking the app still works

`python selftest.py` runs the whole app against stubbed Medium pages: the library API, the search
index, the article renderer and the curator. It never touches the network and never writes to your
library — it works in a temporary folder and prints a line per check.

## Settings

- `FREEDIUM_BASE`: the mirror to use. Defaults to `https://freedium-mirror.cfd`. Change it if the mirror goes down, for example `set FREEDIUM_BASE=https://freedium.cfd`.
- `PORT`: the server port. Defaults to `8765`.
- **Storage location**: PDFs, `library.json`, and the search index are stored next to the app. If that drive has less than 10 GB free when the app starts, the app moves them to `E:\storage\medium-library` once and remembers that location in `data-location.txt`. To pick a location yourself, set `MEDIUM_LIBRARY_DATA`. Separately, the indexer pauses whenever its drive has less than 2 GB free.
## How search works

The app has its own search engine. It doesn't need an API key or an outside search service.

- Medium publishes a sitemap for search engines, with one file per day listing every post published that day. Medium's `robots.txt` allows reading these files.
- While the server runs, `medium_index.py` downloads those daily files in the background, newest day first, one file every couple of seconds. It builds a SQLite full-text index in `search-index/medium.db`.
- Titles come from each post's URL, because Medium builds the URL from the title.
- Searches run against this local index and return results in milliseconds.
- While the index covers less than 30 days, results also include matching posts from Medium's live tag feeds.
- Click the index panel at the bottom of the sidebar to see progress, choose how far back to index (30 days to everything), or pause indexing.

Search results are only links. Nothing downloads until you open an article.

## Always-fresh library

While the server runs, `curator.py` keeps each subtopic stocked with trending Medium articles:

- It visits one subtopic at a time. It finds candidates in the local index, reads a few post pages it hasn't seen before, and keeps posts whose Medium tags fit the subtopic.
- Posts are ranked by claps weighted by age, so fresh popular posts can outrank older ones.
- The curator adds up to its subtopic's share of the topic target. After that, a clearly better post replaces the weakest article the curator added that you haven't downloaded. It never removes articles you added yourself or articles you downloaded.
- Subtopics with fewer than 8 articles are filled quickly first. After that, the curator visits one subtopic every 2 minutes.
- Each topic aims for at least 1,120 articles. Its subtopics share that target, with at least 12 each. When a subtopic runs out of candidates, the other subtopics in that topic make up the difference.
- That target is a ceiling the curator walks towards, not a download. Every post page it reads goes through the shared rate limiter at about one request a second, so a fresh library fills over days of uptime, and only articles you actually open are turned into PDFs.
- Every article shows whether it is **🔒 Member-only** (paywalled) or **Free**. The status comes from the story's Medium page. A background check fills it in for older articles and refreshes their clap counts. Use the **All / Free / Member-only** filters above any list to show just one kind.
- Articles added in the last 24 hours show a **New** badge, and the page refreshes itself when the library changes.
- To turn this off, click the index panel in the sidebar and clear **Keep adding trending articles automatically**.

## Staying under rate limits

Every request to another site goes through `polite.py`, including sitemaps, post pages, tag feeds, and Freedium.

- Each site gets a minimum gap between requests: 1 second for Medium, 3 seconds for the Freedium mirror.
- When a site pushes back (HTTP 403, 429, or 503), the gap grows. New requests wait at least as long as the site's `Retry-After` header asks, and the gap shrinks back as requests succeed.
- The curator skips its turn while Medium has asked the app to wait.
- The app never retries around a refusal. The sidebar panel shows when Medium has asked the app to slow down.

## Notes

- Medium rate-limits bursts of feed requests (HTTP 429). Discover fetches one tag at a time and caches feeds for 15 minutes. If some tags fail, wait a minute and click **refresh**.
- PDFs are saved for your personal offline reading. Please respect authors' rights and don't redistribute them.
"# news" 
