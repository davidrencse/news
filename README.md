<h1 align="center">Library of Babel</h1>

<p align="center">
  A personal reading room for articles, research, and vulnerability records.<br>
  Keep long-form reading, notes, and CVE intelligence together.
</p>

<p align="center">
  <sub>A local Python app with a browser-based reader, optional Telegram backups, and on-demand CVE enrichment.</sub>
</p>

---

## Contents

- [Quick start](#run-it) · [Phone access](#on-your-phone) · [First session](#first-session)
- [Run it](#run-it) · [How it works](#how-it-works)
- [The library](#the-library) · [Discover and recommendations](#discover-and-recommendations) · [Reading, highlights, and notes](#reading-highlights-and-notes) · [Vulnerabilities](#vulnerabilities)
- [Search](#search) · [The curator](#the-curator) · [Speed](#speed) · [Rate limits](#rate-limits)
- [Settings](#settings) · [Files](#files)
- [Storage and backups](#storage-and-backups) · [API](#api) · [Troubleshooting](#troubleshooting) · [Development checks](#checking-the-app-still-works)

## Run it

### Requirements

- Python **3.10 or newer**, available as `python` (or `python3` on macOS/Linux).
- Chromium installed through Playwright for article rendering and PDF generation.
- Internet access for installation, article downloads, indexing, and CVE imports.
- Writable disk space for articles, images, PDFs, and SQLite indexes. Usage grows with your library;
  the sitemap indexer pauses when its drive has less than 2 GB free.

The backend uses FastAPI and Uvicorn. The frontend is plain JavaScript and CSS with vendored
rendering libraries; no Node.js, frontend build step, Medium API key, or OpenAI API key is required.
Telegram is optional for the standalone setup below.

> **Already have a library archived in Telegram?** Use [Existing Windows + Telegram setup](#existing-windows--telegram-setup)
> and launch with `run.bat`. The standalone commands create a separate local data set; they do not restore
> your Telegram archive.

### Standalone setup (Windows PowerShell)

Run these commands from the project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
$env:MEDIUM_LIBRARY_DATA = Join-Path $env:LOCALAPPDATA "LibraryOfBabel"
$env:HOST = "127.0.0.1"
.\.venv\Scripts\python.exe app.py
```

Open **http://127.0.0.1:8765**. Stop the server with **Ctrl+C**.
For subsequent launches, repeat the two environment assignments and the `app.py` command in the
same PowerShell window. Explicitly choosing a data directory keeps your library location predictable.

### Standalone setup (macOS/Linux)

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
export MEDIUM_LIBRARY_DATA="$HOME/.local/share/library-of-babel"
export HOST=127.0.0.1
.venv/bin/python app.py
```

If Chromium reports missing Linux system libraries, install its dependencies with
`.venv/bin/python -m playwright install --with-deps chromium`.
Repeat the `export` assignments when launching from a new terminal.

### Existing Windows + Telegram setup

`run.bat` connects this app to an existing Telegram File Storage System (TGFS) checkout. TGFS is a
separate project: cloning this repository does not clone TGFS, create its Telegram session, or set
up its encryption key. Those credentials and dependencies stay in TGFS and are **not** installed by
this project's `requirements.txt`.

Before launching, clone/configure TGFS using its own setup instructions. Its checkout must have
`src/tgfs`, a `.venv` with TGFS requirements installed, a working Telegram session, and the required
encryption key in the TGFS-configured key store. The selected remote archive must already contain
`Medium-Library`, `notes`, `CVEs`, and `search-index` under one archive root. The default root is
`/newsletter-archive-2026-10-02`; set it to the archive path that actually holds your data.

For example, in PowerShell, set paths for your machine and start the launcher from this repository:

```powershell
$env:TGFS_PROJECT = "C:\path\to\telegram-file-storage-system"
$env:MEDIUM_LIBRARY_DATA = "E:\storage\newsletter-active"
$env:TGFS_NEWSLETTER_PATH = "/newsletter-archive-2026-10-02"
.\run.bat
```

You can also double-click `run.bat` when using its defaults: the TGFS checkout at
`%USERPROFILE%\Desktop\telegram-file-storage-system` and data cache at
`E:\storage\newsletter-active` when the E: drive exists, otherwise `%LOCALAPPDATA%\LibraryOfBabel`.
On the first app launch, the batch file creates the app's `.venv`,
installs requirements, and downloads Chromium. If required cached files are absent, it restores
from the configured archive and verifies the library, CVE catalog, search database, and notes folder
before starting the app. If the local data directory already contains files but is incomplete, it
asks before restoring because the archive can add or replace files there. The launcher waits until
this app's page is ready before opening a browser, then uploads changed files only after a clean app
exit. See [Storage and backups](#storage-and-backups) for the archive behavior.

The launcher uses those defaults only when `MEDIUM_LIBRARY_DATA` and `TGFS_PROJECT` are not already
set in the environment; set them before running or edit the assignments to change those paths.
Its browser address follows `HOST` and `PORT`; for `HOST=0.0.0.0` it opens the local loopback address.
It refuses to open a different server already using that port. An existing `.venv` skips dependency installation, so after updating
the project rerun the `pip install` and `playwright install` commands above.

## First session

1. Choose a topic and subtopic, paste a Medium article URL, and select **Add & read**.
2. Wait for the download to finish. The reader saves article HTML, images, and a PDF locally.
3. Select text to highlight it; use **Notes** for annotations and **Notebook** to review them later.
4. Try **Discover** or search. The sitemap index builds in the background, so coverage improves over time.
5. Open the index panel to adjust the indexing window, pause indexing, or disable automatic curation.
6. Optionally open **Vulnerabilities** to import a CVE or start the complete catalog download.

Starting the server also starts the sitemap indexer and curator. Saving a link alone does not
download its article, but these background services do make network requests.

## On your phone

The app serves the same pages to a phone, laid out for a small screen: the topic tree becomes a
slide-over drawer, the paste box folds behind **＋**, and the reader fills the screen.

1. Start the app with `HOST=0.0.0.0` (`$env:HOST = "0.0.0.0"` in PowerShell). It prints a second address, like `http://192.168.1.24:8765`.
2. Put the phone on the same Wi-Fi and open that address.
3. In Safari, tap **Share → Add to Home Screen**. It then opens like an app, with its own icon and
   no browser chrome. On Android, Chrome's **Install app** does the same.

Offline caching requires the browser to register the service worker. This needs a secure context:
localhost on the computer, or HTTPS when accessed from another device. Plain HTTP at a LAN address
supports online reading but does not enable service-worker offline caching. Installation options
also depend on the browser. The app itself serves HTTP; HTTPS requires separate configuration.

When the service worker is active, it caches the interface and opened article content for offline
reading. Browser storage may be cleared or evicted, so this cache is not a backup. Searching,
downloading, adding articles, and saving notes require the running server.

There is no login. Anyone who can reach the server can read and modify your library, so use it on a network you
trust, or set `HOST=127.0.0.1` to keep the app on your computer only.

> iOS only installs web apps this way — an App Store build would need an Apple developer account
> and a native wrapper, which this project doesn't have.

## How it works

```
click article → check the story on Medium
    free story                     → rendered from Medium's own page ─┐
    member-only / paywalled (402)  → freedium-mirror.cfd              ├→ headless Chromium → content.html + article.pdf
    Medium refuses the request     → freedium-mirror.cfd             ─┘
```

Nothing is downloaded until you open an article. Everything else — search, Discover,
recommendations — deals in links only.

## The library

- **Sidebar:** topics and subtopics, mirroring the `Medium-Library/` folder layout. Click a topic to
  see everything in it, a subtopic to open just that one.
- **Badges:** every article shows **🔒 Member-only** or **Free**, and the **All / Free / Member-only**
  filters above any list narrow it down. Articles added in the last 24 hours show a **New** badge.
- **Search bar:** press `/`, type anything, press Enter to search all of Medium. Results are links;
  **Save link** stores one without downloading. Pasting a Medium URL adds it directly.
- **Paste a link:** drop a URL in the top bar, choose where it goes, click **Add & read**.
- **Removing:** a link you never downloaded goes right away, with a few seconds to **Undo**. A
  downloaded article or one with notes asks first.
- **Custom topics:** **+ New topic** creates one under `custom/`; **+ Add subtopic** works inside any
  topic. Only custom subtopics can be deleted.

## Discover and recommendations

**For you** ranks posts against what you've already read. **Trending** is the plain popularity list.

<img src="docs/images/discover-foryou.png" alt="Discover: For you, with a label bar and cards explaining why each post was picked" width="900">

Each card says why it's there — *"Because you read #cybersecurity, #machine-learning"* — so the
ranking is never a black box.

**What it learns from.** Highlights and notes count most, opening an article counts, downloading it
counts a little less, and saving one by hand without opening it counts a little. Articles the curator
added on its own count for nothing — that's the app's opinion, not yours. Older reading fades, so the
page follows you when your interests move, and until you've read enough, the topics you set up stand
in for a history.

**Where the candidates come from.** Two places. The curator's page cache holds a few thousand posts
with full metadata. The search index holds every post Medium published in the window you index, which
is far more reach but only what a URL reveals — so your own subtopics, ordered by how much you read
each one, are used as the queries against it. A share of every page is reserved for posts the curator
has never opened; otherwise the richer cards would always win and recommendations would never reach
past what the app already knows.

**How it ranks.** Labels are a post's Medium tags plus the subtopic it fits. Rare labels count for
more than common ones, and broad ones like `technology` barely count. Label overlap and title-word
overlap carry the score, with a bonus for authors you read, claps-weighted-by-age (or Medium's own
priority, for posts the curator hasn't opened) and freshness on top.

**How the page is shaped.** Restatements of the same headline and a third post from the same author
move down rather than out. Every seventh slot goes to something **New to you** — a post in a subtopic
you read that shares none of the labels which characterise your taste — because a profile built from
your own history can't widen on its own. The order rotates each hour, so the page isn't the same list
every time you open it.

**Not for me.** Waving a post away stops it being suggested and makes posts like it rank lower, with
a few seconds to **Undo**. Dismissals live in `library.json`.

**Narrowing by label.** Type a label, or click one from your own top labels, and stack as many as you
like. The Free / Member-only filters still apply — posts from the index are left out of those two,
since a URL doesn't say whether a story is paywalled.

<img src="docs/images/labels.png" alt="The label bar with #cybersecurity selected, narrowing the ranked list" width="900">

Recommending costs no requests to Medium. A ranking is reused for 60 seconds, or until your library
changes.

```
GET  /api/recommend?labels=llm,rust&access=all|free|locked
POST /api/recommend/dismiss     {"url": "...", "undo": false}
POST /api/articles/{id}/read
```

Inside a subtopic, Discover instead shows the latest stories from that subtopic's Medium tags
(`medium.com/feed/tag/<tag>`). **edit** changes which tags it pulls from.

## Reading, highlights, and notes

<img src="docs/images/reader.png" alt="The reader with the highlight toolbar open over a selected paragraph" width="900">

Opening an article saves it to `Medium-Library/<topic>/<subtopic>/<title>/`:

| File | What |
|---|---|
| `content.html` | A clean copy of the article, shown as one continuous scroll |
| `images/` | The article's images, downloaded once so it works offline |
| `article.pdf` | The same article as a single continuous page, no page breaks |

**The Library of Babel.** Opening an article plays a short 3D intro. The camera drops down the air
shaft of an endless stack of hexagonal galleries to the article's own leather-bound volume, which
slides off the shelf and opens. While the download runs, the pages show Babel's random letters and
the current download step. Once it finishes, the next page settles into the article's first lines and
the camera falls into the page as the reader appears. It never delays reading. **Esc**, **Space**, or
a click skips it, and the **⬡** button next to the theme toggle turns it off. It starts off when your
system asks for reduced motion. Three.js is loaded from `static/vendor` only on first use.

**Highlights.** Select text for the toolbar. Pick a color or press **H**. Click a highlight to
recolor it, attach a note, or delete it.

**Notes.** The **Notes** panel lists every highlight in reading order, with a free-form notes box per
article. **Copy** exports highlights and notes as Markdown.

**Summarize.** Select text, then **✦ Summarize** or press **S**: ChatGPT opens with your study-notes
prompt plus the selection, also copied to your clipboard. The **Summary** box at the top of the Notes
panel can **Draft key points** offline, or send the whole article to ChatGPT.

**Notebook.** **✎ Notebook** in the sidebar gathers every summary, note, and highlight in one place —
filter it, edit in place, copy one entry or all of it as Markdown, or jump back into the article.

**Math and code.** LaTeX renders with KaTeX (`$$…$$`, `\[…\]`, `\(…\)`, and `$…$` when the contents
look like TeX, so "$5" stays plain text). Code blocks get syntax highlighting. Both libraries live in
`static/vendor`, so they work offline.

Highlights, notes, and summaries live in `notes/<article id>.json`, so they survive re-downloading or
moving an article. Very long articles can exceed Adobe Acrobat's 200-inch page limit, though browsers
open them fine.

## Vulnerabilities

Open **Vulnerabilities** in the sidebar and choose **Import complete CVE catalog** to download the
official CVE List V5 baseline and its recent hourly deltas. The large first download runs in the
background; its progress appears in the view. The searchable local index includes every CVE JSON 5
record and preserves each complete source record, including CNA, CVE Program, and other ADP
containers, affected versions, metrics, and references. Later updates use the repository's release
deltas instead of downloading the full baseline again. Search by identifier, description, or year,
open the complete canonical record, and add selected CVEs to your shelf.

The CVE List index is stored in `CVEs/cvelistV5.sqlite3`; manually enriched shelf records remain in
`CVEs/cves.json`. Add a CVE ID or choose **Add to shelf** to also check NVD for CVSS, CPE, and
references; CVE.org/MITRE; GitHub Advisories and OSV for package ranges and fixes; CISA KEV for
known exploitation; and Red Hat, Microsoft MSRC, and Cisco. Each source is queried independently.
If set, `NVD_API_KEY` is sent to NVD to use its higher request allowance; the public API is queried
without a key otherwise.

## Search

The app has its own search engine — no API key, no outside search service.

- Medium publishes a sitemap for search engines, one file per day listing that day's posts. Medium's
  `robots.txt` allows reading these files.
- `medium_index.py` downloads them in the background, newest first, one file every couple of seconds,
  building a SQLite full-text index in `search-index/medium.db`.
- Titles come from each post's URL, because Medium builds the URL from the title.
- Searches hit that local index and return in milliseconds. While the index covers less than 30 days,
  results also include matching posts from Medium's live tag feeds.
- The index panel at the bottom of the sidebar shows progress and lets you choose how far back to
  index (30 days to everything) or pause it.

**Finding, then ranking.** SQLite picks a wide band of candidates by bm25 with the title weighted
above the author — which costs about the same for 500 rows as for 20, because the sort dominates —
and `relevance.py` re-scores that band on how much of the query a title really contains, whether the
words sit together and in order, whether they matched as typed or only after stemming, how fresh the
post is, and how highly Medium's own sitemap rates it. So searching **transformer** brings back the
architecture rather than everything about *transformation*, and **llm agents** puts the two words
side by side first.

**When nothing matches everything.** Rather than an all-or-nothing fallback, the net widens in steps
— every word, then the last word left open (`kubernete` still finds Kubernetes), then any word — and
partial matches sort below full ones, each card saying which word it *doesn't* mention.

**Operators.**

| Type this | To get |
|---|---|
| `"vector database"` | Those words, in that order |
| `-crypto` | Nothing mentioning crypto |
| `author:predict`, `by:"Will Lockett"` | One writer or publication |
| `since:30d`, `after:2026-08-01` | Published since then (`d`, `w`, `m`, `y`, or a date) |
| `before:2026-07` | Published before then |

The page echoes how it read your query, so `since:60d` shows up as a real date.

**Your own reading, gently.** Ambiguous queries lean toward what you read — search **agents** and AI
agents come before insurance agents — as a tie-break that only reorders results that already match.
Posts the curator has opened show their real title, subtitle, image, claps and member-only badge;
the rest are the bare link the index knows about. Your library is searched too, by the same rules,
and appears above the Medium results.

Local index searches make no requests to Medium. The live-feed fallback can make requests while
the index is young or a query has no indexed results.

## The curator

While the server runs, `curator.py` keeps each subtopic stocked with trending member-only articles.
Automatic collection skips free stories and drops unread free stories it previously added. Links you
added or downloaded are kept, including free links explicitly requested through Bulk add.

- One subtopic at a time: candidates from the local index, a few unread post pages, keeping posts
  whose Medium tags fit the subtopic. Ranked by claps weighted by age.
- At least 12 articles per subtopic, with more allocated from the topic target. Past that, a clearly better post replaces the weakest auto-added
  article you never downloaded. Articles you added or downloaded are never removed.
- Subtopics under 8 articles fill quickly first; maintenance cycles then wait 60 seconds between subtopics.
- Each topic aims for at least 1,120 articles, shared across its subtopics, at least 12 each. When one
  subtopic runs dry, the others make up the difference.
- A background check records whether each article is member-only or free, and refreshes clap counts.
- **Bulk add:** in the index panel, enter how many free and member-only articles to add and click
  Save. It uses posts it has already read first and never swaps out existing articles.
- To stop it, clear **Keep adding trending articles automatically** in the index panel.

## Speed

- **Library loading:** the home page requests at most 60 cards first, then loads counts and index status;
  count refreshes preserve existing card elements. Saved article HTML displays while notes load, and
  saved articles skip the 3D download intro. See [loading-performance.md](docs/loading-performance.md)
  for measured desktop/mobile diagnostics and the reproducible benchmark command.
- **Processing capacity:** up to 4 article renders run concurrently, with up to 12 image-download
  workers per active article. Curation reads up to 10 new post pages per cycle and can add or rotate
  up to 6 links during a maintenance cycle. Filling cycles wait 1 second; maintenance cycles wait
  60 seconds. Sitemap indexing adds only 0.25 seconds of idle time after each completed sitemap.
  All Medium/Freedium requests still use the existing shared rate limiter and backoff.
- **CVE imports:** catalog writes commit in batches of 10,000 records. Enrichment already queries
  all 8 sources concurrently. Catalog releases are applied in order so newer deltas win.
- **Search** re-ranks a wide band of candidates in one pass, and consecutive pages rank the same
  band, so page 2 carries on exactly where page 1 stopped.
- **Recommendations** are cached for 60 seconds, so switching labels and filters is instant; the
  index half of the candidate pool is held for an hour, since rebuilding it is seconds of work
  for the same answer.
- **Browser caching:** the app's scripts and styles and the KaTeX/highlight.js libraries are cached
  for a year (their URLs carry a version stamp, so updates still load immediately), article images
  for a day, and article HTML, PDFs, and the main page are revalidated each time — a cheap 304 when
  nothing changed.
- Downloaded articles open straight from disk, with no network round trip.

## Rate limits

Every request to another site goes through `polite.py` — sitemaps, post pages, tag feeds, Freedium.

- A minimum gap per site: 1 second for Medium, 3 seconds for the Freedium mirror.
- On HTTP 403, 429, or 503 the gap grows, new requests wait at least as long as `Retry-After` asks,
  and the gap shrinks back as requests succeed.
- The curator skips its turn while Medium has asked the app to wait, and the sidebar panel says so.
- The app never retries around a refusal.

Medium also rate-limits bursts of feed requests. Discover fetches one tag at a time and caches feeds
for 15 minutes; if some tags fail, wait a minute and click **refresh**.

## Settings

Set environment variables in the shell before starting the app; it does not load a `.env` file.
PowerShell uses `$env:NAME = "value"`, Command Prompt uses `set "NAME=value"`, and macOS/Linux
shells use `export NAME="value"`. Restart the server after changing them.

| Setting | What |
|---|---|
| `FREEDIUM_BASE` | The mirror to use. Defaults to `https://freedium-mirror.cfd`; change it if that mirror goes down, e.g. `set FREEDIUM_BASE=https://freedium.cfd` |
| `PORT` | Server port. Defaults to `8765` |
| `HOST` | Listen address. Defaults to `0.0.0.0` for phone access on your network; use `127.0.0.1` for this computer only |
| `MEDIUM_LIBRARY_DATA` | Where PDFs, `library.json`, and the search index live |
| `FREEDIUM_MIRRORS` | Comma-separated fallback mirrors, tried after `FREEDIUM_BASE`. A nonempty list replaces the built-in fallback of `https://freedium.cfd` |
| `CHROMIUM_PATH` | Optional executable path to an existing Chromium browser; otherwise Playwright uses its installed browser |
| `NVD_API_KEY` | Optional key sent with NVD enrichment requests; requests work without it at the public allowance |
| `TGFS_PROJECT` | TGFS checkout used by the backup scripts; defaults to `~/Desktop/telegram-file-storage-system` |
| `TGFS_NEWSLETTER_PATH` | Remote archive root used by the backup scripts; defaults to `/newsletter-archive-2026-10-02` |

Indexing, curation, and topic/tag preferences are controlled in the interface. The sitemap index
defaults to a 365-day window for a new index.

### Pipeline tuning

These startup settings control processing capacity, without changing collection targets or the
indexing date window. Higher concurrency uses more memory and CPU; actual throughput also depends
on source latency and rate limits. The defaults are increased capacity limits, not a measured speedup.

| Environment variable | Default | Allowed range | Controls |
|---|---:|---:|---|
| `ARTICLE_WORKERS` | 4 | 1–16 | Simultaneous article renders (previously 2) |
| `IMAGE_WORKERS` | 12 | 1–32 | Image workers per active article (previously 6) |
| `CURATOR_PAGES_PER_CYCLE` | 10 | 1–50 | New post pages read per cycle (previously 5) |
| `CURATOR_ADDS_PER_CYCLE` | 6 | 1–50 | Maintenance additions/rotations per cycle (previously 3) |
| `CURATOR_CYCLE_SECONDS` | 60 | 1–3600 | Idle time after maintenance cycles (previously 120) |
| `CURATOR_FAST_CYCLE_SECONDS` | 1 | 0.1–60 | Idle time after filling cycles (previously 3) |
| `INDEX_REQUEST_GAP` | 0.25 | 0.1–60 | Extra idle time after each sitemap (previously 1.5 seconds) |
| `CVE_IMPORT_BATCH_SIZE` | 10000 | 100–50000 | CVE records per database commit (previously 5000) |

Worker counts and batch sizes must be integers; intervals accept decimals. Invalid values stop
startup with an error naming the setting. Restart the server to apply changes. On a machine with
limited memory, set `ARTICLE_WORKERS=2` and `IMAGE_WORKERS=6` to restore the previous download limits.

## Storage and backups

All paths below are relative to the active data directory:

```text
<MEDIUM_LIBRARY_DATA>/
  Medium-Library/
    library.json                 # topics, saved links, reading history, dismissals
    <topic>/<subtopic>/<title>/
      content.html
      article.pdf
      images/
  notes/<article-id>.json         # highlights, notes, summaries
  search-index/medium.db          # sitemap search index
  CVEs/
    cvelistV5.sqlite3             # complete CVE catalog
    cves.json                    # enriched CVEs saved to your shelf
```

When `MEDIUM_LIBRARY_DATA` is unset, `app.py` first honors `data-location.txt` in the project
directory, then defaults to the project directory itself. If that drive has less than 10 GB free
and `E:\storage` exists, it moves eligible data folders to `E:\storage\medium-library` and records
that location in the marker file. Set `MEDIUM_LIBRARY_DATA` explicitly to bypass this automatic choice.

For a manual backup, stop the server cleanly and copy all four data folders together. To restore
that backup, place them under your chosen data directory and point `MEDIUM_LIBRARY_DATA` there.
Keep the article IDs in `library.json` together with their note files.

The optional TGFS scripts use `E:\storage\newsletter-active` when `MEDIUM_LIBRARY_DATA` is unset;
they do not use the app's marker-file logic. Encryption follows the external TGFS configuration.
`restore_telegram.py` retrieves all four trees, so back up an existing local library before restoring
over it. `sync_telegram.py` compares file size and SHA-256, uploads new or changed files, and saves an
index snapshot when anything changes. It does **not** propagate local deletions to the archive.
All four local trees must exist for sync to complete.

To run these scripts manually, stop the app, set the same data directory used by the app, and use
the configured TGFS environment (PowerShell example):

```powershell
$env:MEDIUM_LIBRARY_DATA = "E:\storage\newsletter-active"
$env:TGFS_PROJECT = Join-Path $env:USERPROFILE "Desktop\telegram-file-storage-system"
& "$env:TGFS_PROJECT\.venv\Scripts\python.exe" sync_telegram.py
# Recovery only: downloads the archive into MEDIUM_LIBRARY_DATA.
# & "$env:TGFS_PROJECT\.venv\Scripts\python.exe" restore_telegram.py
```

Automatic sync depends on the launcher reaching its post-exit step. A crash, forced termination,
or closed terminal can prevent it. A failed upload leaves the local data intact; rerun sync after
resolving the TGFS error. Launching `app.py` directly does not restore or sync Telegram data.

## API

With the server running, open **http://127.0.0.1:8765/docs** for interactive FastAPI documentation
or `/openapi.json` for the schema. Use the configured port if different. The API has the same
unauthenticated access as the UI; write operations in the API explorer change the real library.

| Endpoint | Purpose |
|---|---|
| `GET /api/library` | Read the library and topic tree |
| `GET /api/search?q=...` | Search Medium links using the index and feed fallback |
| `GET /api/recommend` | Get personalized recommendations |
| `POST /api/articles` | Save an article link to a topic/subtopic |
| `POST /api/articles/{aid}/fetch` | Start article rendering/download |
| `GET /api/jobs/{jid}` | Check a download job |
| `GET /api/articles/{aid}/notes` | Read saved annotations |
| `PUT /api/articles/{aid}/notes` | Save annotations |
| `GET /api/notebook` | Collect notebook entries |
| `GET /api/index` | Inspect indexing and curator status |
| `GET /api/cves/catalog/status` | Inspect catalog import progress |
| `POST /api/cves/catalog/sync` | Start a CVE catalog update |

For example, in PowerShell:

```powershell
Invoke-RestMethod "http://127.0.0.1:8765/api/index"
Invoke-RestMethod "http://127.0.0.1:8765/api/search?q=vector%20database"
```

## Files

| Path | What |
|---|---|
| `app.py` | FastAPI server: library API, Discover, recommendations, job runner |
| `medium_render.py` | Checks each story on Medium: renders free stories from the post page, sends member-only ones (or refusals) to Freedium |
| `pipeline.py` | Playwright → HTML + PDF |
| `medium_index.py` | Sitemap crawler, SQLite full-text index, and the search itself |
| `relevance.py` | Query parsing and scoring, shared by search and recommendations |
| `curator.py` | Keeps each subtopic stocked with trending articles |
| `recommender.py` | Ranks posts for **For you** from what you've read |
| `polite.py` | One self-throttling HTTP client for every outside request |
| `performance.py` | Validated environment settings for worker counts, batches, and background cadence |
| `topics.py` | Default topic tree and Medium tags per subtopic |
| `cve_sources.py` | On-demand CVE enrichment from NVD, CVE.org/MITRE, GitHub, CISA KEV, OSV, Red Hat, Microsoft, and Cisco |
| `cve_catalog.py` | Full CVE List V5 baseline and hourly delta ingestion, SQLite search, canonical records |
| `static/` | The UI, Babel animation, web-app manifest, icons and service worker |
| `Medium-Library/` | Your articles, plus `library.json` |
| `CVEs/` | `cvelistV5.sqlite3` full catalog and `cves.json` enriched shelf records |
| `notes/` | Highlights, notes, and summaries, one file per article |
| `selftest.py` | Offline self-test of the library API, search index, renderer and curator |
| `run.bat` | Windows launcher for the existing E: drive and TGFS setup |
| `restore_telegram.py`, `sync_telegram.py` | Optional archive recovery and incremental uploads using the external TGFS environment |
| `bulk_add_from_index.py` | Maintenance script for adding links from the local index; inspect its configured paths before running |
| `PRODUCT.md` | Product direction and design context |

## Troubleshooting

| Symptom | What to check |
|---|---|
| `python` is missing or syntax fails at startup | Install Python 3.10+ and check `python --version`. Use the virtual environment's interpreter for all commands |
| `ModuleNotFoundError` at startup | Rerun the requirements installation with the same interpreter used to launch `app.py` |
| Chromium executable is missing | Run `.\.venv\Scripts\python.exe -m playwright install chromium`, or set `CHROMIUM_PATH` to a valid executable |
| `run.bat` says TGFS checkout is missing | Clone/configure TGFS separately and set `TGFS_PROJECT` to its checkout; it must contain `src/tgfs` and its own `.venv` |
| Telegram restore is incomplete | Confirm `TGFS_NEWSLETTER_PATH` points to the archive root containing all four data trees; the app will not start from a partial restore |
| `run.bat` asks before restore | The data directory already has files but is missing required data. Back it up or choose another `MEDIUM_LIBRARY_DATA` path before confirming |
| Port is already in use | Stop the other server, or set `PORT` to another number and open that port manually |
| Library appears empty after restarting | Check `MEDIUM_LIBRARY_DATA` and `data-location.txt`; different launch methods can choose different data roots |
| Search has few results | Check index progress and the indexing window. Let the crawler run; URL-derived titles may differ from the displayed story titles |
| Indexing is paused | Check the pause toggle, disk space, and rate-limit status in the index panel |
| Article download fails | Check the source URL and download error. Deleted stories and unavailable mirrors cannot always be recovered; wait for any rate-limit cooldown before retrying |
| Phone cannot connect | Set `HOST=0.0.0.0`, use the printed LAN address on the same network, and allow the server through the computer's firewall |
| Offline reading does not work on a phone | A plain HTTP LAN address cannot register the service worker. Use a secure origin and open the article while online first |
| Self-test reports missing `httpx` | Install the test dependency with `.\.venv\Scripts\python.exe -m pip install httpx` |

If `library.json` cannot be read, the app preserves a copy named `library.json.broken-<timestamp>`
and starts an empty library. Stop the server and inspect that copy or restore a backup before
continuing to add articles.

## Checking the app still works

Install the test client's additional dependency and run the self-test from the project directory:

```powershell
.\.venv\Scripts\python.exe -m pip install httpx
.\.venv\Scripts\python.exe -m unittest test_telegram_setup -v
.\.venv\Scripts\python.exe -m unittest test_performance -v
.\.venv\Scripts\python.exe selftest.py
```

Optional local loading benchmark (creates 520,000 synthetic entries in temporary storage and needs
Playwright Chromium):

```powershell
.\.venv\Scripts\python.exe benchmark_loading.py
```

On macOS/Linux, use `.venv/bin/python` in place of `.\.venv\Scripts\python.exe`.
`test_performance.py` checks tuning validation and a CVE import spanning a full batch plus a
partial batch, including searchable records. It uses temporary data and requires no network or browser.
`test_telegram_setup.py` checks TGFS paths and restored-file validation using temporary fixtures; it
does not load TGFS credentials or connect to Telegram.

`python selftest.py` runs the whole app against stubbed Medium pages and a stub Freedium mirror on
localhost. It never touches the network and never writes to your library — it works in a temporary
folder and prints a line per check.

It covers the library API, the search index, the curator, the storage layer, and both article
routes end to end: a free story rendered from Medium and a member-only one pulled through a mirror,
each all the way to a real PDF. It also checks what happens when things go wrong — a deleted story,
every mirror failing, Chromium being killed mid-session, twice the configured worker count in queued downloads, a damaged
`library.json` — and drives the interface at iPhone size in a real browser to check the drawer, the
reader and the tap targets. It installs the service worker, pulls the network, and checks the app
still opens and a saved article still reads; it feeds the API typos, other URL schemes, path
traversal and edited paging tokens; and it hands the renderer malformed post data to make sure one
odd page can't take an article down with it.

The PDF and interface checks need Chromium (`playwright install chromium`, or point `CHROMIUM_PATH`
at one you already have). Without it, those checks are skipped and the rest still run.

---

PDFs are saved for your personal offline reading. Please respect authors' rights and don't
redistribute them.
