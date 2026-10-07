# Self-hosted Freedium fallback — setup

The public Freedium mirrors the app shipped with (`freedium-mirror.cfd`,
`freedium.cfd`) keep going down, which is the paywalled-route half of the
[download diagnosis](download-diagnosis.md). This replaces them with a Freedium
instance running on this machine, so the fallback no longer depends on a mirror
staying alive.

## How it wires in

- The app tries Medium directly first for free stories. When Medium refuses
  (Cloudflare `403`) or the story is member-only, `remote_article_content`
  (`app.py`) and `pipeline.render` walk `FREEDIUM_MIRRORS` in order.
- `run.bat` now sets `FREEDIUM_BASE=http://localhost:6752` (only if you have not
  already set it), so the ordered list becomes:

  ```
  ['http://localhost:6752', 'https://freedium.cfd']
  ```

  The self-hosted instance is tried first; the public host stays as a last
  resort and fails fast when it is unreachable. Override `FREEDIUM_BASE`, or set
  a comma-separated `FREEDIUM_MIRRORS`, to change this.
- A Freedium page is parsed by the same readability path (`briefing.fetch_article`)
  the app already uses, so the self-hosted instance is a drop-in for the mirrors.

## One-time setup

The Freedium checkout lives on the big drive, at `E:\storage\freedium-web`
(cloned shallow from <https://codeberg.org/Freedium-cfd/web>), with its `.env`
already created from the template. Postgres data and Docker volumes stay under
that folder, off `C:`.

Start **Docker Desktop** first (the Linux engine must be running), then from the
Freedium checkout:

```bash
docker network create caddy_net
docker compose --profile local -f E:/storage/freedium-web/docker-compose/docker-compose.yml up -d
```

The `local` profile brings up Caddy (exposing `6752`), the Freedium web app, and
Postgres. It does **not** start Redis/Dragonfly or the WARP proxy — those are the
`prod` profile. First boot builds images and can take several minutes.

Check it serves:

```bash
curl -I http://localhost:6752/
```

Stop it later with:

```bash
docker compose --profile local -f E:/storage/freedium-web/docker-compose/docker-compose.yml down
```

## Important: a local instance uses your IP (confirmed blocked)

The `local` profile fetches Medium **from this machine's IP** — the same IP the
diagnosis saw getting Cloudflare-challenged. Standing up Freedium locally does
not by itself change the network origin.

Verified 2026-10-05 from this machine: the stack serves (homepage `200`, parses
the post ID, calls Medium's API), but every article returns Freedium's "Opppps.."
page with `MediumPostQueryError: No post data returned`, because a direct probe
of both the Medium post page and `medium.com/_/graphql` returns
`HTTP 403, server=cloudflare, cf-mitigated=challenge`. The block is at the
IP/edge, before auth — so a bare `local` instance cannot read articles. Pick one
of these to actually get past the block:

1. **Your Medium cookies (recommended if you have a paid account).** Edit
   `E:\storage\freedium-web\.env` and set `MEDIUM_AUTH_COOKIES` to your own
   logged-in `uid` and `sid` cookie values, then restart the stack. This is your
   credential — add it yourself; it is not committed and should stay out of git.
   An authenticated session is what lets Freedium read member-only stories and
   sidesteps the bot challenge.
2. **WARP egress.** Run the `prod` profile instead of `local` (it adds the
   `wgcf`/Cloudflare-WARP proxy and sets `PROXY_LIST`), which routes Medium
   fetches through a different IP. Heavier stack; only worth it if the cookie
   route is not an option.

Without one of these, expect the self-hosted mirror to fail on the same stories
Medium blocks today. That is a source-access limit, not an app bug.

## WARP result (2026-10-06): did not defeat the block

Brought up the WARP proxy with **no credentials**: `wgcf1` (Cloudflare WARP) +
`dante_1` (SOCKS on `wgcf1:1080`), fixed its CRLF `entry.sh` (exit 127 crash
loop), reached `warp=on`, set `PROXY_LIST=socks5://wgcf1:1080` in `.env`, and
recreated `freedium_web_mini` (confirmed the env and that `proxy_list` is passed
to `MediumApi`). Result: the same article still returns
`MediumPostQueryError: No post data returned`.

Changing the egress IP via WARP did **not** help, because Medium is serving a
Cloudflare **managed challenge** (`cf-mitigated: challenge`), not a plain
IP-reputation block. A managed challenge is cleared by a `cf_clearance` cookie
that only a solved browser challenge or an authenticated Medium session carries
— it is not about which IP you come from. The diagnosis already saw a fresh
headless Playwright get `403` from this IP for the same reason.

Bottom line: a self-hosted Freedium (whose whole method is server-side API
scraping) cannot currently read Medium articles without either (a) Medium
session cookies, or (b) a real, undetected browser that solves the challenge
from an IP Cloudflare accepts. WARP alone is not enough right now.

## After it is up

Restart the library (`run.bat`) so `app.py` picks up `FREEDIUM_BASE`. New
downloads of blocked/member-only stories will route through
`http://localhost:6752`. Per-source failure reasons already surface in the job
error (the diagnosis's reporting fix), so if a story still fails you will see
whether it was the direct `403`, the local mirror, or the public fallback.

## Security notes

- The checkout's `.env` keeps the template defaults (`ADMIN_SECRET_KEY="test"`,
  pgAdmin `root`/`root`). Those are fine while everything binds to localhost.
  Change them before exposing any of these ports beyond this machine.
- Keep `MEDIUM_AUTH_COOKIES` and the Postgres volume out of commits; they hold
  your session and cached content.
