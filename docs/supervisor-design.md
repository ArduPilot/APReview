# Review supervisor: orchestration in Python, inference per PR

Status: design, revision 4, 2026-09-28. Slices one and two implement the core
and adapters; slice three supplies opt-in integration and migration. Revisions
1 through 3 were reviewed by Codex (reviews kept under
`/data/review/supervisor-design/` on the development machine); this
revision answers the third review. Out-of-scope decisions are listed at the
end so they are not mistaken for oversights.

## Why

The all-labels run of 2026-09-28 took 5h39m to review seven PRs. Its log
shows where the time went:

| Phase | Started | Ended |
|---|---|---|
| discovery and classification | 00:13 | 00:37 |
| review agent for #33734, launched alone | 00:37 | 01:58 |
| review agent for #33942, launched after the first returned | 01:59 | 02:37 |
| review agents for #34511 and #34479, together | 02:39 | 03:26 |
| Codex validation pool, report sections | 03:33 | 04:02 |
| label re-sweep finds three PRs labelled during the run | 04:02 | |
| review agents for #33868 and #34234, together | 04:09 | 05:15 |
| validation, comment bodies, posting | 05:16 | 05:52 |

The Codex cold pool for the first batch finished ten minutes after it started
and then waited two and a half hours for the Claude side. The instruction to
launch one agent per PR in a single message so they run concurrently
(`commands/reviewprs.md`, step 3) is unambiguous and was not followed. The
main agent also rewrote its discovery, classification and pool-launcher
scripts from scratch, as it does every run. The run held the global lock
throughout, so the followup scheduled behind it waited two hours and gave up.

Token split for that run, measured from its transcripts: the main context
used 90.6M tokens over 312 turns, the seven review agents 139.3M over 1144
turns. The orchestrator is about 40% of the raw tokens. Removing it saves
that share; the per-PR work costs what it costs, reconciliation as a fresh
session and the kept validation pass add some back. The expected gain is
wall-clock, subject to resource and account contention. Cost per job kind
is to be measured from the canary before any saving is claimed.

Everything in that run except reading the diffs was deterministic work. It
belongs in Python, tested once, where a fix stays fixed and a rule cannot be
skipped. What remains for inference is a fixed set of jobs per PR, each
carrying only its own PR, all runnable in parallel.

## Principles

1. **Python owns the run.** Discovery, classification, scheduling, worktrees,
   isolation, timeouts, retries, deferral, the report, publishing, the
   manifest, comment posting and the board are code. The 1300-line prompt
   shrinks to short per-PR prompts plus the house rules.
2. **Inference does a fixed set of things per PR**, one job each: a primary
   review, a cold pass, a validation of the primary's findings, and a
   reconciliation that writes the final section and comment. No job
   orchestrates another, grants itself posting authority, or reports its
   own resource use as authoritative.
3. **Runs share the machine, not a lock.** A run waits on another run only
   where they touch the same PR, the same page, the board, the reference
   clone or a resource budget, and each of those waits is bounded and
   named.
4. **Depth is fixed; the number of PRs flexes.** Deferral rules are
   unchanged: oldest-first up to the limit, the rest named and carried over
   with their old manifest head, never a shallower pass. A PR whose passes
   did not all succeed is deferred, not posted from what did.
5. **Reviewing a PR and delivering the result are different things.** An
   accepted review is immutable and never repeated because a publish, a
   post or a board sync failed. Delivery is an outbox, drained by whichever
   supervisor runs next.
6. **The page is a view, not a run artefact.** It is rendered from a durable
   store of accepted results and membership records. A run writes only its
   own PRs' rows. Nothing about a run in progress erases the record of a
   review that happened.
7. **Locks die with their holder and vanish on reboot.** The box is a VM
   under Windows, which reboots when it likes. Nothing may depend on a lock
   file being cleaned up, and nothing written in a lock file authorises
   taking or clearing a lock.
8. **Discovery happens once per phase, against a snapshot.** Latency for a
   PR labelled afterwards comes from the run cadence and from
   `review-now.sh`, not from re-sweeping at the end of every run.

## Locking

One lock file, `$REVIEW_DATA/locks`, created once and never replaced or
unlinked (a new inode would be a new lock domain). Linux OFD byte-range
locks (`F_OFD_SETLK`), 32 bytes per region. The layout is versioned; every
participant must reject an unknown version. Changing it requires quiescing
all participants, not quietly using a different mapping.

| region numbers | use |
|---|---|
| 0, 1, 2, 3, 4 | `board`, `refresh`, manual `pause`, observation counter, quota state |
| 256..511 | `permit:claude:0..255` |
| 512..767 | `permit:codex:0..255` |
| 768..1023 | `permit:heavy:0..255` |
| remaining regions below 4096 | reserved, never hashed into |
| `4096 + k * 2^20 .. 4096 + (k+1) * 2^20 - 1` | separate tables: `k=0` PR, `1` page, `2` run, `3` account |

The byte offset is 32 times the region number. Only slots below the
configured pool size are usable; changing sizes requires drained pools.
For striped kinds use the first eight bytes of SHA-256, big-endian, modulo
2^20. Hash UTF-8 canonical keys including the kind prefix, never Python's
process-randomised hash. PR keys are `pr:<lowercase-owner>/<lowercase-repo>#<decimal-number>`;
resolve aliases and repository renames to the configured canonical name
and check the stored node id. A rename requires drained ownership before
changing that name. Run keys are `run:<absolute-real-path-of-run-directory>`.
Page keys are `page:<endpoint-id>/<relative-path>`, using a configured
publication endpoint id and its case-sensitive, normalised relative POSIX
path (no `..`, query or fragment); URL/rsync aliases
must map to the same endpoint. Account keys are `account:<provider>/<id>`
with a stable account id, not the caller's role alias; shared credential
homes map to one key. Preserve UTF-8 case/bytes outside the lowercased repo
name; do not let callers independently normalise configured identifiers.

**One open description per owned region and lifetime.** Take each region on
its own `open()`, close-on-exec by default. A PR description is shared only
with that PR's attempt guardians via `pass_fds`; inference and its descendants
receive none. Never unlock a shared PR description explicitly: each holder
closes its copy, and the last close releases it. No descriptor is shared
between PR keys, even if they hash to the same stripe.

**Claims never wait.** The scheduler tries a PR with `F_OFD_SETLK` once. If
busy, including a stripe already held by this controller for another PR,
it leaves the PR queued, keeps its original oldest-first position, and
tries other candidates. Retry with 1..30 second backoff until the admission
deadline; expiry records `deferred: PR busy`. There is no waiting for a PR
while holding another PR, page, board, account or permit, and no multi-PR
transaction. A controller may own several independently claimed PRs, but
an operation on one never depends on claiming another. Same-stripe PRs
therefore serialize; they are never combined or given a fallback slot.

After the optional run-controller lock and a non-blocking PR claim, the
only nested acquisition order is:

```
observation counter -> refresh -> provider permit -> account -> quota state
                    -> heavy permit -> page regions in offset order -> board
```

The run and PR precede this list. Skip unused kinds; release short-lived
locks promptly. Never acquire a run lock while holding any other lock or
hold two run locks. Pool acquisition tries free slots, never waits for a
particular occupied slot. Deduplicate page *regions* before a multi-page
operation, using one description for a colliding set for that operation's
whole lifetime. Page renderers read atomic store snapshots and never claim
PRs. Board code never takes a PR or page lock. Refresh code never takes a
PR or permit. Account probes/cleanup never take a provider permit. Thus
none of these waits has an edge back to a PR, including a controller's
other PRs. Every poll and external call has an absolute deadline.

The header in each region remains advisory:

| bytes | field |
|---|---|
| 0-3 | magic and layout version |
| 4-7 | first 32 bits of the canonical key's SHA-256 |
| 8-11 | holder pid |
| 12-19 | process start time from `/proc/<pid>/stat` |
| 20-23 | kind |
| 24-31 | boot id prefix |

Only an exclusive holder writes it; shared readers do not overwrite one
another's headers. The full boot id, pid/start time, cgroup and attempt
identity live in durable attempt records. The kernel decides ownership;
neither a stale header nor a missing heartbeat authorises clearing a lock.
A PR probe says only that some holder lives, not that a particular job lives.

`run:<dir>` permits one controller. Manual `pause` is held by
`pause-runs.sh`; schedulers and `review-now.sh` probe it before admission,
while delivery and cleanup continue. `refresh` is exclusive for reference-clone refresh and
shared for worktree creation/removal; worktrees pin their commits against
gc. `page:<destination>` protects render, remote replacement and receipt;
the label's latest-page region also protects its membership read/merge/write.
`board` protects the project write, replacing its separate `flock`.

Account regions are shared leases for the full credential-using CLI lifetime,
including launches and quota probes. Stale OAuth-file cleanup, credential
relinking and account configuration changes require an exclusive lease:
verify boot/pid/start-time ownership before deleting a stale native lock.
At launch, try exclusive cleanup and downgrade to shared before exec; if
another shared user is live, join shared without cleanup. Never upgrade a
shared lease in place or delete a live CLI's native refresh lock. Token
refresh still uses the CLI's native credential lock; test that concurrent
clients honour it before enabling overlap for an account. An account whose
CLI cannot safely share credentials uses exclusive leases until fixed.
Quota state writes use the separate fixed quota region, not a shared lease.
GitHub adapters acquire any credential leases before page/board locks; if
several accounts are needed, acquire deduplicated account regions in offset
order. No credential operation acquires an earlier lock kind.

A small Python lock library and `review-lock.py` share this mapping. Shell
adapters call bounded leaf operations through it; `project-sync.sh` must
not wrap an outbox drain in a board lock. The global run lock goes away.

## Components

All under `runner/bin/`, wrapped by `run-reviewprs.sh`, which keeps account
selection and the permission preflight, and gains `--resume <run-dir>`.
Quota preflight becomes input to inference admission: it cannot prevent
starting the supervisor or recovering publication, comments and board work.
The exit trap does not kill surviving attempts; the old path-based reaper
is replaced by attempt/cgroup reconciliation.

Each inference attempt has a small **Python guardian**, started as an
independent `systemd-run --user` unit outside the supervisor's cgroup, with
a private payload cgroup beneath it carrying the current CPU, memory and
task limits (today `MemoryMax=40G TasksMax=4000 CPUQuota=1600%` for a
pool). User units die with the user manager when the last login session
ends, so `loginctl enable-linger tridge` is a prerequisite on the box; it
is off today. It owns
the inherited PR descriptor, provider permit,
account region, any heavy permit, and the payload cgroup. Before launching
anything it durably records identity, full boot id, pid/start time, cgroup,
resource slots and absolute timeout, then acknowledges ownership to the
controller. Only then may the payload start. It closes unrelated inherited
descriptors, and closes all lock descriptors in the payload. There is no
parent-death signal tied to the supervisor.

The guardian waits for the actual CLI child, enforces its timeout even if
the supervisor disappears, and records exit code/signal, timed-out/aborted
flags and result-status (`missing`, `invalid`, `incomplete`, `complete`).
On exit or timeout it kills remaining payload descendants and waits for
`cgroup.events` to report empty, validates and persists the now-stable result
and retained evidence, then fsyncs its terminal record before
closing permits, account and PR descriptors. JSON alone is never success:
acceptance needs exit zero, no timeout/abort, a valid complete result and
matching generation/attempt identity. A guardian does not promote results
or start the next inference stage.

The heavy wrapper takes a `permit:heavy` region itself, on its own
`open()`, **before** the build or SITL starts, and runs the command with
that descriptor inherited. Every heavy descendant, background SITL
included, then holds the permit until it exits, and the guardian's cgroup
kill bounds all of them. A nested wrapper finds the permit in its
environment and reuses it. The wrapper returns the command's status
normally. This is the one lock descriptor a payload may hold, and it
is a permit, never ownership of a PR. Namespace holders run inside the payload cgroup
with no lock descriptors, so neither they nor background builds outlive the
attempt's timeout. Wrap the test, not the inference CLI; retain the current
network-namespace dependency prefetch and no-`--uds` rules.

The service manager kills the payload if the guardian itself dies. Before
reusing a PR or resource slot, its new holder checks durable previous-owner
records and completes orphan cleanup, verifying the old cgroup is empty;
it must not infer emptiness merely from acquiring the OFD lock. Persist
previous-owner records by **physical region**, so colliding PR keys find
the earlier payload too. The PR record points to its claim/attempt registry;
the controller registers each attempt before spawning it. Permit records
are written under their slot locks before use. These records guide cleanup,
never replace kernel ownership.
A dead guardian without a terminal record gives an unknown/failed attempt,
never an inferred successful exit. Failure to empty a cgroup blocks reuse
and is reported. Reboot makes old-boot payloads absent and releases all locks.

### `review-supervisor.py <mode> [--resume DIR]`

Modes and resolution precedence are those of the command today: a label,
`followup`, `rsync`, an author, a PR reference, and `all`. Strip
`--interactive` before resolving the mode; it enables questions on the
controlling terminal, never unbounded waits or wider posting authority.
Without a terminal use the documented unattended defaults. Comment-posting
labels and rsync post; other labels and author sweeps are report-only unless
explicitly authorised; repository holds still apply.

Each run gets `$REVIEW_DATA/runs/<mode>-<stamp>/`. Freeze mode, request id,
archive dates, destinations, repository/rule/config snapshots, account ids
and credential locations, models, effort, permissions, granted directories,
pool limits and deadlines there, without copying credential secrets. Resume
uses this record, even when an `all` invocation discovers an unfinished rsync
run. Missing credentials or incompatible configuration defer that run; they
never silently substitute the new wrapper environment. Attempt inputs and
results carry run/job/attempt ids; stale attempt outputs are rejected.

Startup takes a finite snapshot of unfinished run directories and outbox
work before owning its new run lock. A recovery coordinator tries each run
lock once and starts at most one controller for a free run; it neither waits
for a busy run nor owns two runs. Each controller continues independently.
The initial delivery drain is at most 100 entries or 60 seconds total,
whichever comes first, before new discovery. Bundle/index repair and orphan
inspection share that budget; persist a cursor over the finite startup
snapshot for later ticks. Unresolved attempts stay unavailable, not duplicated.
Each later drain takes a new finite snapshot with the same limits.

`--resume` takes the run lock non-blockingly and monitors live guardians
without taking over their descriptions. It can handle other queued PRs
while a guardian finishes. When all guardians for a PR have closed, claim
that PR non-blockingly, reread state and validate their terminal records
before continuing. If another controller claimed it first, normal
reclassification applies. The supervisor keeps its own PR copy during
normal work and closes it on acceptance/deferral only after guardians finish.

`review-now.sh --abort <run>` first writes a durable idempotent abort request
and signals the verified controller/guardian identities; it does not wait
for the run lock to ask a live controller to stop. Guardians check that
request at launch and at least once a second. They stop payloads and record
aborted outcomes. If the controller is dead or unresponsive, the service
manager stops its services and the run's attempt services after a bounded
grace period. Only then does a cleanup controller claim the run and PRs to
reconcile state. Already accepted results and delivery debts survive abort;
no new inference starts for that run.

The dashboard reads versioned atomic summaries, `runs/<run>/summary.json`
written by the controller and `runs/<run>/attempts/<attempt>/status.json`
written by the guardian, carrying: run, PR, generation, job, attempt,
provider/account, session id, boot/pid/start time, cgroup, state,
heartbeat, exit/timeout/result-status, quota observations and usage totals. Guardians heartbeat every 30 seconds; the controller emits
aggregate summaries. Session ids belong to exactly one attempt and token
counts are deduplicated by that identity, never wall-clock overlap or the
first result line in a combined log. Silence means unknown until process
identity/cgroup checks prove death. The dashboard has its own page lock.

### Discovery (`review-discover.py`)

Runs once per discovery phase, against a snapshot of heads; the candidate
set is fixed. A queued candidate may be refreshed under its PR claim before
admission; after that, job inputs are immutable. Persist the complete phase
snapshot before admission and reuse it on resume; a failed fetch before
that commit may be retried, but a committed phase is never rediscovered.
Output: a candidate list
with, per PR: repository,
number, node id, manifest key (the `repos.json` key plus number, so `wiki`
and the two mavlink repositories map as they do today), head, base and
merge-base, created-at, title, author, labels, draft flag, the CI state as
one of passing / failing / pending / none / unknown with the head and time
it was observed at, the previous comment (id, body, told-head, its
findings), the previous section if any, the previous manifest head, the
resolved house-rule text for the repository, and the classification
`REVIEW`, `REUSE`, `DROPPED` or `DEFERRED` with the reason.

- **Label modes:** `gh` search per swept organisation for the label, over
  the repositories `repos.py --sweep` lists plus ArduPilot-owned submodules.
  Board rows are not candidates for a label report; they belong to
  followup eligibility and to the board's own retention rule. Manifest-head
  comparison is provisional: accepted state is authoritative at claim time.
  A PR in the manifest and no longer labelled is `DROPPED`.
- **Followup:** the union of every published per-label manifest, plus the
  board's rows (read with the project-capable identity, as the board sync
  does), then the told-head from the PR's newest AI comment matched against
  every configured comment account with pagination, then the filters as
  today: open, not a draft, head moved, and the patch at each head compared
  with hunk offsets and index lines stripped plus the blob comparison for
  binaries, so a rebase-only move is skipped. A PR with no AI comment is not
  a followup case. Every affected label's page is refreshed, and the
  refresh of each is its own outbox entry so a crash between two labels
  leaves the second still owed.
- **PR mode:** the one PR, incremental skip ignored, publishing to that
  PR's own page, refreshing every label page it appears on, and inserting
  the PR on the board by node id if it is not there, since board discovery
  would never find an unlabelled PR.
- **Author mode:** as today, report only, page kept current, never
  overwritten with an empty page.
- **Rsync:** `RsyncProject/rsync`, label `AIReview`, own page and manifest.

Admission order within the REVIEW set is by PR creation time, oldest
first, with canonical PR key as the tie-breaker. Busy candidates keep their
place but do not hold up unrelated PRs.

There is no re-sweep during or at the end of a run, and no end-of-run
recheck of heads that were skipped as unchanged. Both were added after a PR
labelled in the same minute as discovery was missed on 2026-08-30. With
all-runs every six hours and followups every three, and with
`review-now.sh` able to start at any time because there is no global lock,
the next selecting run catches anything still eligible. One-off labels,
author sweeps and unlabelled PR requests need an explicit retry if deferred;
the summary must not promise a cron run that will never select them. This morning's re-sweep cost
1h50m and held the followup behind it to its timeout. The promise is about
a PR's latest eligible state, not every head it passed through.

### The scheduler

Per PR, four independent tracks:

```
review:    pending -> claimed -> reviewing -> reconciling -> accepted(gen)
                                    |             |
                                    +-> deferred  +-> deferred
review:    pending -> reused | dropped
publish:   per destination: owed -> published(revision) | superseded
comment:   owed -> sending -> posted(id) | uncertain | held | not_applicable
board:     owed -> synced(target, observation) | not_applicable
```

All delivery tracks can finish `superseded`; a retryable error leaves durable
work owed. The run-local outcome may be `delivery_deferred` while that debt
remains. Held and report-only results are accepted reviews, not failed jobs.

Under each PR lock, **reread before allocating**: the claim record, accepted
`current`, receipts, unfinished attempts and GitHub's current head, base,
open/draft state and relevant labels. Recover promotable completed work
first. If the accepted review already covers the live head, reuse it and
repair delivery/projections, even if the published manifest or told-head
lags. Followup still applies its rebase-only test when heads differ. A new
explicit PR request forces a fresh review even at the same head; resuming
that same request does not. If discovery's head/base is stale, refresh just
this candidate's diff and metadata before allocating, subject to the same
admission deadline; defer if that cannot finish or eligibility is gone.
Never allocate a newer generation against a known older snapshot. These
per-candidate checks are not another discovery sweep.

`claim.json` has a monotonically increasing counter, separate from `current`,
and the active generation, run/request id and status. Durably increment it
under the PR lock before launching an attempt; gaps from crashes or deferrals
are never reused. Retry attempts keep the generation but get new attempt ids.
Acceptance requires that active claim and all selected attempt ids still
match. `current` advances only at acceptance, never at claim. A new claimant
resuming the original run may reuse completed passes only if head/base,
rules, frozen configuration and input identities still match. Another run
can recover a complete acceptance transaction, but fences an abandoned
incomplete claim before allocating its own generation. A live guardian
always prevents that takeover.

`reviewing` runs primary and cold together; validation starts when primary
finishes. Before reconciliation Python fetches fresh title, head and the
paginated thread, including replies to previous findings, and gives this
new snapshot to the reconciler. Fetch failure defers reconciliation. A moved
head stays explicit: this generation reviews its pinned head, cannot claim
to validate newer code, and is eligible for a later followup. If any required
pass fails after its retry, defer the PR, carry its previous section and
manifest head, and publish no verdict from a subset.

Acceptance commits the bundle below, then releases the PR after all guardians
finish. Delivery independently claims the PR and performs publication,
then comment, followed by independently receipted deprecation and targeted
board update; labels share one canonical comment delivery. Both latter
steps depend on posting success; failed deprecation does not block the
board. Hold decisions and exact manual commands are durable; report-only
comments are `not_applicable`.

Machine-wide defaults are 4 Claude, 4 Codex and 4 heavy permits. Claude slot
0 is reserved for reconciliation and Codex slot 0 for validation, so
finishing admitted PRs takes priority over new primary/cold jobs; that
leaves three primaries and three cold passes in flight at once. Accounts requiring
exclusive credential leases may impose a lower limit. The admission
deadline is frozen at run creation (default four hours). Permit/account/refresh
waits default to two minutes;
job wall limits, starting at payload launch, are review 90 minutes, cold and
validation 30, reconciliation 45. Heavy waits count inside that wall limit.
Cleanup has a 30-second grace before service-manager escalation. External
delivery calls default to 20 seconds, lock waits to five seconds, each
capped by the remaining aggregate drain budget. Expiry defers inference or
leaves delivery owed; it never drops ownership of live payloads.

Isolation: one detached worktree per attempt with its own sibling buildlogs,
created/removed under shared `refresh`, never a partial clone. Each attempt's
PATH replaces base-clone `Tools/autotest` with its own worktree's tools;
no job reads scripts from the mutable reference checkout. Dependencies are
fetched before namespace entry. Reconciliation gets a clean worktree plus
retained evidence outside disposable worktrees.

### The inference jobs and their contract

Each job runs in its own attempt directory with `REVIEW_JOB_DIR` set, is
given `job.json`, and must write a result file the supervisor validates
against a schema before accepting it. Invalid output, an unknown field, an
identity that does not match the input, duplicate finding ids, or an
incomplete status is a failed attempt. Enforce bounded file/string/list
sizes in the schema before loading or rendering output.

`job.json` carries: schema version, run, job, attempt and generation ids,
repository, PR node id, number, full head, base and merge-base, the diff
snapshot, the thread snapshot, the previous comment and its findings with
told-head, the mode, the resolved house-rule text, the worktree path, and
the job kind. The cold pass's input excludes the primary's results.

Every result carries the same identity fields, a `status` of `complete` or
`incomplete` with the gaps named, and a `heavy` flag stating whether it
built or ran SITL (informational; the permit is what enforced it). The
guardian records process exit, timeout and validated result-status
independently.

**Primary review** (`claude -p`, the review prompt): `review.json` with a
verdict; findings each with a namespaced id (`primary:F1`), kind BUG /
ISSUE / NOTE, severity rationale, claim, location (file, line, side of the
diff, revision) or an explicit non-line-specific marker, status VERIFIED /
UNCONFIRMED, and evidence as commands run, exit status, observed result, and
retained artefact paths and build/environment configuration; a list of what
was checked and found clean; and for a followup, a disposition RESOLVED /
STILL OPEN / DISPUTED for every finding
of the previous round, engaging with any author reply. Metadata-dependent
claims are provisional until reconciliation checks the fresh title/thread.

**Cold pass** (`codex exec`, raw, never `codex-session`): `cold.json`, same
finding shape, ids `cold:F1`, its own verdict, no sight of the primary.

**Validation pass** (`codex exec`): given the primary's findings, checks
each for correctness, severity and location; `validate.json` with an
outcome CONFIRM / ADJUST / REFUTE per primary finding and any NEW ones,
each with evidence. This is the pass the current step 7 runs; it is kept so
that every primary finding receives an independent challenge. Codex's
counts in the report come from this file and only this file.

**Reconciliation** (`claude -p`, the reconcile prompt): given all three
results and a clean worktree. Re-verifies everything that drives the
verdict and every withdrawal; `final.json` with the verdict, an outcome for
every finding from all three inputs including merged duplicates, an outcome
for every previous-round finding, `section_md` and `comment_md` as prose
only, and a one-line summary. Validation must account for every primary id;
reconciliation must account for every primary, cold, validation-NEW and
previous-round id exactly once. A merged duplicate names its surviving id;
reject missing targets or cycles. Each final finding has an explicit
blocking flag and disposition retained / adjusted / refuted / merged, with
evidence for adjustments and withdrawals. A disputed or unconfirmed blocker
is still blocking until explicitly refuted or adjusted with rationale.
`ACCEPT` (input alias `APPROVE`) is illegal with an unrefuted blocker;
`REQUEST CHANGES` requires one; nonblocking actionable findings yield
`COMMENT`. Python checks these relationships as well as the schema.

Python generates what must not disagree with the structured result: the
AI-generated marker line, the verdict line on line 2, the reviewed-head
line, the report URL and anchor, and the structured tables. Inference writes
the prose beneath them. A followup note that re-reviews nothing is its own
result type, `note.json`; it does not advance the manifest head, carries no
verdict, and is posted without deprecating the previous review, so the
readable verdict stays.

The prompts live in `commands/` as short files: review, cold, validate,
reconcile. Repository rules are injected as text, resolved from
`repos.json` by `house_rules` category and `notes`, with a fallback for
submodules discovered dynamically.

### The result store, the outbox and the report

Durable records under `$REVIEW_DATA/`, all on one local filesystem:

- `results/<owner>/<repo>/<n>/generations/<gen>/`: an immutable bundle with
  result, retained evidence references, selected attempt identities, previous
  `current`, and the **complete** delivery-intent list. Include every
  destination, label projection, mode/policy, frozen archive date, account,
  comment template and dependencies. A note bundle refers to the previous
  reviewed generation and leaves its manifest head/verdict intact. Accept a
  note only once that review has a posting receipt; a note cannot supersede
  an undelivered verdict.
- `current` beside `generations/`: the promoted generation and bundle digest.
  Its predecessor chain identifies every accepted bundle, including ones
  superseded before their deliveries were materialised. `claim.json` is the
  separate allocation/fencing record described above.
- `membership/<label>.json`: rows and removal tombstones. The label's latest
  page lock protects the entire reread/merge/replace, not just the write.
- `outbox/<delivery-id>.json`: a rebuildable index plus durable sending state,
  frozen final payload, retry count and next attempt time. Retained
  `receipts/<delivery-id>.json` record success, hold, not-applicable or
  supersession, including target, generation, payload digest and returned
  ids/URLs. Never delete a receipt when removing an outbox entry.

Canonical JSON is UTF-8 with sorted object keys, no whitespace, unescaped
Unicode and no NaN/infinities. A bundle digest hashes the canonical sorted
list of relative file names and SHA-256 file digests; omit any digest field
from its own input. A delivery id is lowercase SHA-256 of canonical JSON of
`["delivery-v1", canonical-PR-key, generation, kind, canonical-target]`.
There is one comment id per generation, independent of how many labels
reference it; deprecation and each publication/board/projection have their
own ids. Complete intent does not mean a final wire payload: comment action,
edit target and body are frozen at first send after the live checks below.

**Acceptance transaction, under the PR lock:** write the bundle in a new
sibling temporary directory, persist referenced evidence too, fsync every
file and its directories, rename it to `generations/<gen>`, and fsync
`generations/`. Recheck the active claim;
write a temporary `current`, fsync it, rename over `current` and fsync its
parent. This promotion is the commit point. Only then update membership,
run projections and outbox indices. For every mutable record and receipt,
use temp-file/write/fsync/rename/directory-fsync; fsync the parent after
unlink too. Newly created parent directories must also be persisted.

Recovery always compares generation numbers and follows the accepted chain,
not merely the existence of a pointer:

| crash boundary | recovery |
|---|---|
| partial bundle, or complete bundle before promotion | not accepted; remove temporary data; under a matching active claim, revalidate guardian results and finish promotion, otherwise ignore the orphan |
| promotion before any or only some outbox entries | enumerate **every** intent in each accepted bundle; materialise each missing entry unless its receipt already exists |
| receipt committed before outbox removal | receipt wins; remove the redundant entry, never resend |
| new `current` before old entries reconstructed | traverse predecessors too and apply supersession below to each old intent |
| promotion before membership/run-state updates | replay idempotent projection intents; accepted bundles override a run's stale belief that inference is unfinished |
| membership head before promotion | forbidden: projection may reference only an accepted bundle; pending/deferred rows retain the previous head |
| remote side effect before local receipt | publication verifies served bytes (sampled since step 5 of the scalability plan: every tenth upload of a page or after six hours; a comment's own check still fetches every section it links to), comments reconcile delivery id, board reads target state; never infer failure from a missing receipt |

Discovery/progress/REUSE/DROPPED updates also need recovery without a new
review. Journal each as an immutable operation bundle with its membership
patch and publication intents before applying it, keyed by run/phase/PR;
use the same rename/fsync and receipt protocol. Their delivery ids use the
operation id instead of generation in the same hash tuple. REUSE references
the accepted generation and never creates another comment intent. A newly affected label
gets a projection/publication intent through this journal.

An observation ticket is allocated under the fixed counter region **before**
each discovery or candidate-refresh fetch; it is a persisted monotonic
integer, not wall time. Membership merges compare tickets per PR, preserving
unrelated rows and retaining tombstones. An older observation cannot remove
or resurrect a newer row. Projection intents for accepted heads update only
the reviewed-generation field of a still-present row, monotonically by
accepted generation; they cannot reverse a membership tombstone. Separate
CI observations retain head/time. Rendered manifest heads come from accepted
review references only, never `pending` or a claim counter. No page operation
needs a PR lock to follow immutable bundles. No tombstone/receipt/bundle GC
is part of this first implementation.

**Supersession:** acquiring the PR precedes rereading `current`, the intent
and its receipt. Unsent old comments/notes become `superseded` when a newer
bundle replaces them; never post an old verdict after a newer one. Old
`sending`/`uncertain` writes must first be reconciled, and block newer comment
writes until resolved. A confirmed old post keeps its receipt; deprecation
can only target its frozen predecessor ids, never a newer comment. Old page
intents render the current store, not the old run's HTML; a newer revision
satisfying that destination discharges them; a tombstoned row needs no old
section and is receipted as superseded. Retained generation pages are the
exception: fulfil them from their original bundles, never with newer bytes.
Old board intents reconcile the latest authoritative comment. New bundles
inherit outstanding mutable destinations and affected label projections so
replacing a generation cannot lose a
second-label refresh. Supersession is recorded as a durable receipt, not
just removal from the queue.

A page is rendered under its page lock in the current layout: manifest first
inside `<body>`, review/call date, accessible sortable contents, one section
per PR, summary, validation counts, visible heads and coverage, CI changes,
moved-during-run annotations, posting actions and held-comment commands.
Progress is beneath the previous section. Render from current membership
and accepted references, preserving unrelated rows. A run's final flush
bypasses rate limiting but has the same bounded delivery budget; failed work
remains durable and is named in its summary.

Publish via existing rsync to `REVIEW_PUBLISH`, using atomic remote file
replacement (no in-place transfer), under the destination's page lock.
Canonical page bytes are the renderer's UTF-8 output with LF newlines and
its single digest meta element omitted. SHA-256 those bytes, then insert
`<meta name="apreview-digest" content="<hex>">`. Fetching removes that exact
element and recomputes the digest. Each section also carries its PR key,
accepted review generation and section-content digest (SHA-256 of the
canonical rendered review core, excluding its digest attribute and mutable
CI/progress/delivery annotations). The receipt records
those identities as well as the page digest; HTTP success and an anchor
alone are insufficient.

Destinations: label latest; per-label archive
`DevCallReviews/<DATE>/<label>/devcall_pr_reviews.html`, dates frozen at run
creation (upcoming Tuesday/Wednesday for the call labels, Canberra today
for AIReview); followups under `DevCallReviews/followups/<DATE_TIME>/`;
per-PR pages, author pages and `RsyncReviews/index.html`. Every postable
bundle also includes a retained generation page at
`PRReviews/<owner>/<repo>/<n>/<gen>.html`, rendered solely from that bundle
and always included in its complete intent list. An empty followup
publishes nothing; an empty author or rsync run preserves the useful page.
Before sending a comment, take all required page regions in sorted,
deduplicated order, fetch each served page and verify its expected section
identity/digest, retaining those locks through the GitHub call. An earlier
receipt is not enough. If a section was dropped or replaced, regenerate
where membership permits; a destination with a newer removal tombstone is
superseded. The retained generation page must always verify and is the
comment's canonical report URL; label refreshes are still separate required
deliveries. Later label removals therefore cannot break a frozen link.
A frozen payload's URL cannot change on retry; if it no longer verifies,
hold for repair rather than send a different payload under the same id.

The legacy landing page is exactly
`DevCallReviews/<DATE>/devcall_pr_reviews.html`, with its own page lock.
It lists all labels for that date and a persisted map from old `#pr...`
fragments to label pages containing that anchor. Inline script routes a
known fragment to the same fragment on its mapped page; matching anchor
links provide a no-script fallback. For duplicate anchors choose
DevCallTopic, then DevCallEU, then AIReview, then lexical label order; retain
previous mappings while the target remains served. Missing anchors show
an explicit unavailable section and links rather than redirecting to an
unrelated PR. Landing-page updates merge the date's publication receipts,
never replace other labels with one run's list.

### Posting and the board

All drainers, including startup, completion and fifteen-minute board cron,
use the same protocol. Snapshot at most 100 due delivery ids, ordered by
next-attempt time then id; stop after 60 seconds total. Do not append newly
created work to this drain. Try each PR lock once, skip busy PRs, and after
acquisition reread the entry, receipt, current generation and dependencies.
Keep the PR lock through the side effect and durable receipt. Two drainers
may select one id, but the second must see the first's receipt or uncertain
state before deciding anything; it cannot execute its pre-lock snapshot.
Each intent specifies its execution gate: PR for generation deliveries;
page for standalone render/membership operations, which never mutate PR
claim/current state or acquire a PR. Hold that gate through its receipt.
Contention consumes no retry. A failed request
gets exponential backoff from one minute to one hour. Deliveries that redo
their whole effect from current state (page publication and annotation,
board sync) keep retrying at most hourly without end: a full publishing disk
on 2026-10-07 parked 195 entries, and 25 comments behind them, under a
give-up nobody reset. GitHub writes (comment, note, deprecate) stop after
five failures, as does a comment reconciliation finds ambiguous, and name
the debt in both dashboard and run summary; a human retry resets that
budget, not its identity or payload.

For a comment, first reconcile any earlier ambiguous write. Then fetch live
head and thread under the PR lock. If head moved, include the exact reviewed
and observed heads and record followup eligibility; never present the old
review as covering new code. Python selects post/edit/repost using the
existing policy, holds and explicit mode authorisation. A note can only
create a note or edit the same note delivery; it cannot select a verdict
comment as an edit target and never deprecates one. A new PR-mode request
gets a new generation and may deliberately repost; recovery of that request
always deduplicates the same delivery, with no PR-mode exemption.

Preserve the AI-generated marker verbatim on line 1 and the visible
`**Verdict: ACCEPT|COMMENT|REQUEST CHANGES**` line on line 2. Put the stable
identifier on **line 3** as exactly `<!-- apreview-delivery:v1:<64-lowercase-hex-id> -->`.
For a no-verdict note leave line 2 blank and use the same line-3 marker.
It is not the older `apreview:` verdict marker. Python owns these lines.

After the moved-head check, policy selection and publication verification,
freeze the final UTF-8 body, its digest, HTTP action/target id, account and
predecessor ids in the delivery record; fsync `sending` **before** the first
request. Hold page locks from verification through that request. Return
comment id/URL from `post-comments.py`; record posting and deprecation as
separate receipts, new comment first. Deprecation never changes the new
comment's delivery id. Recovery matches a paginated comment by configured
author account, exact delivery marker, target PR and frozen-body digest
(or known deprecation wrapper around those bytes), independent of head,
URL and creation time. A marker with changed bytes or multiple matches is
uncertain and requires inspection, not a fresh post.

A timeout, sender death or lost response leaves `sending`/`uncertain`. A
missing match immediately afterwards proves nothing. Reconcile on at least
two reads 60 seconds apart after a two-minute request grace period; preserve
that schedule durably across drains. A unique match records success. If
still absent, an unsuperseded delivery may retry the **same** frozen action
and bytes after revalidating its served section and head. If the head no
longer matches the frozen observed-head annotation, stop and require repair;
do not silently rewrite an uncertain payload. A superseded delivery is
cancelled after that reconciliation window, not retried. Remote writes can
outlive the window, so exactly-once remains unpromised; a later duplicate
is reported for repair and newer reconciliation considers it explicitly.

Board drain ordering is PR claim, credential leases, board lock, targeted
fetch/write, then target acknowledgement/receipt. Selection and PR claims happen outside
the board lock, including in `project-sync.sh`. Under board ownership fetch
the fresh PR and newest readable verdict across configured accounts,
insert by node id even if the label vanished or this is an unlabelled
PR-mode delivery, apply desired fields, and read them back. The returned
acknowledgement names delivery id, PR node, board item, observed comment id
and fields actually verified. Busy, failed or unknown verdict is not an
acknowledgement and remains owed. Notes preserve the last readable verdict.
The cron's ordinary finite board sweep may take credentials then board,
releasing both before the outbox drain; it never claims PRs inside that sweep. Board truth
is eventually consistent, and the dashboard displays pending debt.

### `all` mode

One controller. The three label discoveries run at once; their fixed
candidate union is deduplicated by PR key. Followup discovery starts after
label admission closes, every label PR is accepted/reused/deferred, and
one bounded final delivery drain finishes. Comment outcomes are posted,
held, not-applicable, superseded or run-local `delivery_deferred`; durable
owed work never holds the phase barrier indefinitely. Followup consults
accepted state as well as told-heads, so failed posting cannot cause the
same head to be reviewed again. No phase reopens discovery. The final
summary is one block per label plus followup, with its funnel and every
review/delivery deferral explicitly named.

## Failures

- Timeout, transport error or invalid/incomplete output: one retry in a
  fresh attempt directory, then defer. Preserve other successful passes
  when inputs still match. Never accept a result without the guardian's
  terminal record or from a fenced attempt.
- Quota exhaustion: no immediate retry. With the account lease and fixed
  quota region, write durable provider/account state (paused, scope, reason,
  observation, retry time); a lock is not the pause. Every admission checks
  it under that region after acquiring its account lease. Only a later
  successful probe clears it atomically; reboot and caller exit do not.
  Other accounts continue unless the recorded scope is provider-wide.
  Release the quota region before running the CLI. Delivery continues.
- Supervisor death: guardians finish/timeout and clean up independently;
  resume claims only free PRs and reconciles completed attempts. Live sibling
  guardians are not mistaken for proof that a dead primary is still alive.
- Guardian death: service-manager cleanup plus the previous-owner check
  fences payload/resource reuse; unknown exit means failed attempt.
- Reboot: all old-boot processes/locks are gone. Recover accepted chains,
  intent journals and receipts first, reconcile ambiguous remote writes,
  then resume unfinished claims with their frozen configuration. Remove
  disposable worktrees only after verifying no live same-boot owner; retain
  result/evidence data. Exhausted inference quota cannot bypass this recovery.

## Cost and time

The orchestrator's tokens go to zero; that was 40% of this morning's raw
tokens. Against that, reconciliation becomes a fresh session per PR and the
validation pass is kept. Measure per job kind from the canary and record it
here before any saving is claimed.

Wall-clock depends on the longest admitted review chain and queueing for
resources and delivery. Removing this morning's re-sweep alone would move
the three late PRs to the 06:13 run; a two-hour label run is a canary target,
not a guarantee derived from serial timings.

## Prerequisites and spikes

Two things must be shown to work before implementation, because the design
depends on them and nothing in it can substitute for them:

1. **Several `claude -p` processes on one account at once.** Today's
   parallelism is subagents inside one CLI; the review box has never run
   four independent CLIs on one credential file. Run four for an hour and
   watch token refresh. If the CLI's own credential lock does not survive
   that, accounts take exclusive leases, Claude parallelism becomes one per
   account, and the design's value is the Codex side plus the orchestrator
   savings. That is worth knowing before any code exists.
2. **A guardian prototype under `systemd-run --user`** with linger enabled,
   killed in every combination: supervisor, guardian, payload, and the
   box. The lock must be observed to follow the design's ownership table
   in each case.

## Testing

- The scheduler is a pure state machine over fake jobs: every transition,
  the permit pools, retry budgets, deferral, generations, resume from disk,
  rejection of superseded attempts, the outbox drain.
- The lock module against a real file: striping, one description per lock,
  disjoint kinds, deliberate same-stripe PR/page collisions, inheritance
  only into guardians, release on death/reboot, advisory headers and wait
  deadlines. Exercise a busy manual PR beside an all-run and simultaneous
  startup/board drains; no blocked PR may stop unrelated claims.
- The renderer against the page contract: manifest first in body, heads
  for accepted and reused rows only, in-progress rows beneath previous
  sections, per-label archives, legacy filename/fragment routing, canonical
  digests, and a replaced/dropped section between receipt and comment.
- Discovery and followup filters with recorded `gh` output, including the
  rebase-only and binary cases and the account and pagination rules.
- Recorded replay: captured inputs from real runs replayed through
  discovery, classification, rendering and posting decisions, with crash
  points at every acceptance boundary in the table, remote success before
  receipt, partial fan-out, superseded sending entries and receipt removal.
- Real guardians: kill supervisor, guardian and payload separately; exercise
  resume, abort, timeout, netns holders and background SITL. No resource slot
  is reused until its previous payload is empty. Test paused quota across
  reboot and cross-mode resume with different accounts/configurations.
- Result fixtures reject an ACCEPT with an unrefuted blocker and every
  missing/duplicate finding outcome. Note posting must preserve the previous
  verdict comment. Migration routing tests include manual rsync URLs and
  PRs appearing on multiple label/author pages.
- Everything in the mutation harness.
- A stub mode (`REVIEW_AI_STUB=1`) with canned results exercises the
  plumbing without inference; it does not validate depth, CLI lifecycle or
  token accounting, which the canary does.

## Migration

The old global lock protects runs, local reports, reference refresh, pause,
reaping and the dashboard's single-running-job assumption. Its replacements
are PR/page ownership, immutable store, refresh/pause regions, guardians and
explicit run/session records. Mixed operation needs more than new locks on
new code: old paths must honour the ownership boundary too.

1. **Recorded replay**, then **read-only shadow**. Old is the sole production
   writer. Shadow has separate state, reference clones and staging pages;
   comment/board writes are disabled at the adapter boundary, including
   traps. Before live shadow jobs, adapt both paths' resource admission,
   account launches/probes/cleanup and reapers to the shared protocol. If an
   old job cannot participate, drain it before starting shadow work with that
   account/resource. Evaluate coverage, false positives, delivery recovery,
   real guardian lifecycle, quota use and latency as well as verdicts.
2. **Shared infrastructure before canary.** Install one routing configuration
   used by cron, `review-now.sh`, direct wrapper invocations and discovery.
   Old code must reject new-owned targets at admission, not merely omit a
   cron line. Board and dashboard writes go through the new serialized
   adapters only; old traps cannot independently write them. Both paths use
   the account protocol. Keep separate reference clones while any old reader
   can read the mutable base; old refresh retains its old exclusion, new
   refresh uses its OFD region. Pause applies to both admission paths.
3. **First canary: rsync.** Pause admission briefly and drain existing old
   rsync/manual jobs and side effects before atomically assigning the entire
   canonical `RsyncProject/rsync` repository, its PR pages and rsync report
   to new ownership. Import its manifest. Route both `review-now.sh rsync`
   and explicit rsync PR URLs to the new path; old all/followup/author/PR
   paths exclude that repository even when selected indirectly. Before the
   handoff, remove rsync rows from old shared label/author manifests and
   pages. During canary, new rsync work publishes only transferred rsync/PR
   destinations; defer cross-owner author/label report requests until the
   full cutover. Shared board/dashboard adapters remain sole writers. Enable
   the canary cron only after these exclusions pass.
4. **Remaining modes in one drained cutover.** Do not use the mutable AIReview
   label as ownership. Stop old admission, disable old cron/manual bypasses,
   drain all old jobs, publishers, board hooks and refreshes, and import
   manifests/membership. Resolve or explicitly hold outstanding old comment
   writes before new deliveries. Switch the routing configuration for all
   remaining repositories, labels, followup, author and PR mode, together
   with every latest/archive/legacy page, to the new store/renderer. Verify
   no old writer remains, then enable new admission. This brief migration
   quiescence is not a global run lock in the new design. Runtime manual
   reviews can overlap all-runs immediately after the switch.
5. **Rollback uses the same drained handoff.** Stop admission and guardians,
   resolve uncertain writes and export compatible accepted manifests. Keep
   the new delivery/renderer adapters as sole writers for their destinations
   until every pending intent is delivered or explicitly cancelled with a
   receipt; do not let old wholesale publication or posting race them.
   Transfer ownership only after that fence. Retain bundles and receipts.
6. Retire orchestration from `commands/reviewprs.md`, keeping the four prompts
   and review rules. Remove old refresh/global-lock paths only after their
   last reader and writer have retired.

### Slice-three commands and transfer format

Installing this slice changes no ownership. The only live routing file is
`$REVIEW_ROOT/etc/routing.json`; absence means old ownership. Start with the
empty `runner/etc/routing.json.example`. Its `repositories`, `labels` and
`modes` arrays name new owners, not exclusions from new ownership. `modes`
accepts `all`, `followup`, `rsync`, `pr`, `author`; `all` transfers every mode.
For partial transfers, repositories are the PR boundary and labels/modes name
shared report destinations: transferring a label/mode alone does not authorize
old-owned repositories. Repository names are canonical, case-insensitive
`owner/repo`. Ownership of a
repository applies even in an old-owned followup or author sweep. It does not
transfer those sweeps' shared publication destinations. Use `@name` for an
author request. A repository canary defers cross-owner report requests.

On the runner, after installing the code and setting up separate full clones
under `REVIEW_NEW_REPOS` (default `$REVIEW_DATA/references`):

```bash
. "$HOME/review/bin/review-env.sh"
cp runner/etc/routing.json.example "$REVIEW_ROOT/etc/routing.json"  # first install only
loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
python3 "$REVIEW_ROOT/bin/review-route.py" rsync
```

Run the concurrent-account and real systemd guardian spikes before enabling
production overlap. Accounts default to exclusive leases; wrapper preflight,
legacy CLI lifetimes, quota probes and cleanup participate in those leases.
Legacy unregistered descendants cannot be safely killed automatically; the
handoff waits for them and times out for manual inspection.

The handoff consumes a **complete publication mirror**, not just the rsync
page. Keep it under `/data/review/`. For dry-run planning fetch a mirror first;
the actual handoff refreshes it again *after* draining, using `REVIEW_PUBLISH`
and `RSYNC_AUTH`, so an old run cannot publish a newer head between the mirror
fetch and import. If publishing is unset, `--pages` must name the actual local
publication tree. None of these commands posts GitHub comments:

```bash
mkdir -p /data/review/canary-pages
rsync -a $RSYNC_AUTH "$REVIEW_PUBLISH/" /data/review/canary-pages/
python3 "$REVIEW_ROOT/bin/review-handoff.py" new --pages /data/review/canary-pages --dry-run
python3 "$REVIEW_ROOT/bin/review-handoff.py" new --pages /data/review/canary-pages --wait 900
"$REVIEW_ROOT/bin/run-reviewprs.sh" rsync --dry-run
```

The handoff takes `pause`, waits boundedly for the old global lock, live
attempts, unregistered legacy readers and delivery debt to drain, imports
the manifest and sections, removes rsync rows from shared label/author pages,
publishes those replacements under their page locks, then atomically replaces
routing.json. It records changed pages and backups under
`$REVIEW_DATA/handoff/rsyncproject/rsync/`. A durable pending-page journal makes
a failed publication retryable even after its local mirror was rewritten.
Rerun the same command after interruption before requesting the reverse.

Imported reviews occupy **generation zero**, explicitly marked `legacy`, with
their original sections preserved. They are not fabricated successes of the
four inference passes. Import receipts prevent re-delivery of already served
pages; inherited page intents ensure a later accepted review updates the
transferred report too. No old comment is reposted by import. A missing section
or a conflicting already accepted head stops the transfer.

Replace the old rsync cron line with its adjacent commented new-path line only
after validating the canary, and enable the commented outbox drain. Nothing
installs a crontab. `REVIEW_EXPECT_PATH=new` refuses an accidentally old-owned
slot. The board sweep stays the existing shared serialized adapter.

Before rollback, abort unfinished target runs with
`review-now.sh --abort <run>`, including queued runs without attempts. A paused
controller can still create page intents, so absence of live guardians alone
is not a drained transfer. The handoff refuses to cross that boundary.

Rollback is the same fenced operation and is blocked by any outstanding debt,
including uncertain comment writes. First drain it, or resolve it with an
explicit receipt using the delivery protocol; the handoff never cancels debt:

```bash
"$REVIEW_ROOT/bin/review-outbox.sh"
python3 "$REVIEW_ROOT/bin/review-handoff.py" old --pages /data/review/canary-pages --dry-run
python3 "$REVIEW_ROOT/bin/review-handoff.py" old --pages /data/review/canary-pages --wait 900
```

Rollback exports the accepted rsync page with a compatible manifest before
returning ownership. It keeps bundles, receipts and backups; it does not
restore stale rows into shared reports. Their next old sweep rebuilds them.
The remaining all-at-once transfer (step 4) is `review-handoff.py new --full`.
It reads the same complete mirror, imports every row of the latest shared
pages (the three label pages and every author page) as generation zero, one
membership row per page, and then adds `all` to `modes`, which makes every
mode, label and repository new-owned. Dated archives and followup reports are
history; the new path writes its own. A PR on two pages keeps the first
imported section and the conflict is reported; an already accepted new-path
generation is authoritative for its row. The dry run lists every manifest key
that resolves to no configured repository and every entry without a section,
and the transfer refuses while any remain, because that review would vanish
from its page on the first republish. It needs a full reference clone under
`REVIEW_NEW_REPOS` for every repository in repos.json, submodules included.
There is no automated reverse: rolling back means restoring routing.json and
accepting that the old command rebuilds its pages from the new renderer's
manifests.

## Decisions taken

- Reconciliation is Claude. Codex does the cold pass and the validation
  pass.
- A PR with any failed pass is deferred, never posted from a subset.
- Comments post per PR as each is published; one post per review
  generation whatever the number of labels.
- Board rows feed followup eligibility, not label reports.
- No re-sweep and no end-of-run head recheck; discovery once per phase.
- Locks are OFD byte ranges in one stable file, disjoint by kind, with
  non-blocking PR claims and per-attempt guardians; no global run lock.
- Default limits: 4 Claude, 4 Codex and 4 heavy permits, machine-wide.
- Sections are markdown from the reconciler, rendered by Python; the
  marker, verdict and head lines and all tables are generated by Python.
- Archive paths become per label under the date, with a landing page at
  the old location.
- Every review generation gets a retained page under `PRReviews/`, and that
  page is the URL a comment links to; label and archive pages are refreshed
  as well.
- A comment carries an invisible delivery id on line 3, under the visible
  verdict line, for write recovery.
- The first canary is rsync mode.

## Out of scope, deliberately

- A PR labelled and then unlabelled between two discoveries, with no AI
  comment and no board row, is not reviewed. Recording such requests
  durably would be a separate feature.
- Exactly-once comment delivery is not promised by locking; it is
  approximated by intent-before-write plus thread reconciliation, which is
  what is specified.
- Full input/evidence content-addressing and automatic proof that prose
  agrees with findings are deferred. Bundle/page/payload digests, complete
  finding outcomes and verdict-versus-blocker checks are required here.

## Open questions

- Whether validation should also run against the cold pass's findings, or
  only the primary's as today.
