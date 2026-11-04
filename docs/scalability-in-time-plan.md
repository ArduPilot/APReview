# Scalability in time: bounding everything that grows

Status: draft 2, 2026-11-04, after a Codex review. Measured read-only on blu6. Companion to
`scalability-plan.md`, which bounded per-run *work*. This plan is about
*accumulation*: the box has to run for years, so every store needs a
retention rule, and every periodic job has to cost what current work costs,
not what all of history costs.

## Headline numbers (blu6, 2026-11-04)

- Root filesystem: 703 GB used, **253 GB free**. The store went from 302 GB
  to 380 GB in about 17 days of GC log, roughly **4.6 GB/day**. At that rate
  the disk fills in **about 55 days (late December 2026)**.
- `runs/` 295 GB, 5.96 M files. **268 GB of that is attempt `scratch/`**
  (build trees), and **254 GB is in 2020 attempts named by accepted
  claims**. GC never prunes them (`finished scratch` skips them as "named by a
  claim").
- `receipts/` holds 1.48 M files (49 GB). Compaction **is** keeping up. It
  moves about 3.2 k files an hour, which matches the creation rate of about
  70 k a day, but `RECEIPT_DAYS = 21` keeps three weeks of files by design.
  Every file older than 21 days is already in the ledger (the oldest file is
  dated 2026-10-14).
- `receipts.db` is 24.6 GB, 960 k rows, and grows by about 70 k rows a day
  (about 1.8 GB/day before slimming). About 57% of receipts are `publish`
  (56 KB each, carrying page copies) and 43% are `projection` (300 B). Almost
  all of them belong to **per-run operation journals** (generation = a 64-hex
  operation id), not to accepted generations.
- `run.json` files total **9.5 GB** (up to 60 MB each). GC parses every one
  of them each pass (`remember_references`).
- CLI state outside the store: `~/.claude/projects` 4.4 GB (about 3 GB/month),
  `~/review.auth/codex-work` 14 GB (sessions about 8 GB/month,
  `thread_history_1.sqlite` 2.9 GB). Nothing prunes it.
- `~/.cache/uv` 29 GB (still in use), `~/review/cache/uv` 5.8 GB,
  `~/review/pilots` 78 GB (the quality pilot of 2026-10-08, accepted
  2026-10-09).

## Inventory

Growth is per month unless noted. "Readers" lists what reads the data back,
which is what decides how much of it is safe to drop.

| # | Item | Where | Now | Growth | Readers | Proposed bound | Risk |
|---|---|---|---|---|---|---|---|
| 1 | Attempt scratch pinned by accepted claims | `runs/*/attempts/*/scratch` | 254 GB, 2020 dirs | ~+100-150 GB (new PRs × ~400 MB) | carry and promotion read job, status, result and cited files; later inference passes are *shown* earlier attempts' roots through `result_sources` and may look at anything there | prune non-cited scratch once the claim is accepted (`current.generation >= claim.generation`; the claim's `status` stays `active` after acceptance) and 2 days have passed, under the PR lock; cited files and `evidence/` stay | low to medium: needs an explicit contract that later passes rely only on results and cited evidence |
| 2 | Receipt files awaiting compaction | `receipts/*.json` | 1.48 M files, 49 GB | flat at 21 d × 70 k/day | `receipt_of`/`has_receipt`/`receipt_time` look at the file first, then the ledger | `RECEIPT_DAYS` 21 → 2 once slimming has landed; `receipt_time` already reads `written` from the ledger | low; the followup window reads the ledger time |
| 3 | Receipt ledger rows | `receipts.db` | 960 k rows, 24.6 GB | ~2.1 M rows; 1.8 GB/day now, ~0.1 GB/day after slimming | see the readers table below | **operation receipts**: delete after 30 days under the exclusions of step 8. **generation receipts**: keep (~75 k rows/month, small once slimmed); bounded later only by archiving long-closed PRs | medium: a dependency whose receipt has gone stops being "settled" |
| 4 | Page copies inside publish receipts | ledger and files | ~93% of ledger bytes | — | nothing | fix in progress (`Store.PAGE_COPY`, `review-slim-receipts.py`) | done separately |
| 5 | `run.json` | `runs/*/run.json` | 9.5 GB, 571 files | ~+6 GB | GC `remember_references` (every pass, all runs), resume of unfinished runs, `make-runs-page` fallback for old summaries, `review-baseline`, `review-control abort`, `review_handoff.py:69` | write `run-meta.json` (mode, created, reference_clones) at creation; GC reads that instead; strip `run.json` of runs whose `summary.json` says `complete` (not GC's `run_finished`, which also accepts a stale heartbeat) and older than 7 days down to the meta | low: completed runs are never resumed |
| 6 | Runs never expiring | `runs/*` | 571 dirs (410 `all`, 142 `followup`) | ~+450 dirs | dashboard, review-context, supervisor startup (`runs/*/run.json` glob, `summary.json` of each run) | the 90-day expiry is blocked while any attempt is pinned, and a closed PR's claim pins its last attempts for ever. Let expiry keep only the pinned attempt dirs (thinned to PROOFS + result), and unpin claims of dormant PRs (#9) | low |
| 7 | Per-run `summary.json`/`state.json` | `runs/*/` | 252 MB / 295 MB | ~+200 MB each | dashboard every 10 min (all summaries + all attempt status files); `recover_controllers` reads each run's summary at every controller start | dashboard filters by run-name date before reading; `recover_controllers` skips runs with a `complete` marker file (tiny) or older than N days | low |
| 8 | Generation evidence | `results/*/*/*/generations/*/evidence` | 11.3 GB | ~+9 GB (53 generations/day × 5.7 MB) | `bundle_digest` (on the first `bundle()` read of each generation by each `Store` instance, so in every drain process), renderer (retained pages), promotion | prune evidence of superseded generations after 30 days once the digest covers a manifest, not file bytes (see step 12) | medium: changes the bundle-verification contract |
| 8a | Generation bundles and retained pages | `generations/*/bundle.json`, `pages/<digest>/N.html` | 0.66 GB bundles; one retained page per generation | ~+0.5 GB bundles, ~+1500 retained pages | chain walks, renderer, comments (link to the retained page) | proportional to generations reviewed, not to passes; long term, archive (compress) the results of PRs closed over a year | low; this is the residual growth after this plan |
| 9 | Closed or dormant PRs walked for ever | `results/` (648 PRs, 1964 generations) | 12 GB | ~+300 PRs, +1500 generations | recovery revisits every PR every 6 h (`RECOVERY_REVISIT`): chain walk, `bundle_digest` of every generation in each new drain process, `materialize` of every intent of every generation; `snapshot()` enumerates every claim; `recover()` reads and rewrites the whole `recovered.json` each call; `pinned_attempts` | "dormant" PR (strict conditions, step 9): weekly revisit instead of 6-hourly, and an index of recovery candidates so `snapshot()` lists active PRs only | medium: the periodic revisit is what repairs a lost outbox entry |
| 9a | Chain walks for active PRs | `Store.chain` callers: `inherited`, `_promote`, `rebuild`, `_merge_membership` (generation patch), `bundle_at` | O(generations of the PR) bundle parses per call | grows with PR age (max 25 generations now) | — | per-PR chain index: generation list, accepted times, the inherited destination set, and a `settled_through` watermark below which rebuild skips; `bundle_at` opens the generation directory directly | medium: the index must be rebuilt from the chain if damaged |
| 10 | Inherited destinations per generation | bundle `intents` | median 18, max 95 intents (#33955: 40 publishes, 26 annotations, 26 projections) | grows with the PR's age: each generation re-publishes every dated and followup page the PR was ever on | drain renders and uploads each of those pages; receipts per generation | freeze dated pages after their call date and followup pages after their run: `inherited()`/`_promote` skip them (existing plan §7) | medium: an archive page no longer gains later generations, which is the intent |
| 11 | Operation receipt *rate* | projection journals | ~20 k journalled projections/day | constant | — | not a retention problem; already reduced by "merged unchanged" (~40 k/day). Optional: a followup page per day rather than per run | product choice |
| 12 | Page store | `pages/<digest>/` | 2537 dirs, 1.3 GB | ~+2000 dirs, ~+1 GB | drain (render, confirm), `examine` (epoch), dashboards | after freezing (#10), drop the local html of frozen pages older than 30 days and keep the small records | low once pages are frozen |
| 13 | Membership | `membership/*.json` | 718 files, 64 MB, 120 k rows (1660 tombstones) | ~+700 files, ~+60 MB | every projection merge rewrites the whole file; renderer; `inherited`/`_promote` read `unreachable` rows | only after a durable freeze record exists for the page (step 11): drop membership of frozen pages; never drop `unreachable` rows (they stop inheritance restoring a retired page); prune `removed` tombstones only below every queued and journalled ticket | medium (ticket fencing, journals and chain recovery can recreate rows) |
| 14 | Remote published tree | firmware.ardupilot.org `DevCallReviews/followups/*`, dated pages, `PRReviews/*` | ~24 followup pages/day of 0.5-2.7 MB | ~+1 GB remote | people following links | policy decision: keep followup pages 30-90 days; keep dated and retained pages (comments link to them) | link rot for followup pages |
| 15 | GC per-pass cost | `review-gc.py` | `--stats` walks 7.7 M files hourly; `remember_references` parses 9.5 GB of JSON; `compact_receipts` stats 1.5 M entries | grows with everything | — | stats daily (or reused from the last walk); references from `run-meta.json`; compaction window as in #2 | none |
| 16 | GC fence availability | `review-gc.py` | fenced rules skipped in ~10% of passes (25 of 237 recent) | — | — | fine while compaction has headroom; keep `limit=100000` | none |
| 17 | Landing full scan | `review_delivery.py` landing render | full `Store.receipts()` scan (all files + whole ledger) the first time each new date's landing renders before its `.complete` marker exists | ~once a day, cost ∝ all receipts ever | — | one-off backfill of every date any receipt names, with a cutover marker; dates first indexed after the cutover are complete by construction; then remove the scan (step 6) | low once migrated |
| 18 | Renderer memory and time | `Renderer.body` | ~500 MB RSS for a 300-PR page; render 0.25 s (Oct 5) → 0.5 s (Nov 4) per call | ∝ PRs on the page × bundle size (bundles median 221 KB, max 2.4 MB) | drain, supervisor `examine` | stream: per PR, load the bundle, render its section and views, drop it; optionally cache rendered sections by (pr, generation, annotation digest), with the cache entry dropped when no membership row shows that generation | low |
| 19 | Claude transcripts | `~/.claude/projects`, `~/review.auth/claude-personal/projects` | 4.4 GB + 0.7 GB, ~13 k files | ~3 GB, ~8 k files | `review-context` (3 days), `make-runs-page` (globs every transcript every 10 min, then filters by mtime) | delete transcripts older than 30 days that no live process has open; set `cleanupPeriodDays` in the role config dirs | low: dashboards look back 10 days at most |
| 20 | Codex sessions and state | `~/review.auth/codex-work/sessions`, `thread_history_1.sqlite`, `~/.codex` | 14 GB + 5.7 GB | ~8 GB | `review_context.py` (globs every date dir for one thread id), `make-runs-page` `read_quota` | delete `sessions/YYYY/MM/DD` older than 30 days; find out whether Codex can bound `thread_history` (else compact it offline when no Codex is running) | low for sessions; unknown for the sqlite |
| 21 | Package caches | `~/.cache/uv` 29 GB, `review/cache/uv` 5.8 GB, `pip` 1.4+2.0 GB, `~/.npm` 2.8 GB | 41 GB | unbounded | builds | weekly `uv cache prune` and `pip cache purge` at a size threshold when no pass is running; find what still uses `~/.cache/uv` | low |
| 22 | One-off leftovers | `~/review/pilots` 78 GB, the contents of `~/review/work` 38 MB (the directory itself is run-reviewprs.sh's working directory and must exist), old `logs/*` dirs (~70 MB) | 78 GB | 0 | none | delete after tridge confirms | none |
| 23 | Logs and metrics | `review/logs/*.jsonl`, `*.log`, `reviewprs-*.log` (894), `data/metrics/*.jsonl` | 81 MB; metrics 5.5 MB | ~+10 MB; ~900 wrapper logs/month | `quota.py` reads all of `quota.jsonl` (`readlines`); `make-runs-page` reads every wrapper log before filtering | monthly rollover of the jsonl files; read only the tail; filter wrapper logs by mtime before opening; metrics kept 180 days | none |
| 24 | Reference clones | `data/references/*` | 13 GB; packs: wiki 117, MethodicConfigurator 142 | slow | worktree creation | `git maintenance`/`gc --auto` in the nightly refresh; GC already prunes worktrees | none |
| 25 | Outbox given-up entries | `outbox/` | 5 entries now | grows only with failures | every drain pass and controller save reads every entry; `_coalesce` re-globs the outbox for each publish | leave them in the outbox (moving them would make `materialize` recreate them with fresh retry state and lose uncertain-write records); keep a small in-memory or on-disk index of due entries so scans skip them | low |
| 26 | Landing indexes | `landing/<endpoint>/<date>/` | 41 dirs, 1 MB | ~+30 small dirs | landing renders | tiny; no rule needed for years | none |
| 27 | Bounded already | `http-cache` (14 d), `venvs` (14 d), `tmp`/`scratch` (7 d), `operations` (unlinked when receipted), `owners` (fixed stripe table), `locks` (fixed regions), ccache (20 GB cap) | — | — | — | no change | — |

### Who reads receipts (constraints on #3)

| Reader | Needs | Consequence of a missing receipt |
|---|---|---|
| `Store.materialize` (rebuild of every chain intent; journal fan-out) | existence | the intent is re-queued: a comment re-posts (reconciliation and the marker check guard it), a publish re-renders |
| `_recover_one` for journals | existence of every intent's receipt | the journal is fanned out again rather than unlinked |
| `delivery_snapshot` `ok(dep)`, `settled()` | state of dependencies | the dependent waits for ever unless its dependency is queued |
| `Supervisor.intents` (discovery projection dependency, same run) | existence | the dependency is dropped (safe) |
| `Store.inherited`/`may_settle` | existence | the inherited dependency is dropped (safe, by design) |
| `generation_receipts` (controller save) | publish, comment and board receipts of the current generation | state shows "owed" |
| `comment_receipts` (renderer), `in_followup_window`, `last_worked` | comment receipts of the *displayed* generation (a row can show an older one); `written` time | page loses the comment link; PR may leave the followup window early |
| `claim.prepared` recipes promoted by recovery (`recover_pr`) | dependencies on discovery-operation projections | a prepared claim promoted after its journal was unlinked waits on a receipt that has gone |
| `Supervisor._project` on resume | absence of the journal file | an unfinished run that resumes rebuilds a journal whose fan-out already happened; only the receipts stop it re-delivering |
| landing render (`review_delivery.py`) | dated label publish receipts (only without `.complete`) | label missing from the landing page |
| `review-retry.py`, `make-runs-page` | outbox and recent receipts | display only |

So **operation receipts** (generation is an operation digest: the per-run
projection and publish journals, including landing publishes) are
needed only while their journal exists, while something queued depends on
them, and in the run that wrote them. **Generation receipts** (integer
generation) are needed for as long as rebuild walks the chain, and comment
receipts for as long as the PR is shown or followed up.

## Ordered steps

Each step is one small change with its own check, in a separate commit per
subsystem. The disk deadline sets the order: steps 1-5 have to land well
before December.

**Coordination rule for every deleting step.** GC's `maintenance` fence
stops controllers but not the cron drain (`review-drain.py` takes only
`drain.lock`, then recovers and delivers). Any rule that deletes delivery
state (steps 8, 9, 11, 12, 14) must also hold `drain.lock` for its deletion
window, or recheck each item under the page or PR lock the drain itself
takes. Exclusion sets are built after those locks are held, never before.

### Step 1: release scratch pinned by accepted claims (≈250 GB)

*Status 2026-11-05:* implemented (397abdf), Codex reviewed twice; owner
records of any kind, not only the PR region, still pin.

`review-gc.py` `finished_scratch`: an attempt named only by a claim whose
generation is accepted (`current.generation >= claim.generation`) and that
finished over `PINNED_SCRATCH_DAYS = 2` days ago loses its `scratch_waste()`,
as unpinned attempts do. The claim's `status` stays `active` after
acceptance, so the test is reachability of the generation, not the status
field. A claim whose generation is not yet current (active, prepared,
deferred) keeps everything. The rule keeps the PR lock and the re-read of the
claim. `evidence/` and the cited files stay.
First make the contract explicit in `review_inputs.py`: an earlier attempt
reached through `result_sources` provides its result and cited evidence; its
scratch may be gone. A carried pass with a reconciliation rerun later then
sees what a fresh claim would.
*Check:* dry run on blu6 reports about 250 GB; a test covers an accepted
claim losing scratch while an unpromoted one keeps it; a later claim with the
same review key still promotes (`carryable` and `_promote` read job, status,
result and cited files only).

### Step 2: land receipt slimming, then shorten the file window

*Status 2026-11-05:* `PAGE_COPY` landed (34269f3); the script holds the
collector lock, so only GC pauses and the drain keeps running. Running on
blu6.

Finish the in-progress `PAGE_COPY` change and run `review-slim-receipts.py`
with GC paused, and
`--vacuum` with free space about equal to the slimmed
ledger. Do it before the disk gets tight. Then `RECEIPT_DAYS` 21 → 2:
`receipt_of`, `has_receipt` and `receipt_time` already fall back to the
ledger, and `compact_receipts` copies the file's mtime into `written`.
*Check:* ledger bytes per 10 k rows before and after; the `receipts/` count
drops to ~150 k within a day; the followup-window test using a compacted
receipt passes.

### Step 3: `run-meta.json` and run.json thinning (9.5 GB; GC CPU)

The supervisor writes `run-meta.json` (mode, created,
`configuration.reference_clones`) next to `run.json`. GC
`remember_references` reads meta files, falling back to `run.json` only when
no meta exists (a backfill writes meta for existing runs). A new GC rule
replaces the `run.json` of a run whose `summary.json` state is `complete`
and is older than 7 days with its meta. It does not use `run_finished()`,
which also accepts a stale heartbeat on a run that may still need recovery.
Readers to update or confirm: `make-runs-page.run_meta`, `review-baseline`,
`review_handoff.py:69`, `review-control` abort (only on unfinished runs).
*Check:* GC pass time before and after; `review-resume.py` refuses a stripped
run cleanly; the reference-clone pruning test still passes.

### Step 4: CLI transcript and session retention (~11 GB/month)

A GC rule (outside the store fence, like `finished_scratch`) deletes Claude
`projects/**.jsonl` and Codex `sessions/YYYY/MM/DD/*.jsonl` older than
`SESSION_DAYS = 30` under each real directory in `REVIEW_AUTH` (resolved
once, deduplicated), skipping files that a process has open (`in_use()`) and
any transcript an attempt in a not-yet-complete run names. Set
`cleanupPeriodDays` in each Claude role directory's `settings.json` as a
second line of defence. Find out separately whether Codex's
`thread_history_1.sqlite` (2.9 GB) can be bounded by configuration.
*Check:* dry-run list of byte counts; `review-context` and the runs page
still find the last 10 days.

### Step 5: operator clean-up (≈110 GB, no code)

*Status 2026-11-05:* `~/review/pilots`, `~/review/work` and `~/.cache/uv`
deleted with tridge's agreement (100 GB freed; uv cache was 22 GB at
removal). `~/review/work` had to be recreated empty: run-reviewprs.sh
refuses to start without it, which lost the 06:13 all run. Still to do: what writes `~/.cache/uv`, and the weekly prune.

Confirm, then delete `~/review/pilots`, `~/review/work` and the retired
`logs/*` directories. Find what still writes `~/.cache/uv` (likely a pass
without `UV_CACHE_DIR`) and point it at `review/cache/uv`; add a weekly
`uv cache prune` when no pass is live.
*Check:* `df`; the next `all` run builds normally.

### Step 6: landing index migration, then no full scan

One-off, with the drain paused: scan all receipts once, backfill a landing
index for **every** date any dated label receipt names (including dates
with no index directory yet) and mark each complete. Record a cutover time
in `landing/<endpoint>/.migrated`. After that, a date directory first created
after the cutover is complete by construction, since every label upload since
then writes its index entry under its page lock. The fallback scan then goes,
and `Store.receipts()` with it.
This must land before step 8 deletes any landing publish receipt.
*Check:* a test where a new date's landing renders with no scan; a test that
a pre-cutover date with no index directory is still backfilled by the
migration; `render landing` time in metrics.

### Step 7: dashboard and periodic job scans

- `review_dashboard.summaries`: skip runs by the date in their name only
  when their `summary.json` is complete; live and unfinished old runs are
  always read.
- `review-context.py`: it selects by attempt *finish* time, so a recently
  finished attempt in an old run must stay eligible: keep a small index of
  attempts finished in the window (written by the guardian at terminal
  status) instead of globbing every `status.json`.
- `make-runs-page`: open wrapper logs and CLI transcripts only after an
  mtime filter; `read_quota` lists only date directories in the window.
- `quota.py`: read the last N KB of `quota.jsonl`.
- `recover_controllers`: a tiny `complete` marker written with the final
  summary lets it skip completed runs without parsing each 440 KB summary;
  runs without the marker are examined as now, whatever their age.
- GC `--stats`: once a day.

*Check:* wall time of `publish-runs-page.sh` and of a controller start,
before and after.

### Step 8: operation-receipt retention (bounds the ledger)

Add an indexed classification column at insert (`op INTEGER`: 1 when
`generation` is a string) with a one-off backfill, and an index on
`(op, written)`. A GC rule, under the coordination rule above, deletes in
batches of 10 k within its budget the operation receipts with `written`
older than `OP_RECEIPT_DAYS = 30` whose id is none of:

- the id of any outbox entry, or a dependency of one;
- the id of any intent in any `operations/*.json` journal, or a dependency
  of one;
- a dependency of any intent in a claim's `prepared` recipe whose
  generation is not yet current;
- a dependency of an unreceipted intent of any PR's chain (the bundle
  projection intents made by `Supervisor.intents` depend on the discovery
  operation's projection).

And it deletes nothing while any run older than `OP_RECEIPT_DAYS` is not
`complete`: a resumed run can rebuild a journal its first life already fanned
out, and then only the receipts stop a re-delivery. Landing publishes are
operation receipts too; step 6 must have landed first.
SQLite reuses freed pages, so the file stops growing without a VACUUM.
*Check:* tests for each exclusion (an outbox dependant, a journal still
present, a prepared claim promoted after its journal was unlinked, a resumed
old run); a test that deletion cannot recreate an outbox entry; dry-run
counts on blu6 before enabling.

### Step 9: dormant PRs and a recovery index

A PR is dormant only when: its current generation equals its claim's
generation (no newer claim, unfinished or deferred); every intent in its
chain has a receipt (checked, not inferred from an empty outbox); its owner
record has cleanup proofs; no `pending.json`; and `last_worked` is over 30
days ago. Dormant PRs are revisited weekly rather than every 6 h, keeping the
lost-outbox repair guarantee at a slower rate, and are revisited at once when
their claim, current or owner mtimes change. Keep an index of non-dormant
PRs so `snapshot()` and `recover()` walk those, and keep `recovered.json`
entries only for them (split per PR or per stripe so a call rewrites only
what it touched). A dormant PR's claim stops pinning its attempts once they
have been thinned as in step 10. A returning PR allocates a new claim;
`carryable` returns False for a missing attempt, so its passes run again.
*Check:* recovery-slice time and PRs visited per day; tests for a PR with an
accepted current and a newer active claim (not dormant), a lost outbox entry
on a dormant PR (repaired within a week), and a returning dormant PR.

### Step 10: runs expire around pinned attempts

The 90-day `expired run` rule removes everything in the run except the
attempt directories still pinned, which keep PROOFS, METRICS, the result file
and every file the result cites (promotion of a carried pass copies those
from the original attempt). Old-run thinning (30 days) also drops
`state.json`. Runs holding pinned attempts of deferred claims stay until the
claim moves on or the PR goes dormant, so the run count is bounded by
current work plus those.
*Check:* the run count levels off at about 90 days of runs plus the pinned
remainder; a carried pass from a thinned old run still promotes.

### Step 11: freeze dated and followup pages

At promotion, `Store.inherited` and `_promote` stop carrying publish,
projection and annotation intents for `DevCallReviews/YYYY_MM_DD/...` pages
whose date is before the generation's run date, and for any
`DevCallReviews/followups/...` page of an earlier run. The filter applies
**before** comment dependencies are built from the publication list, so a new
comment never waits on a frozen page. Label and author pages and retained
pages are unchanged. Already promoted bundles keep their intents
(immutable), with their receipts and any debt they still owe: a frozen page
can still be published by entries queued before the freeze. A page counts
as frozen for deletion purposes (step 14) only once a durable
`pages/<digest>/frozen.json` exists and nothing queued, journalled or
unreceipted in any chain targets it.
*Check:* intents per new bundle no longer grow with PR age; publishes per
generation in metrics; a test that a new generation does not queue a publish
to yesterday's dated page and that its comment does not depend on one.

### Step 12: generation evidence retention, chain index

A per-PR chain index (generation numbers, explicit `accepted_at`, the
inherited destination set, and a `settled_through` watermark below which
rebuild need not materialize) removes the O(generations) walks from
`inherited`, `_promote`, `rebuild`, `_merge_membership` and `bundle_at`; it is
rebuilt from the chain when missing or inconsistent. `last_worked` moves to
`accepted_at` before anything else touches generation directories, since it
reads the newest generation directory's mtime today.
Then evidence retention: the generation pointer's digest covers
`bundle.json` plus an evidence manifest (path, size, sha256) written at
promotion; verification checks the manifest and, for each file, the bytes or
a `pruned.json` marker. Migration is crash-safe and handles successors,
whose bundles embed their predecessor's pointer: either keep the old digest
in place and add the manifest beside it as a second, verified record, or
migrate a PR's whole chain under its lock, newest last. GC then prunes
evidence of non-current generations older than 30 days. Routine reads may
skip rehashing only for files whose (inode, size, mtime_ns) match a
verification recorded earlier; a full rehash still runs weekly.
*Check:* recovery CPU per day; `results/` growth per month; tests for
corruption detected after restart and for a crash at each migration point.

### Step 13: renderer streaming

`Renderer.body` builds the contents table and sections in one pass per PR,
dropping each bundle after use. If a section cache is added (keyed by pr,
generation and annotation digest), its entries go when no membership row
shows that generation.
*Check:* drain peak RSS (target < 150 MB on a 300-PR page); render time per
page in metrics; output byte-identical to today's on fixtures.

### Step 14: membership and page-store pruning, logs

For pages with `frozen.json` (step 11) and no queued, journalled or
unreceipted work: after 30 days drop the local html and the membership file,
keeping `epoch.json`, `revision.json`, `confirmed.json` and the freeze
record. Never drop `unreachable` rows of live pages; prune `removed`
tombstones only below every queued and journalled ticket for the page.
Monthly log rollover; metrics kept 180 days. Given-up outbox entries stay
where they are.
*Check:* the membership file count levels off; projection merge time; a
test that a recovered chain cannot recreate a pruned frozen page's
membership.

### Step 15: remote retention policy

Decided 2026-11-05 (tridge): followup pages on firmware.ardupilot.org may
expire, after at least 30 days.
Retained `PRReviews` pages and dated call pages stay, since comments and call
notes link to them.

## Expected effect

| After step | Store growth |
|---|---|
| now | ~4.6 GB/day; disk full in ~55 days |
| 1-5 | ~250 GB + 110 GB freed at once; growth ~0.5-1 GB/day (results evidence, pages, ledger after slimming) |
| 8-12 | ledger, runs and evidence level off; per-pass recovery and chain costs track active PRs |
| residual | bundles (~0.5 GB/month), retained pages and generation receipts still grow with the number of generations reviewed, and one result directory per PR ever reviewed remains. That is proportional to useful work and fine for years; archiving PRs closed over a year would bound it |

## Codex review

Codex (codex-cli 0.166.0, read-only) reviewed draft 1 against the code. Its
findings, checked against the code, and what changed:

1. **Residual growth understated.** Bundles, generation receipts and
   per-generation retained pages still grow with generations, and "results
   grow only with current generations" was wrong. Added #8a and a residual
   row to the expected effect; generation receipts are now explicitly kept.
2. **Chain walks and recovery enumeration not bounded by dormancy.**
   `snapshot()` lists every claim, `recover()` rewrites all of
   `recovered.json`, and active PRs walk their whole chain in several places.
   Added #9a, a chain index (step 12) and a recovery index (step 9).
3. **Step 1's predicate was wrong.** A claim stays `status: active` after
   acceptance (`review_store.py:396`, only deferral changes it at
   `review-supervisor.py:517`). It now tests `current.generation >=
   claim.generation`.
4. **Step 1's "only cited files are needed" was not established.**
   `result_sources` shows later passes the earlier attempt's root
   (`review_inputs.py:289`). No code reads non-cited scratch, but the contract
   is implicit; step 1 now writes it into the inputs first.
5. **Step 2 omitted the slimming tool's own precondition** (pause drain and
   GC; VACUUM needs no other writer). Added.
6. **"Finished" is not "complete".** `run_finished()` accepts a stale
   heartbeat (`review-gc.py:218`); steps 3 and 7 now require a `complete`
   summary, keep unfinished old runs visible, and keep review-context's
   finish-time semantics. Added the `review_handoff.py:69` reader.
7. **Step 6 needed a migration boundary.** An absent landing directory
   does not prove there are no receipts for that date. It is now a one-off
   backfill of every date with a cutover marker, and a prerequisite of step 8.
8. **Step 7's (now step 8) exclusions were insufficient.** Added outbox
   entries' own ids, every journal intent id, dependencies in unpromoted
   `prepared` recipes, and a guard against unfinished old runs re-journalling.
9. **GC's fence does not exclude the drain.** Added the coordination rule
   at the top of the steps.
10. **Dormancy could catch unfinished work.** Conditions tightened (no newer
    claim, receipt closure checked, cleanup proofs), and dormant PRs are
    revisited weekly rather than never, keeping the lost-outbox repair.
11. **Step 9 (now 10) would have removed evidence carried passes still
    copy.** Pinned attempts keep their cited files.
12. **Freezing must filter before comment dependencies are built** and keep
    existing debts; a durable freeze record now gates any deletion.
13. **Evidence migration must be crash-safe across successors**, a cached
    verification keyed on manifest mtime would miss corruption, and
    `last_worked` reads a directory mtime that migration would change. Step 12
    now moves `last_worked` to `accepted_at` first, keys skipped rehashes on
    each file's (inode, size, mtime_ns) with a weekly full check, and
    migrates whole chains or adds the manifest beside the old digest.
14. **Moving given-up entries to `held/` was unsafe** (`materialize` would
    recreate them with fresh retry state) and pruning `unreachable` rows would
    let inheritance restore retired pages. Both are dropped from the plan.

Smaller corrections: bundle verification is cached per `Store` instance (in
practice, per drain process); renderers read receipts of the displayed
generation, which can be an old one; `landing` grows per date (tiny, now
listed); a section cache needs its own retention. Codex did not check the
blu6 figures, which were measured directly.
