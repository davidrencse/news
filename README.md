# Medium Library

A local, curated library of Medium articles. Articles are organized by topic and subtopic and saved as clean PDFs.

```
click article → Medium link → freedium-mirror.cfd → headless Chromium → Medium-Library/<topic>/<subtopic>/<title>.pdf → shown in the app
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

## Files

| Path | What |
|---|---|
| `app.py` | FastAPI server: library API, Discover (RSS), job runner |
| `pipeline.py` | Freedium → Playwright → PDF. Isolates the `<article>` and strips Freedium's toolbar and pop-ups |
| `topics.py` | Default topic tree and Medium tags for each subtopic |
| `static/` | The UI (plain HTML, CSS, and JS) |
| `Medium-Library/` | Your PDFs, plus `library.json` (the index of topics and articles) |

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
- The curator adds up to 12 articles per subtopic. After that, a clearly better post replaces the weakest article the curator added that you haven't downloaded. It never removes articles you added yourself or articles you downloaded.
- Subtopics with fewer than 8 articles are filled quickly first. After that, the curator visits one subtopic every 2 minutes.
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
