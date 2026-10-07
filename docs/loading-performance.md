# Loading performance

The homepage fetches at most 60 articles for its initial screen. Its initial API stops
after finding that page and copies only those rows under the store lock. Counts and
index status load after the first rendering opportunity. Refreshing counts preserves
existing card elements rather than rebuilding the list.

The homepage no longer ships the reader's rendering libraries. KaTeX (~272 KB), its
stylesheet (~24 KB) and highlight.js (~129 KB) — about 425 KB that the library-browsing
view never uses — are fetched by `app.js` only when an article is first opened, then
memoized so later opens are instant. The article text is already on screen before they
arrive, so math and code enhance in place; a failed load degrades to plain text as before.
The service worker still precaches these libraries off the critical path, so an installed
phone keeps rendering saved articles offline, and the PDF printer embeds them in its own
template unchanged.

Text responses are gzipped at level 6 rather than level 1. For the same immutable files this
drops KaTeX from 91 KB to 76 KB on the wire, highlight.js from 51 KB to 44 KB and app.js from
38 KB to 31 KB; level 9 would save barely another 1%. The bottleneck here is the phone's link,
not the server CPU, and the browser compresses each immutable asset only once per cache lifetime.

Only the homepage-critical stylesheets (`style.css`, `reading-refresh.css` and `motion.css`)
block the first paint. `article.css` (reader) and `briefing.css` (briefing view) are preloaded
without blocking render and apply once fetched; neither view is shown until a click, so they are
ready in time, and they are still request-stamped for year-long caching. The PDF printer embeds
`article.css` in its own template, unaffected.

Saved article HTML appears as soon as its request completes. Notes load concurrently;
editing is enabled after they arrive, preserving existing notes. Saved articles skip
the 3D download intro. On the homepage, only its module is preloaded: scene building,
texture creation and shader compilation wait until an intro is played.

## Reproduce

Use the project virtual environment:

```powershell
.\.venv\Scripts\python.exe selftest.py
.\.venv\Scripts\python.exe benchmark_loading.py
```

The benchmark creates 520,000 synthetic entries in temporary storage, serves a saved
HTML fixture on a temporary localhost port, and opens desktop and mobile viewports in
Chromium. It does not read or change the personal library or contact article sources.
It prints API and browser timings and verifies the saved document can be read.

Observed on this Windows machine, October 4, 2026, after the changes:

| Operation | Local elapsed time |
| --- | ---: |
| First-page API | 13 ms |
| Cold library counts | 683 ms |
| Subsequent page API | 0.11 ms |
| Desktop first cards | 1.66 s |
| Mobile viewport first cards | 2.01 s |
| Desktop saved article | 0.96 s |
| Mobile viewport saved article | 1.02 s |

These are one-run diagnostics, not guarantees or an isolated percentage speedup.
Browser timings varied across runs; the mobile viewport still uses the desktop CPU.
The cold counts index remains proportional to library size and rebuilds after a store
version change. Newly imported links still need their first source download and PDF
generation; those depend on source latency and rate limits.

Regression checks in `selftest.py` verify bounded initial-page iteration, filtered
selection, stable snapshots, count/access cache invalidation, cards surviving metadata
refresh, and readable article text while the notes response is deliberately held pending.
The suite also checks offline startup, saved reading, and note persistence.
