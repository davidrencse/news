# Library of Babel: agent guidance

## Project

Python 3.10+ / FastAPI / SQLite FTS5 / Playwright, with plain JavaScript and CSS in
`static/`. There is no React, Node build, ORM, or external task queue. Read `README.md`
for setup and `PRODUCT.md` for UI intent. Keep changes compatible with this stack.

## Skills to use

Read the relevant `SKILL.md` before using it. Load only the skill and reference files
needed for the task; do not load this entire catalog for every edit.

| Task | Skill | Project application |
|---|---|---|
| Slow Python code, memory growth, throughput, caching | [python-performance-optimization](.agents/skills/python-performance-optimization/SKILL.md) | Profile library pagination, recommendation rebuilding, JSON persistence, image handling, and backup hashing before changing limits |
| Download jobs, background work, thread pools, shutdown, cancellation | [async-python-patterns](.agents/skills/async-python-patterns/SKILL.md) | Bound queued as well as running work; preserve the dedicated Playwright event loop and Windows subprocess support |
| Search/index latency, CVE imports, SQLite contention | [sql-optimization-patterns](.agents/skills/sql-optimization-patterns/SKILL.md) | Use SQLite `EXPLAIN QUERY PLAN`, FTS5-aware updates, and measured batching; the skill's PostgreSQL syntax and index types are not applicable here |

For UI design or rendering-performance work, use the installed `impeccable` skill
when available and preserve `PRODUCT.md`. It is user-installed, not bundled here.
Use `find-skills` only when a task reveals a missing capability. Avoid accumulating
overlapping design, generic workflow, or framework-specific skills.

Skill examples are starting points. Preserve Python 3.10 compatibility: do not adopt
`TaskGroup` or other newer APIs without an explicit version decision. This app polls;
prefer browser assertions on readiness, responses, and visible state over fixed sleeps
or relying solely on `networkidle`. Start with existing tests rather than introducing
a new test framework just because an upstream example uses one.

## Efficiency workflow

- Inspect `git status` and the relevant diff first; other work may already be in progress.
- Choose a representative workload and record a baseline. Distinguish code-inspection
  hypotheses from measured bottlenecks. Use synthetic or copied data for benchmarks.
- Measure cold and warm latency, throughput, peak memory, pending jobs, and source
  request counts as appropriate. More workers alone are not evidence of a speedup.
- Treat `ARTICLE_WORKERS * IMAGE_WORKERS` as a potential combined thread budget.
  Keep settings validated in `performance.py` and documented in `README.md`.
- Keep rate limits and server-requested cooldowns effective across concurrent requests.
  Avoid increasing source request rates to conceal CPU, disk, or queueing bottlenecks.
- Preserve article IDs, note associations, atomic writes, CVE raw records, ordered
  deltas, and FTS consistency. Benchmark indexing with one coordinated writer before
  introducing concurrent SQLite writers.
- Include cache invalidation and a changing library in performance tests; a warm,
  unchanged cache alone is insufficient. Bound cache growth and payload sizes.
- For a fix, reproduce the failure, add a targeted regression where useful, then run
  the affected integration checks. Report remaining failures and skipped browser checks.

## Data and execution

`app.py` creates storage and worker objects at import time. Set `MEDIUM_LIBRARY_DATA`
to a temporary directory before importing it in a test or benchmark. Stub external
requests before importing the app, following `selftest.py`. Starting the application
lifespan starts the crawler and curator.

`run.bat` is the existing E: drive/TGFS launcher; do not use it for automated tests.
Do not run restore, archive sync, bulk-add, or full catalog downloads as audit checks.
Keep personal data, logs, database copies, and profiles out of commits. Preserve
unrelated edits and do not stop the user's running server for tests.

## Validation commands

Run from the repository root with the project environment:

```powershell
.\.venv\Scripts\python.exe -m unittest test_performance -v
.\.venv\Scripts\python.exe selftest.py
git diff --check
```

The self-test needs `httpx`; PDF/browser checks need Playwright Chromium. Setup is
documented in the README. On macOS/Linux, use `.venv/bin/python`. These tests use
temporary data and local stubs. Run the full self-test for changes spanning the API,
storage, rendering, curation, or browser behavior; use narrower checks for isolated edits.

See [the efficiency audit](docs/efficiency-audit.md) for the dated findings and skill
provenance. Treat findings as a backlog to recheck against current code, not automatic
authorization to expand an unrelated task.
