---
name: security-review
description: Security review of the Medium Library app. Use when auditing this codebase for vulnerabilities, before exposing the server beyond localhost, before deploying it anywhere, or when changing anything that parses a fetched page, writes a file, or serves a request. Carries this app's threat model, the boundaries that matter, and the results already verified.
---

# Security review — Medium Library

A local FastAPI app that fetches third-party pages, renders them into HTML, prints them to PDF with a
headless browser, and serves the result back to a browser. It has **no authentication of any kind**.

Review against this app's real shape, not a generic checklist. Every finding must name a concrete
attack path; a missing hardening measure is not a finding.

## Threat model

Trust boundaries, in the order they matter:

1. **The network the server listens on.** `app.py` binds `0.0.0.0` by default so a phone can reach
   it. Every API route is unauthenticated. Anyone who can route a packet to the port is an admin.
2. **The web browser of anyone who visits any website while the app is running.** A local server
   with no auth and no Origin/Host checks is reachable from any page the user opens, given a way
   around the same-origin policy.
3. **Medium's post pages and the Freedium mirrors.** Attacker-influenced input: any Medium user
   controls the title, body, tags, image URLs and embedded JSON of a post the curator may fetch.
   Freedium is a third-party mirror the app renders in a real browser.
4. **`library.json`, `notes/`, `Medium-Library/`** — data the app wrote, but a damaged or edited file
   must not become code execution or a path escape.

Out of scope: the user's own machine, `run.bat`, and environment variables (trusted).

## Where untrusted data enters

| Entry point | Source | Ends up in |
|---|---|---|
| `medium_render.parse_post` / `meta_of` / `render_body` | a Medium post page | `content.html`, `library.json` |
| `pipeline.SANITIZE_JS` | a Freedium page, in a live browser | `content.html` |
| `pipeline.localize_images` | `<img src>` from either route | outbound HTTP, files on disk |
| `app.fetch_tag_feed` | Medium RSS, parsed with ElementTree | Discover list |
| `medium_index.crawl_day` | Medium sitemaps | the SQLite FTS index |
| `app.normalize_url` | the paste box, the search bar | everything downstream |
| `/api/*` bodies and path params | any client that can reach the port | the store, the filesystem |

## What must hold

- **`content.html` must never contain script or an event handler.** It is written to disk, served
  from `/files/` (where opening it directly is a page on the app's own origin), injected with
  `innerHTML` into the reader, *and* loaded into Chromium over `file://` by `pipeline._shell`. One
  escaping gap is stored XSS in three places at once.
- **Every attribute in generated HTML is `html.escape`d and every URL is scheme-checked**
  (`^https?://`) before it is written. `javascript:` must never survive.
- **Paths derived from article data stay inside `Medium-Library/`.** `slugify` is the only thing
  between a title and a filename; `notes_path` strips everything but `[0-9a-f]`.
- **SQL is parameterised, and FTS phrases are quoted** so a subtopic tag cannot become an operator.
- **Outbound fetches**: the host is the security-relevant part. A path-only injection is not SSRF; a
  *host*-controlled fetch is.

## Verified, with the test that pins it

Do not re-report these without a new attack path. Each has a check in `selftest.py`:

- Path traversal on `/api/articles/{id}/notes` and `/files/` — 404s across the board.
- FTS operators (`NEAR`, `*`, `AND`, quotes) in subtopic tags — quoted, inert.
- `medium_render.inline` UTF-16 markup offsets with emoji, overlapping spans, out-of-range
  offsets — correct; `javascript:` hrefs dropped; quotes escaped.
- `normalize_url` rejects other schemes and hostless input, strips credentials.
- Malformed post JSON (infinite claps, prehistoric dates, wrong types) cannot crash the renderer.
- A damaged `library.json` cannot stop the app starting.

## Reporting

Report HIGH and MEDIUM only, each with: file:line, category, the attack path in concrete steps, and
the fix. Skip DoS, resource exhaustion, rate limiting, dependency versions, and theoretical races.
"Local network only" is not a reason to downgrade — this app's whole exposure is local.
