# Download failure diagnosis — 2026-10-05

Scope: the existing app's source request, browser fallback, article extraction,
background download, remote save, and Telegram read path. This was targeted
debugging, not a certification of the whole application for public hosting.

## Confirmed live causes

- The running app on `127.0.0.1:8765` reported Telegram storage mode. Python
  reported no configured proxy schemes or proxy environment variables. The
  downloads already originate from the user's computer; temporary disk staging
  would not change their network origin. The public IP's residential classification
  was not independently verified.
- A control HTTPS request to `example.com` succeeded. DNS resolved Medium and
  `freedium-mirror.cfd`, but `freedium.cfd` failed with Windows error 11001.
  These are observations from this machine, not proof of a worldwide outage.
- A free-marked library article returned HTTP 403 from Medium with
  `Server: cloudflare`, `cf-mitigated: challenge`, and challenge markup.
  An independent fresh Playwright Chromium session also received HTTP 403,
  title `Attention Required! | Cloudflare`, and no article element.
  This confirms rejection of the tested automated clients, not a permanent IP
  ban or a finding about the user's signed-in browser session.
- The first configured mirror failed with Python TLS EOF and Chromium
  `net::ERR_CONNECTION_CLOSED`. The second mirror could not resolve. Neither
  reached article extraction successfully.
- Reading an existing 12,034-byte HTML document through the running app's
  `/files/` endpoint succeeded (HTTP 200). The remote reader validates the file
  length and SHA-256 after obtaining its chunks. No live write was made for this
  audit; passing a read does not establish current upload permissions or quota.

The earlier batch attempted five articles and stopped after five errors; one
additional free-marked article also failed. No new article was saved by those
attempts. Failures occurred before the upload stage.

## Code defects reproduced and fixed

1. `polite.get` passed non-ASCII paths directly into urllib. A Korean path
   raised `UnicodeEncodeError` before a usable HTTP request was sent. Paths and
   queries are now percent-encoded while preserving existing escapes and
   delimiters. This fixes the request construction, not Medium's refusal.
2. `remote_article_content` discarded every mirror exception and reported
   unreadable content. It now retains the direct-source reason and identifies
   each fallback host's DNS, TLS, HTTP, timeout, or extraction failure. Error
   summaries exclude full URLs, credentials, and response bodies.

The running server was not restarted. These code changes take effect on its
next normal restart; no restart can by itself repair the observed source blocks.

## Storage and execution observations

- Telegram mode disables `PdfPipeline` and uses a synchronous HTTP/extraction
  path in background threads. The old local mode uses Playwright for fallback
  rendering and PDF generation. A browser is not currently an automatic fallback
  in Telegram mode, and the live browser probe also failed at the source.
- Article content and images are held in memory and written to TGFS over a pipe.
  TGFS authentication, configuration, and its file-location index remain local.
- Jobs report completion after document upload and library-state flush. Tests
  verified that a source error never calls the remote writer, and that successful
  HTML, image, and metadata writes can be reopened using a fresh in-memory store.
- Individual image errors are currently caught and omitted by
  `save_remote_document`; a completed article does not guarantee every image was
  archived. This is an existing limitation, not the cause of the source failures.
- The TGFS FUSE adapter's `statfs` returns a hard-coded `2**50` bytes (1 PiB)
  free. That number is not a measured storage allowance. TGFS's Telegram client
  also implements FloodWait sleeping, so storage presentation is not an indication
  of unlimited request throughput.

## Validation

- Reproduced failing Unicode and lost-cause regressions before the fixes.
- 45 tests passed across `test_download_sources`, `test_remote_storage`,
  `test_performance`, and `test_briefing`.
- One additional document/image/metadata persistence regression passed.
- Full `selftest.py` passed, including browser, PDF, mobile UI, and offline tests.
- `git diff --check` passed (existing line-ending warnings only).
- Tests used temporary data, local source stubs, or fake Telegram storage.
  No restore, archive sync, bulk catalog download, or second TGFS writer ran.

## What actually unblocks downloads

The next useful access check is the same article in the user's ordinary browser.
If that browser can read it, a user-mediated import of accessible content is a
possible route. If it cannot, temporary storage or a browser rewrite will not
restore access. A working, authorized source must be established before resuming
the batch. Do not increase request concurrency to compensate for these failures.

A temporary-folder workflow remains possible: download into a bounded staging
area, upload and verify the content and recoverable index, then delete the staging
copy. The current memory-only path already avoids a persistent article cache.
