# Setup guide — Library of Babel

This is the step-by-step guide to getting the app running on a machine. It covers two situations:

- **[Path A — Fresh install](#path-a--fresh-install-no-telegram)**: a brand-new library, no Telegram. Pick this to try the app, or on a machine that has no archived data.
- **[Path B — Restore your library from Telegram](#path-b--restore-your-library-from-telegram)**: you already have a library backed up in Telegram and want this machine to pull it down and keep it in sync. **This is the normal setup for your own data.**

If you just cloned this repo and want your articles back, you want **Path B**.

> **The one thing to understand first:** git stores **only the code**. Your actual library — articles, PDFs, notes, the search index, the research-paper shelf — is **never** in git (it's personal data and copyrighted articles). So a fresh `git clone` gives you a working *app* with an *empty* library. Your data comes back from your **Telegram backup** (Path B), not from git. See [How clone / pull works](#how-git-clone-and-pull-work) for the full picture.

---

## Before you start (both paths)

You need:

| Requirement | How to check | If missing |
|---|---|---|
| **Python 3.10 or newer** | `python --version` (try `python3` on macOS/Linux) | Install from [python.org](https://www.python.org/downloads/). On Windows, tick *"Add Python to PATH"*. |
| **git** | `git --version` | Install from [git-scm.com](https://git-scm.com/downloads). |
| **Internet access** | — | Needed for install, article downloads, indexing, research-paper lookups. |
| **Free disk space** | — | The library grows over time; a large library needs tens of GB. Indexing pauses automatically when its drive drops below 2 GB free. |

No Node.js, no build step, and no API keys are required. The frontend is plain JavaScript/CSS; article rendering uses a Chromium browser that Playwright installs for you in the steps below.

---

## Path A — Fresh install (no Telegram)

This gets the app running with an empty library. Run the commands **from the project folder** (where `app.py` lives).

### Windows (PowerShell)

```powershell
# 1. Create an isolated Python environment
python -m venv .venv

# 2. Install the app's dependencies into it
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. Download the Chromium browser used to render articles
.\.venv\Scripts\python.exe -m playwright install chromium

# 4. Choose where your library is stored, and keep the app on this computer only
$env:MEDIUM_LIBRARY_STORAGE = "local"
$env:MEDIUM_LIBRARY_DATA = Join-Path $env:LOCALAPPDATA "LibraryOfBabel"
$env:HOST = "127.0.0.1"

# 5. Start it
.\.venv\Scripts\python.exe app.py
```

### macOS / Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
export MEDIUM_LIBRARY_STORAGE=local
export MEDIUM_LIBRARY_DATA="$HOME/.local/share/library-of-babel"
export HOST=127.0.0.1
.venv/bin/python app.py
```

> If Chromium complains about missing system libraries on Linux, run:
> `.venv/bin/python -m playwright install --with-deps chromium`

### You're running when…

The terminal prints a line like `Library of Babel -> http://127.0.0.1:8765`. Open **http://127.0.0.1:8765** in a browser. Stop the server anytime with **Ctrl+C**.

**Every time you start it afterwards**, repeat the two environment lines (steps 4) and the start command (step 5) in the same terminal. Setting `MEDIUM_LIBRARY_DATA` explicitly keeps your library in a predictable place.

Jump to [First launch checklist](#first-launch-checklist).

---

## Path B ? Open your library directly from Telegram (default)

Configure the Telegram File Storage System checkout, its Python environment, Telegram session,
and encryption key. Then set the checkout and archive paths:

```powershell
$env:TGFS_PROJECT = "$env:USERPROFILE\Desktop\telegram-file-storage-system"
$env:TGFS_NEWSLETTER_PATH = "/newsletter-archive-2026-10-02"
$env:HOST = "127.0.0.1"
.\run.bat
```

The launcher creates the app environment if needed and reads the library from Telegram into RAM.
It does not restore files to disk or run a backup after exit. Changes go directly to Telegram;
failed saves show an error. Keep the app open and retry rather than discarding unsaved changes.

Telegram mode disables local search indexes, automatic curation, full catalog downloads, new PDF
generation, and browser offline caches. Existing PDFs remain readable; new articles are saved as
HTML without launching Chromium. Pages that require JavaScript may need the original website.
The TGFS login session and small file-location index still remain locally, along with app code.

Large libraries take time and RAM to load each launch. The current library includes both the
original `library.json` and its `remote-state.json` overrides. Never omit the latter from recovery.
See README's **Storage and backups** for the new behavior and legacy-data migration.

---

## First launch checklist

These legacy disk-based steps apply only to explicit local mode (Path A). In default Telegram
mode, use the reader, notes, and live feeds; no local index or offline copy is created.

1. Pick a topic and subtopic, paste a Medium article URL, and choose **Add & read**.
2. Wait for the download — it saves the article's HTML, images, and a PDF locally.
3. Select text to highlight; use **Notes** to annotate and **Notebook** to review later.
4. Try **Discover** or search. The search index builds in the background, so coverage grows over time.
5. Open the index panel (bottom of the sidebar) to set the indexing window, pause indexing, or turn off automatic curation.

Starting the server also starts the background search indexer and the curator, which make network requests on their own. Simply saving a link does **not** download the article until you open it.

---

## Reading on your phone

1. Start the app with `HOST=0.0.0.0` (`$env:HOST = "0.0.0.0"`). It prints a second address like `http://192.168.1.24:8765`.
2. Put your phone on the same Wi-Fi and open that address.
3. iPhone: Safari → **Share → Add to Home Screen**. Android: Chrome → **Install app**.

Offline caching needs a *secure* context (localhost on the computer, or HTTPS from another device). Plain HTTP over the LAN works for online reading but won't enable offline caching.

> **There is no login.** Anyone who can reach the server can read and change your library. Use `HOST=127.0.0.1` to keep it to your own computer, or only use `0.0.0.0` on a network you trust.

---

## How `git clone` and `pull` work

This is what makes "clone it anywhere and my files come back" true:

- **In git (shared):** all the code — `app.py`, `run.bat`, the Telegram scripts, the UI in `static/`, etc.
- **Not in git (ignored):** your library (`Medium-Library/`), notes, search index, research-paper shelf (`Papers/`), `data-location.txt`, and `.venv`. These are personal/large and listed in `.gitignore`.

So:

- **`git clone`** → you get a complete, working app with an empty library. Run **Path B** to restore your data from Telegram, or **Path A** to start fresh.
- **`git pull`** (updating an existing checkout) → updates the code only; your local library is untouched. If dependencies changed, refresh them:
  ```powershell
  .\.venv\Scripts\python.exe -m pip install -r requirements.txt
  .\.venv\Scripts\python.exe -m playwright install chromium
  ```
- **Your data never travels through git.** It travels through Telegram (Path B) or a manual folder copy (see README → *Storage and backups*).

> Because of this split, committing and pushing from one machine and cloning on another is safe — you won't accidentally publish your articles or credentials. If you ever see library files or a `.session`/key file show up in `git status`, stop and check `.gitignore` before committing.

---

## Verify your install

From the project folder, with the app's virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install httpx
.\.venv\Scripts\python.exe selftest.py
```

`selftest.py` runs the whole app against stubbed pages on localhost — it never touches the network or your real library, and prints a line per check. The PDF/browser checks need Chromium (installed in the setup steps); without it they're skipped and the rest still run.

TGFS-specific checks (paths and restore validation, no Telegram connection):

```powershell
.\.venv\Scripts\python.exe -m unittest test_telegram_setup -v
```

On macOS/Linux, use `.venv/bin/python` instead of `.\.venv\Scripts\python.exe`.

---

## Troubleshooting setup

| Problem | Fix |
|---|---|
| `python` not found | Install Python 3.10+ and make sure it's on your PATH (`python --version`). |
| `ModuleNotFoundError` on startup | You used the wrong interpreter. Always run with `.\.venv\Scripts\python.exe` (or `.venv/bin/python`), and re-run the `pip install` step with that same interpreter. |
| Chromium missing | `.\.venv\Scripts\python.exe -m playwright install chromium`, or set `CHROMIUM_PATH` to an existing Chromium. |
| `run.bat`: "TGFS checkout is missing" | Set `TGFS_PROJECT` to your TGFS folder; it must contain `src\tgfs` and its own `.venv`. |
| `run.bat`: restore is incomplete | `TGFS_NEWSLETTER_PATH` must point at the archive root holding all four trees (`Medium-Library`, `notes`, `CVEs`, `search-index`). The app won't start from a partial restore. |
| `run.bat` asks before restoring | Your data folder already has files but is incomplete. Back it up, or pick a different `MEDIUM_LIBRARY_DATA`, before confirming. |
| Port already in use | Stop the other server, or set `$env:PORT` to a free number. |
| Library empty after a restart | You likely started with a different `MEDIUM_LIBRARY_DATA`. Check that variable and `data-location.txt`. |
| Changes didn't sync to Telegram | Sync only runs after a **clean** app exit via `run.bat`. A crash or closed terminal skips it; just run `run.bat` again, or sync manually (README → *Storage and backups*). Launching `app.py` directly never syncs. |
| Phone can't connect | Use `HOST=0.0.0.0`, open the printed LAN address on the same Wi-Fi, and allow the app through your firewall. |

---

## Where to go next

- **[README.md](README.md)** — the full reference: every feature, all environment variables, the API, performance tuning, and the detailed storage/backup behavior.
- **Environment variables** — README → *Settings*.
- **Manual backup/restore without the launcher** — README → *Storage and backups*.
- **Keeping the app healthy** — README → *Checking the app still works*.

---

PDFs are saved for personal offline reading. Please respect authors' rights and don't redistribute them.
