# Scalability and efficiency plan

Status: revision 2, 2026-10-04, after a Codex review. Changes from revision 1
are marked *(rev 2)*.

The supervisor works, but it clogs: the outbox reached 3000 entries twice in
two days, the data directory holds 4.7 million files, and every followup
re-examines every PR we have ever reviewed. This plan reorganises the work
around one rule:

> **Never pay for an operation when a cheaper tier can answer the question.**

| Tier | Operation | Cost | Typical unit |
| --- | --- | --- | --- |
| 1 | Inference (Claude, Codex) | quota, minutes, money | 3 to 40 min per pass, four passes per PR |
| 2 | GitHub API | 5000/h REST budget, ~360 ms per `gh api` spawn | 8 to 12 calls per PR per run |
| 3 | firmware.ardupilot.org | rsync over ssh, HTTPS fetch | ~7 s per page publish |
| 4 | Local filesystem on blu6 | stat, read, write | microseconds to milliseconds |

Every check becomes a ladder: answer it locally if possible, else from our own
published mirror, else from GitHub, and only then decide whether inference is
needed. Each tier must also be bounded, so that work per run grows with what
*changed*, not with what *exists*.

## Measurements (blu6, 2026-10-04)

| What | Measured |
| --- | --- |
| Files under `review/data` | ~4.7 M |
| Legacy work directories (`fu_*`, `allrun-*`, `aireview-*`, `followup-*`, from the retired path) | 104 dirs, ~2.5 M files |
| `runs/*/attempts` | 1601 attempts, 979 k files; median 31 files, max 11 057 (a Codex `cold-evidence` tree) |
| `receipts/` | 248 k files, one per delivered outbox entry, never removed |
| Agent litter at the top of `review/data` (build trees, logs, venvs, scratch, tmp) | ~470 entries; `scratch` 115 k, `venvs` 108 k, `tmp` 66 k files |
| Outbox entries queued by one all run, almost all for PRs whose review was *reused* | ~2000 |
| Followup candidates | 186; 132 had our last comment within 14 days, 41 have none (dropped) |
| GitHub calls per PR in a followup | `pulls/N`, issue comments, commit status, check-runs at discovery, then the same again at admission: ≥8, plus reviews and inline comments for a moved PR |
| `gh api` cost | 363 ms per call on blu6 (process spawn and auth), not counting the request |
| Page publish | ~3 s rsync + ~3 s HTTPS fetch-back verify + ~1 s render |
| Local mirror of the published tree | exists: `review/data/mirror`, 57 MB, written at cutover, not kept current |

## 1. Local storage: a garbage collector and a smaller footprint (tier 4)

### 1a. `review-gc.py`, a separate cron job

A standalone Python job, hourly under `nice`/`ionice -c3`, holding no review
locks except where it deletes store state. It has `--dry-run` and prints what
it would remove per rule. Rules, in order:

GC works by **reachability and verified ownership first, age second**
*(rev 2)*. An attempt directory is pinned while any of these name it: an
active or prepared claim, a carried pass (promotion copies evidence from the
original attempt), a deferred PR's selected passes, or a live guardian
(process and cgroup checked, as the design requires before disposing of
worktrees). Rules, in order:

1. **Unpinned attempt evidence.** Delete evidence and build trees from
   unpinned attempts of finished runs after 2 days, keeping `job.json`,
   `status.json`, `launch.json`, the result file and `payload.log`.
   Compressing `payload.log` waits until the dashboard reads the compressed
   name.
2. **Old runs.** A run older than 30 days keeps only `run.json`,
   `summary.json` and the attempts' `status.json`; after 90 days it goes
   unless something above pins it.
3. **Scratch areas.** `tmp/`, `scratch/` and `venvs/` at the store root are
   agent litter from before 1b; they are removed once 1b is in place and no
   pass is running. Directory age alone is not proof a tree is unused.
4. **Legacy directories.** The retired path's work directories are deleted
   once, after confirmation; nothing reads them.

**Receipts are not garbage-collected in this step** *(rev 2)*. Recovery
walks every accepted generation and re-materialises any intent without a
receipt; deleting old receipts would recreate old publications, annotations
and board syncs, and consumers read receipt contents (posting predecessors,
section digests, landing anchors), not just their existence. Absence must
never be read as success. Compaction waits for step 7: one receipt-lookup
abstraction over an indexed ledger, fsynced before any file is removed, with
recovery, dependency checks, renderers and dashboards all moved onto it.

The GC job records what it found and removed per area; runs.html shows the
GC job's last statistics rather than walking the tree on every build.

### 1b. Stop agents writing into the store root

Agents leave build trees, logs and venvs at the top of `REVIEW_DATA`.
`review-run-config.py` grants `REVIEW_ROOT` plus configured writable roots,
and inference adds the attempt directory and already puts `BUILDLOGS` there.
First audit the effective grants and what tools need (caches, venvs); then
narrow writes to the attempt directory with its own `scratch/` and `TMPDIR`.
Setting `TMPDIR` alone is not isolation. *(rev 2)*

## 2. Followup: bounded, change-driven discovery (tiers 4 then 2)

### 2a. Window

A followup considers a PR only if **we posted a review of it within the last
14 days** (`REVIEW_FOLLOWUP_DAYS`, default 14), or it currently carries a
trigger label. That needs a small local index the store does not have yet
*(rev 2)*: per PR, the accepted head, the told head, the time the comment was
actually posted (from the posting receipt), the latest observed head, and any
pending followup. Accepted, posted, held and report-only are different
states, so the newest generation's date is not a substitute. Imported legacy
comments keep their GitHub timestamps. The window never expires work already
found pending or delivery still owed. A PR outside it is re-reviewed when
labelled, or on request with `review-now.sh`. Unlabelled PRs with a previous
review stay eligible inside the window, as today.

### 2b. One batched query instead of four calls per PR

For each repository, one GraphQL query per 100 PRs returns `number`,
`headRefOid`, `updatedAt`, `state`, `isDraft`, labels, and the
`statusCheckRollup` state. Compared locally against the stored told head, it
classifies every candidate:

- **unchanged head** → reused, with no further GitHub calls;
- **closed, merged or unlabelled** → dropped;
- **moved** → full per-PR fetch: thread, reviews, inline comments, diff.

A followup of 186 PRs then costs a few GraphQL calls plus a full fetch for
the PRs that moved, instead of ~1500 REST calls. The query is specified and
measured, not assumed *(rev 2)*: by stored node id with aliases, explicit
head-commit rollup, null handling, label pagination, split-and-retry of a
failed batch, `rateLimit.cost` recorded, and a partial result rejected
unless each PR's data is complete (GitHub caps connections at 100 items and
queries at 500 000 nodes). For the first weeks it runs in shadow beside the
full scan, and the two classifications are compared.

A search per repository, `is:pr is:open updated:>=<watermark>`, can narrow
the batch further but is never the only negative proof *(rev 2)*: it cannot
report PRs that closed, it caps at 1000 results with its own 30 per minute
limit and an `incomplete_results` flag, and a watermark advanced past a PR
that failed or was deferred loses it. Watermarks overlap, advance only after
candidates are durably recorded, failed and deferred PRs are kept
separately, and the eligible set is reconciled directly every few runs.

### 2c. Admission does not refetch what discovery just read

Today admission repeats the full per-PR refresh. Instead:

- a reused or dropped PR is not refreshed again, but its accepted state is
  still consulted and its delivery obligations repaired, as claim admission
  does today *(rev 2)*;
- a PR about to be reviewed gets **one eligibility check immediately before
  its first inference pass** (tier 2 guarding tier 1): head, node id, base
  branch, open and draft state, and labels, not the head alone, since a
  retargeted base changes the diff. Discovery's thread is reused if all of
  those match and the read is under an hour old. Inputs stay pinned once work
  starts; reconciliation keeps its fresh look at later changes.

## 3. GitHub efficiency (tier 2)

1. **One HTTP client instead of `gh api` spawns.** A small persistent client
   (`http.client` with keep-alive, token from `gh auth token`) removes the
   ~360 ms per call and allows the next item.
2. **Conditional requests.** Store each REST response's `ETag` with its
   body and pagination links, keyed by account, URL, media type and API
   version; send `If-None-Match`. An authenticated `304` costs no primary
   rate-limit budget, though it is still a round trip. Reads used to
   reconcile a possibly lost comment always revalidate: a stale cached
   absence must never authorise a repost. *(rev 2)*
3. **CI only where shown.** CI state comes from the batched rollup; the
   detailed status and check-run listing is fetched only for PRs being
   reviewed.
4. **Local git before GitHub.** Diff construction fetches head and base and
   then asks GitHub for the file list it could compute locally; check for the
   objects first and derive the pinned diff locally. That also avoids the
   files endpoint's 3000-file cap. *(rev 2)*
5. **Board delivery.** Each board intent reads the PR and thread, discovers
   fields, enumerates the whole project, writes, then enumerates again. Cache
   field and item ids and read back only the changed item. The 15-minute
   `project-sync` sweep becomes a slower reconciliation, since outside edits
   are only seen by looking. *(rev 2)*
6. **Accounting.** The client records per run, endpoint and account the
   request count, `x-ratelimit-resource`, remaining and reset, `Retry-After`,
   latency and GraphQL cost, and paces controllers and the drainer that
   share an account. GraphQL and secondary-limit errors stop being generic
   failures. runs.html shows the totals. *(rev 2)*
7. **Later: webhooks.** Durable webhook ingestion could replace much of the
   polling, with deduplication, replay and periodic reconciliation. Not in
   this plan.

## 4. firmware.ardupilot.org (tier 3)

1. **Desired versus confirmed bytes** *(rev 2)*. Rendering writes a staged
   copy; a separate record holds the digest last *confirmed* published for
   each endpoint configuration and path. A publish is a local no-op only
   when the desired digest equals that confirmed digest and no upload or
   drift is unresolved. A failed upload never leaves identical local bytes
   looking published.
2. **Bounded upload batches.** Changed pages go in one rsync per endpoint
   configuration from a durable changed-file list and an immutable staged
   batch, never the whole mirror, and never with a broad `--delete`. Page
   ownership is held through the upload and acknowledgement so a concurrent
   batch cannot overwrite a newer page. A partial failure leaves the
   unacknowledged pages owed; itemized output alone never receipts a batch.
   Same-size changes within timestamp resolution use `--checksum` on the
   batch's files.
3. **Verification stays for pages a comment links to.** Posting verifies
   that every section a comment cites is served, under the page locks, and
   that guarantee stays. Fetch-back of other republishes becomes sampled,
   and that is an explicit contract change in the design document. *(rev 2)*
4. **Manifests and the dashboard read local state.** Label manifests come
   from confirmed local publication state, not HTTPS, and runs.html stops
   fetching three published pages on every build.

## 5. Outbox churn (tier 4 work that triggers tier 3)

1. **Measure first.** The drain already reuses one render for later publishes
   of the same page, so intent count overstates the cost. Count actual
   renders and transfers per run before estimating savings. *(rev 2)*
2. **Keep the observation, drop the redundant work** *(rev 2)*. A projection
   still records its observation ticket, because membership ordering depends
   on tickets and tombstones: suppressing a newer "present" because it looks
   like the stored row would let an older queued "removed" win. What is
   suppressed is the publish, when the normalised render inputs for that page
   are unchanged and the page's last render is confirmed published.
3. **Versioned page debt instead of per-PR publishes.** Replacing one publish
   per PR with one per page needs durable desired and published page
   versions, the uploaded digest and its section identities, and a rule for
   how each original waiter (comments depend on specific publications,
   landing pages on label publications) is satisfied or explicitly
   superseded. An upload of version N must not clear a request for N+1, and
   a newer version may no longer contain a waiter's section. Retained
   generation pages stay immutable. *(rev 2)*
4. **Delivery leaves the controller, into the existing drainer.** The
   controller only enqueues; `review-drain.py` becomes a systemd service with
   a timer (a path unit alone misses retry deadlines and uncertain-write
   grace periods). It keeps recovery, frozen configuration, page and PR
   locks, intent-before-send and marker reconciliation, which are what
   prevent duplicate comments. The `all` run's final drain and delivery
   summaries change with it. Discovery and admission still do remote work.
   *(rev 2)*

## 6. Inference (tier 1)

Already done: passes carry across runs when inputs are unchanged, the
reconciliation pass reuses finished passes, and a content-filter refusal
retries on a fallback model.

1. **Find out why busy PRs are re-reviewed** *(rev 2)*. Before any cooldown,
   classify #32995's 43 reviews by changed head, forced request, retry and
   failed reuse.
2. **Re-review interval per PR**, if that shows pushes drive it: a PR
   re-reviewed within 12 hours waits unless labelled for a call in the next
   24 hours. It needs a durable `next_eligible_at` and pending head, so the
   change survives search watermarks and the followup window.
3. **Incremental followups.** A followup review of a moved PR covers the
   diff since the told head with the previous findings, with fallbacks for
   force-push and rebase, and a disposition for every previous finding.
4. **Size tiers** (a policy choice for tridge): small diffs could skip the
   independent Codex review. Acceptance currently requires all four passes,
   so this is a design and validator change, not a tuning knob.
5. **Wider pass reuse.** Carrying passes today looks only at the previous
   claim's selected passes, and the key omits prompt, model and effort.
   Widening reuse needs those in the key. *(rev 2)*

## 7. Other work that grows with history *(rev 2)*

| Today | Change |
| --- | --- |
| Every supervisor save enumerates all receipts to rebuild an index | Incremental receipt consumption or indexed lookup |
| Drain selection reads and sorts the whole outbox; coalescing and comment ordering scan it again | Index due work, page debt and uncertain writes |
| Recovery walks historical claims and chains; bundle reads rehash all evidence | Recovery checkpoints; direct immutable-generation lookup without re-verifying on routine reads |
| New generations inherit every earlier mutable destination | Freeze and retire dated and followup pages after their day, keeping their links |
| runs.html reads all wrapper logs and summaries before its 10-day filter | Filter before reading; keep usage aggregates |

## 8. Order of work *(rev 2)*

| Step | Change | Tier relieved | Measure before and after |
| --- | --- | --- | --- |
| 0 | Baseline and correctness gate: instrument inference per PR, head and reason; GitHub requests and points; actual renders and uploads; oldest debt; launch latency; files and bytes; scan times. Add crash tests for out-of-order observations, upload success with lost receipt, a new page version during upload, and lost comment responses | all | the baseline itself |
| 1 | Storage relief: audit and narrow agent writes; GC unpinned evidence, scratch and legacy dirs | 4 | files, bytes, GC time, carried passes still promotable |
| 2 | Producer churn: keep observations, suppress unchanged publishes; versioned page debt | 3, 4 | intents per unchanged PR, actual publishes, remote work in an unchanged run |
| 3 | Delivery moves to the drainer service | 3, 4 | scheduler latency, acceptance-to-post latency, backlog recovery |
| 4 | GitHub demand: posted/pending index and followup window, admission reuse, local git, batched query in shadow, accounting, ETags | 2 | coverage against the shadow full scan, calls and points per changed PR, inference avoided |
| 5 | Confirmed-mirror no-ops and bounded upload batches; verification kept for linked pages | 3 | files scanned and sent, connections, verification traffic, partial-batch recovery |
| 6 | Indexes for receipts, outbox and recovery from section 7 | 4 | controller save, drain selection and recovery times |
| 7 | Receipt ledger, then compaction, once every reader uses it | 4 | restart and rebuild cost; no recreated deliveries |
| 8 | Re-review cause analysis, then cooldown and incremental followups; size tiers only as a policy decision | 1 | tokens and latency per changed head, pending-work age, findings retained |

Each step lands with tests and its own measurement on runs.html, in a
separate commit per subsystem.

## Risks

- **Missing a change.** The batched query decides from head, base, state and
  labels; a new comment on an unmoved PR does not trigger a full read. That
  matches today, which reuses on an unchanged head.
- **Receipt compaction.** Covered by deferring it to step 7 behind a ledger;
  absence is never read as success.
- **Mirror drift.** If someone edits the published tree by hand, the
  confirmed state is wrong. Sampled HTTPS checks detect it; repair goes from
  the store's authoritative state to the server, never the reverse. A full
  checksum scan is proportional to total bytes, so it runs rarely.
- **Window too tight.** A PR whose author returns after three weeks gets no
  followup until it is labelled. The window is a setting, and runs.html can
  list PRs that left it with a pending change.
