# Efficiency audit and agent skills

Audit date: 2026-10-04. Scope: API/storage, rendering, rate limiting, curation,
search/recommendations, CVE catalog/enrichment, browser caching, and archive scripts.
This is a source audit with focused offline checks, not a production load benchmark.
The working tree contained ongoing pipeline and frontend changes. No application
behavior was changed as part of this audit.

## Findings, ordered by priority

### 1. High: source cooldowns are not fully respected

`polite.py:HostLimiter.pushed_back` caps an explicit `Retry-After` at `MAX_GAP=600`.
A local reproduction passed 1200 seconds and observed a 600-second recorded pause.
Separately, `HostLimiter.wait` reserves a slot and sleeps once; it does not recheck
a cooldown announced by another request while it was sleeping. Increased concurrency
makes this second case more relevant, though it was not reproduced in this audit.

Recommended fix: separate the adaptive-gap cap from server-requested cooldowns and
recheck shared deadlines before dispatch. Test with a fake clock and concurrent
waiters, including a cooldown longer than 600 seconds. Use `async-python-patterns`.

### 2. High: active render limits do not bound the pending queue

`app.py:fetch_article` creates a task for each distinct requested article and stores
it in `_tasks`. `pipeline.py:PdfPipeline._render` limits running renders with a
semaphore, but queued jobs are not capacity-limited. `prune_jobs` removes completed
jobs rather than providing admission control. A burst of distinct requests can
therefore keep adding pending tasks, job records, and retained article references.

Recommended fix: add a bounded pending queue/admission policy and explicit shutdown
handling. Preserve deduplication, retry behavior, and note/article consistency. Test
bursts larger than queue capacity, cancellation, failures, and graceful shutdown.
Use `async-python-patterns`; benchmark with `python-performance-optimization`.

### 3. Medium: CVE enrichment multiplies threads and downloads common feeds repeatedly

`cve_sources.py:enrich` creates an eight-worker pool for every import. `_kev` and
`_cisco` fetch the same full feeds for each CVE. Concurrent imports can multiply
worker counts and duplicate downloads. These requests use direct `urllib`, not
`polite.py`, so the Medium limiter does not bound this path.

Recommended fix: bound total enrichment concurrency, share feed caches with a TTL,
coalesce concurrent misses, and preserve per-source errors and freshness information.
Measure feed request counts and peak threads across several distinct CVEs. Use the
Python profiling and async skills. Merely increasing the pool above eight adds no
parallelism to a single import's eight source calls.

### 4. Medium: CVE delta FTS deletes scan the virtual table

`cve_catalog.py:_write_batch` executes `DELETE FROM records_fts WHERE id=?` for each
updated row outside a full rebuild. On a temporary catalog, SQLite reported:

```text
SCAN records_fts VIRTUAL TABLE INDEX 0:
```

This confirms the query-plan shape, not elapsed time on a full catalog. Repeating
the scan per updated record can dominate delta import cost; doubling commit batch
size does not remove that work.

Recommended investigation: benchmark representative deltas, then consider a stable
relational-rowid mapping for direct FTS row updates. Include a migration plan, repeat
imports, changed descriptions, deletions, and FTS consistency checks. Use
`sql-optimization-patterns`, translating examples to SQLite rather than PostgreSQL.

### 5. Medium: broad invalidation repeats whole-library work

`app.py:_library_page_index` copies every article and rebuilds scopes/counts whenever
`store.version` changes. `Store.flush` serializes the full JSON library. Both already
have useful caching/debouncing, but frequent writes can erase much of the benefit.
`recommender.py:profile` additionally checks a notes file for each legacy article
without `notes_count`, before deciding whether that article contributes engagement.

Recommended investigation: profile 10k/100k synthetic libraries with curation and
reading activity, not only warm unchanged pages. Consider more specific invalidation,
incremental counts, and one-time legacy metadata migration after measurements. Keep
read-history and recommendation correctness intact. Use `python-performance-optimization`.

### 6. Medium: worker increases lack a throughput/memory baseline

Defaults now allow four renders and twelve image workers per active article (up to
48 image workers). Configured maxima allow 16 by 32 (512 image workers). These are
capacity bounds, not measured CPU, memory, or throughput gains. Network pacing can
still be the limiting factor.

Recommended measurement: compare several worker settings using the same fixture
articles and images; record completed articles/minute, queue latency, peak process
memory, thread counts, and source requests. Prefer a shared image budget if multiplied
pools cause contention. Use the Python profiling and async skills.

### 7. Lower priority: backup cost scales with all local bytes

`sync_telegram.py:sync` hashes every file before deciding whether it changed and
uploads changed files sequentially. This favors correctness but makes unchanged
large libraries costly to scan. No live archive operation was run in this audit.

Recommended investigation: time hashing separately from upload on disposable data.
Consider a persisted fingerprint cache with explicit invalidation and periodic full
verification. Confirm external TGFS index/thread-safety contracts before parallelizing
uploads. Use `python-performance-optimization`.

## Skills selected

Installed in this repository under `.agents/skills/`; triggers and project-specific
constraints are in [AGENTS.md](../AGENTS.md). These are agent instructions, not app
runtime dependencies. They do not themselves fix the findings above.

| Skill | Source | Selection evidence at audit time |
|---|---|---|
| [python-performance-optimization](https://skills.sh/wshobson/agents/python-performance-optimization) | Community-maintained `wshobson/agents` | About 34.3k installs; profiling and memory analysis match the Python hot paths |
| [async-python-patterns](https://skills.sh/wshobson/agents/async-python-patterns) | Community-maintained `wshobson/agents` | About 17.1k installs; tasks, cancellation, and I/O concurrency match the job pipeline |
| [sql-optimization-patterns](https://skills.sh/wshobson/agents/sql-optimization-patterns) | Community-maintained `wshobson/agents` | About 19k installs; query-plan and batching methods apply, but PostgreSQL-specific examples do not |
| [webapp-testing](https://skills.sh/anthropics/skills/webapp-testing) | Anthropic's skills repository | About 169.8k installs; native Python Playwright matches the existing browser tests |

Repository metadata queried through GitHub reported 40,182 stars for `wshobson/agents`
and 179,547 for `anthropics/skills`. Counts are discovery signals, not correctness
guarantees. The skill manifests were inspected before installation. Detailed reference
examples and helper scripts require task-specific review before use.

Pinned source revisions:

- `wshobson/agents`: `156b7a5e7a8b93642628a339ee4039c925b34c7f`
- `anthropics/skills`: `8a1541c4a3ffa5a20a5a91de0dcf3f0bab1d1ef4`

The official [AGENTS.md documentation](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
defines the project instruction file. A project-level file keeps these constraints
scoped to this repository rather than changing unrelated Codex projects.

Existing `impeccable` remains useful for browser rendering/UX performance when relevant.
React/Next.js optimization skills, deployment skills, and additional design bundles
were not selected because they do not address this app's stack or the observed gaps.

## Validation

- Four `test_performance` tests passed, including a full CVE batch plus its tail.
- The long `Retry-After` reproduction and SQLite plan inspection used local objects
  and a temporary database, with no external requests.
- Full offline self-test passed, including PDF rendering, concurrent downloads,
  mobile UI, offline reading, and rate-limit checks. The earlier offline failures
  did not recur on the current working tree.

Suggested order: cooldown correctness and queue bounds; shared CVE enrichment and
FTS delta updates; profiling cache invalidation; then tune worker counts using evidence.
