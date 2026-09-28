# Review supervisor: orchestration in Python, inference per PR

Status: design, revision 3, 2026-09-28. Nothing here is built yet. Revisions
1 and 2 were reviewed by Codex (reviews kept under
`/data/review/supervisor-design/` on the development machine); this
revision answers the second review. Out-of-scope decisions are listed at the
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
session and the kept validation pass add some back. The certain gain is
wall-clock. Cost per job kind is to be measured from the canary and recorded
here before any saving is claimed.

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

One lock file, `$REVIEW_DATA/locks`, created once and never replaced (a new
inode would be a new lock domain). Byte-range locks in it, Linux
open-file-description locks (`F_OFD_SETLK`). Every lock is a 32-byte region:

| bytes | field |
|---|---|
| 0-3 | magic and layout version |
| 4-7 | 32-bit hash of the key |
| 8-11 | holder pid |
| 12-19 | holder process start time from `/proc/<pid>/stat` |
| 20-23 | kind: `pr`, `page`, `board`, `refresh`, `permit`, `run`, `pause` |
| 24-31 | boot id prefix, so a header from before a reboot is recognisable |

**Striping, not allocation.** A key's region is its hash modulo a 2^20-entry
table, a 32 MiB sparse file, full stop. Two keys that collide share a lock
and serialise; that is harmless and needs no bookkeeping. There is no
fallback to a neighbouring slot: a fallback whose slot choice depends on who
happens to hold a lock lets two holders of one key coexist once the first
occupant leaves. The header's key hash is diagnostic only.

**One open description per lock.** Each lock is taken on its own `open()`
of the file. Locks on one description do not exclude each other and are
released together, so a description shared between two PRs would hand both
locks to whichever job inherited it. Descriptors are passed to a job
explicitly (`pass_fds`), only the PR's own lock, only to that PR's jobs.

**Held as long as any holder lives.** The supervisor takes a PR's region
and spawns the job with the descriptor inherited; the lock persists until
the last of them exits. A supervisor that dies leaves its running job
holding the PR, so a restarted supervisor waits rather than duplicating it.
A job that dies leaves the supervisor holding the PR, so it can reconcile
and post, or defer. Verified on this kernel: inheritance through exec,
persistence when the parent closes its descriptor, immunity to closing an
unrelated descriptor on the file, release when the last holder dies,
independence of regions.

**The header is advisory.** Whoever can take the lock owns the region. The
header lets a waiter and the dashboard say who to look at, and lets a
reader recognise a pre-reboot header by its boot id. A lock probe answers
"is the resource still owned", nothing more: a job's liveness comes from
its attempt record (pid, start time, cgroup), never from the PR lock, since
the PR may be held by the cold job or the supervisor while the primary is
dead.

**Waiting.** `F_OFD_SETLK` is non-blocking; waits are polled with backoff
and every wait has a deadline set by the caller. Lock order, when a process
holds more than one: `run`, then `pr` regions in offset order, then
`page` regions in offset order, then `board`; `refresh` and `permit`
regions are taken only while holding nothing that another run could wait on
except its own `pr`. A page or board operation never takes a PR lock.

Keys:

- `run:<run-dir>` - one controller per run; `--resume` takes it first.
- `pause` - held by `pause-runs.sh` (which today holds the global run
  lock); the scheduler refuses new admissions while it is held and says so.
- `pr:<owner>/<repo>#<n>` - from claim until the PR's review is accepted or
  it is deferred or failed, and again around each delivery step.
- `page:<destination>` - each published destination, including the runs
  dashboard page.
- `board` - the project board write, replacing the `flock` in
  `project-sync.sh`, which also learns to exit non-zero on contention and
  on failure so its caller can record the truth.
- `refresh` - the reference-clone refresh, exclusive, held for the whole
  refresh; worktree creation takes it shared. Worktrees are
  `git worktree add` from the reference clone, so their commits are pinned
  against gc, and jobs read only their worktree, never the base checkout
  the refresh resets.
- `permit:heavy:<i>`, `permit:claude:<i>`, `permit:codex:<i>` for `i` in
  `0..N-1` - machine-wide budgets, shared by every run. A heavy permit is
  taken by a wrapper before the build starts and released when the
  attempt's cgroup is empty, not when the wrapping command exits, since
  SITL backgrounds work. Nested wrappers reuse the permit already held by
  their attempt rather than taking a second. Each permit wait has a
  deadline; a job that cannot get one is a failed attempt, not a hang.

A small CLI, `review-lock.py hold <key> -- command...`, takes a region,
writes the header, and execs the command with the descriptor inherited.
`project-sync.sh`, the publish step, the refresh and the heavy wrapper use
it. The global run lock in `run-reviewprs.sh` goes away; what it protected
is inventoried in the migration section.

## Components

All under `runner/bin/`, wrapped by `run-reviewprs.sh`, which keeps account
and quota selection, the permission pre-flight and dashboard reporting, and
gains `--resume <run-dir>`. Its exit trap and the reaper are re-scoped to the
run's own cgroup: killing the supervisor does not kill its jobs (they are
meant to survive and finish under their PR locks), and the reaper acts only
on attempts the run's state says are dead. The launch settings that today go
to the one `claude -p` invocation (model pin, effort, permission mode,
granted directories) are passed to every Claude job by the supervisor.

Which failures end a run and which preserve its jobs: a supervisor crash or
kill preserves jobs; `--resume` reattaches. An explicit abort
(`review-now.sh --abort <run>`) takes the run lock, kills the run's cgroup,
and marks in-flight attempts failed. Reboot ends everything; recovery is
described under failures.

### `review-supervisor.py <mode> [--resume DIR]`

Modes are the ones the command has today: a label (`AIReview`,
`DevCallTopic`, `DevCallEU`, or any label name), `followup`, `rsync`, an
author, a PR reference, and `all`, resolved with the same precedence as
today. Whether a mode posts comments, and to which repositories, comes from
the same rules as today: the comment-posting labels post, report-only labels
and author sweeps do not, and `repos.json` holds per repository.

Each run gets `$REVIEW_DATA/runs/<mode>-<stamp>/` holding the frozen run
parameters (mode, archive date, boot id, account identities), one state
file per PR, and every job attempt's inputs and outputs. State files are
written atomically and carry attempt ids; a result from a superseded attempt
is rejected. On start, and before its own discovery, every supervisor drains
the outbox (below) and resumes any run directory whose `run` lock is free
but whose state is not terminal.

### Discovery (`review-discover.py`)

Runs once per discovery phase, against a snapshot of heads; the admitted
inputs are immutable. Output: a candidate list with, per PR: repository,
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
  comparison decides `REVIEW` versus `REUSE`; a PR in the manifest and no
  longer labelled is `DROPPED`.
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
first, which is what the deferral rule means across repositories.

There is no re-sweep during or at the end of a run, and no end-of-run
recheck of heads that were skipped as unchanged. Both were added after a PR
labelled in the same minute as discovery was missed on 2026-08-30. With
all-runs every six hours and followups every three, and with
`review-now.sh` able to start at any time because there is no global lock,
the next run catches anything still eligible. This morning's re-sweep cost
1h50m and held the followup behind it to its timeout. The promise is about
a PR's latest eligible state, not every head it passed through.

### The scheduler

Per PR, four independent tracks:

```
review:    pending -> claimed -> reviewing -> reconciling -> accepted(gen)
                                    |             |
                                    +-> deferred  +-> deferred
review:    pending -> reused | dropped
publish:   per destination: owed -> published(revision)
comment:   owed -> posted(id) | held | not_applicable | failed
board:     owed -> synced | not_applicable
```

`claimed` is taking the PR lock and allocating the next **review
generation** for the PR under it. `reviewing` runs the primary review and
the cold pass at once; the validation pass starts when the primary review
finishes; `reconciling` starts when all three have finished. If any of the
three fails after its retry, the PR is `deferred`: named in the report and
summary, previous section carried over, manifest head left alone. Nothing
is reconciled from a subset.

`accepted(gen)` writes the immutable result for that generation and, in the
same step, the outbox entries for its deliveries; then the PR lock is
released. Only the current generation of a PR can be accepted: an attempt
from an older generation that finishes late is discarded. The manifest
records the reviewed head for every accepted PR and keeps the existing head
for `reused` ones.

Delivery order per PR: publish every destination owed for it and verify
each; post the comment (once per review generation, referenced by every
label's row, so a PR under two labels is posted once); sync the board. Each
step re-takes the PR lock while it acts. A held repository reaches `held`
with its posting command recorded for a human; report-only modes reach
`not_applicable`. A failed step stays `owed` in the outbox and is retried by
the next supervisor, up to a bounded number of attempts, after which the
run summary and the dashboard name the PR and a human is told.

Limits: the machine-wide `permit:claude` and `permit:codex` pools (default
4 each, in `local.conf`) with one of each reserved for reconciliation so a
PR's finish is never starved by new primaries; and the heavy permits for
builds and SITL. Each job has a wall-clock timeout (defaults: review 90
minutes, cold and validation 30, reconciliation 45); its process tree is in
its own cgroup scope with CPU, memory and task limits as today, and timeout
or completion kills the scope.

The run has a deadline for admission; PRs not admitted by it are deferred.
Waits on locks, publication and board calls each have their own deadlines,
and a run whose review work is complete finishes by leaving delivery in the
outbox rather than waiting on it.

Isolation: one worktree per job attempt (not per PR: the primary and the
cold pass both build and run experiments), created with `git worktree add`
from the reference clone under the shared `refresh` lock, never a partial
clone, each with its own sibling build-logs directory, removed when the
attempt ends. One network namespace per job for anything that binds ports,
via the existing `netns-run.sh`, with dependencies fetched before entering
it, and the namespace holder inside the attempt's cgroup. The reconciler
gets a clean worktree plus the passes' evidence artefacts, which are
written under the attempt directory, never inside the worktree.

### The inference jobs and their contract

Each job runs in its own attempt directory with `REVIEW_JOB_DIR` set, is
given `job.json`, and must write a result file the supervisor validates
against a schema before accepting it. Invalid output, an unknown field, an
identity that does not match the input, duplicate finding ids, or an
incomplete status is a failed attempt.

`job.json` carries: schema version, run, job, attempt and generation ids,
repository, PR node id, number, full head, base and merge-base, the diff
snapshot, the thread snapshot, the previous comment and its findings with
told-head, the mode, the resolved house-rule text, the worktree path, and
the job kind. The cold pass's input excludes the primary's results.

Every result carries the same identity fields, a `status` of `complete` or
`incomplete` with the gaps named, and a `heavy` flag stating whether it
built or ran SITL (informational; the permit is what enforced it). The
supervisor records the process exit and timeout independently.

**Primary review** (`claude -p`, the review prompt): `review.json` with a
verdict; findings each with a namespaced id (`primary:F1`), kind BUG /
ISSUE / NOTE, severity rationale, claim, location (file, line, side of the
diff, revision) or an explicit non-line-specific marker, status VERIFIED /
UNCONFIRMED, and evidence as commands run, exit status, observed result, and
retained artefact paths; a list of what was checked and found clean; and for
a followup, a disposition RESOLVED / STILL OPEN / DISPUTED for every finding
of the previous round, engaging with any author reply. Title and thread are
re-read from the snapshot before any finding that depends on them.

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
only, and a one-line summary.

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

Three kinds of durable record under `$REVIEW_DATA/`:

- `results/<owner>/<repo>/<n>/<gen>.json` - an accepted review, immutable:
  head, verdict, section, comment body, evidence paths, CI observation, and
  the labels it belongs to. `results/<owner>/<repo>/<n>/current` names the
  promoted generation and is only ever advanced under the PR lock.
- `membership/<label>.json` - for each label, the PRs it currently lists
  with their manifest heads, classification and CI observation, written by
  the run that observed them, with the observation time, so a run with an
  older snapshot never removes or resurrects a row a newer one set.
- `outbox/<id>.json` - one owed delivery: kind (publish, comment, board,
  label refresh), target, the result generation, the frozen payload, and
  the attempts so far. Written in the same step as the accepted result,
  removed when the step's receipt is recorded.

Write ordering on the result path: result file, then `current`, then the
outbox entries, each fsynced. Recovery treats a result without a `current`
pointer as not accepted, and a `current` without outbox entries as owed
everything.

A page is rendered from the store under its page lock, in the layout the
command produces today: manifest comment first inside `<body>`, review date
and the call it is for, contents table with sortable headings, one section
per PR, summary table, Codex validation line, visible reviewed heads and
reviewer coverage per section, CI-change and moved-during-run annotations,
posting-action and held-comment commands. Rows for PRs a run has claimed but
not finished render as their state beneath the previous review's section,
never instead of it. Every run ends with a final render and publish of each
page it touched, not subject to the rate limit.

Publishing is the existing rsync to `REVIEW_PUBLISH`, per destination, each
with its own receipt naming the page revision (a digest of the rendered
page) and verified by fetching the page and finding both the PR's anchor
and that revision's digest in it. A comment is released only against a
receipt for a revision whose digest contains its section. Destinations:
each label's latest; its dated archive, owned per label as
`DevCallReviews/<DATE>/<label>/devcall_pr_reviews.html` with the date frozen
at run start (upcoming Tuesday for `DevCallTopic`, upcoming Wednesday for
`DevCallEU`, Canberra time; today for `AIReview`); a landing page at the
legacy `DevCallReviews/<DATE>/` location listing every label published that
day, since one dated URL used to hold more than one label; followups under
`followups/<DATE_TIME>/`; the runs dashboard, which gets its own page lock
and atomic local render. An empty followup publishes nothing; an empty
author or rsync run does not overwrite a useful page.

### Posting and the board

A comment delivery: re-take the PR lock; fetch the PR's current head and
compare with the reviewed head (if it moved, the comment says so and the PR
is queued for the next followup rather than asserting a stale head); then
a one-entry plan for `post-comments.py` with the head, the frozen body, the
hold list from `repos.json`, and the note type where applicable.
`post-comments.py` records the id of the comment it posted or edited and
returns it; posting and deprecation are two recorded steps, new comment
first.

Ambiguous outcomes: the intent is in the outbox before the write. A sender
that dies during the write cannot cancel it, so on retry the thread is read
first and a comment of ours carrying the same reviewed head and report URL,
posted after the intent's time, is taken as that delivery. PR mode, which
reposts identical text on purpose, is exempt from that match only within
the same run.

The board sync runs under the `board` lock after each post; a skipped or
failed sync leaves the board entry `owed` in the outbox, and the fifteen-
minute cron sync, which now also drains board entries from the outbox,
repairs it. The board is eventually consistent, and the dashboard says so.

### `all` mode

One supervisor process. The three label discoveries run at once; a PR under
two labels is one review generation written to both labels' membership.
Followup discovery starts after label admission has closed and every label
PR's comment track has left `owed` (posted, held, not applicable, failed,
or the PR deferred), so that followup sees the current told-heads and does
not re-review what the labels just did. The combined summary at the end is
one block per label plus followup, including the funnel of what followup
considered and why each was skipped.

## Failures

- Job timed out or wrote an invalid or incomplete result: one retry in a
  fresh attempt directory, then the PR is `deferred`. Retries distinguish
  quota exhaustion, transport failure and malformed output; quota exhaustion
  for a provider stops further admissions for it machine-wide (a `pause`
  sub-region per provider, held until the next quota reading clears it),
  rather than retrying into the same wall.
- Supervisor killed: state is on disk, jobs keep their PR locks while they
  run. `--resume` takes the run lock, and for each in-flight attempt waits
  for its PR lock to free, then takes it, reads what the attempt wrote, and
  either accepts it (if the generation is still current and the result
  valid) or starts a new attempt.
- Reboot: every lock is gone, every job is gone, the boot id differs. The
  next supervisor to start drains the outbox with ambiguous-outcome
  handling, resumes unfinished runs, discards worktrees, and continues.
- Account coordination: the OAuth stale-lock cleanup in `review-env.sh`
  scans and deletes; with overlapping runs that must happen under an
  account-level region so it cannot remove a live launch's lock.

## Cost and time

The orchestrator's tokens go to zero; that was 40% of this morning's raw
tokens. Against that, reconciliation becomes a fresh session per PR and the
validation pass is kept. Measure per job kind from the canary and record it
here before any saving is claimed.

Wall-clock becomes the slowest PR plus its validation and reconciliation,
subject to the permit pools and any waits on shared pages and the board.
For this morning's batch, with the three late PRs labelled at 00:37, 00:38
and 02:24 and no re-sweep, the label run would have finished in about two
hours and the late three would have been the 06:13 run's work.

## Testing

- The scheduler is a pure state machine over fake jobs: every transition,
  the permit pools, retry budgets, deferral, generations, resume from disk,
  rejection of superseded attempts, the outbox drain.
- The lock module against a real file: striping, one description per lock,
  inheritance through fork and exec, release on holder death, header
  advisory-only, lock order, wait deadlines.
- The renderer against the page contract: manifest first in body, heads
  for accepted and reused rows only, in-progress rows beneath previous
  sections, per-label archive paths and the landing page, digest in page.
- Discovery and followup filters with recorded `gh` output, including the
  rebase-only and binary cases and the account and pagination rules.
- Recorded replay: captured inputs from real runs replayed through
  discovery, classification, rendering and posting decisions, with crash
  points injected between every side effect and after every fsync.
- Everything in the mutation harness.
- A stub mode (`REVIEW_AI_STUB=1`) with canned results exercises the
  plumbing without inference; it does not validate depth, CLI lifecycle or
  token accounting, which the canary does.

## Migration

The global run lock in `run-reviewprs.sh` currently protects: one run at a
time, the local report file, the reference-clone refresh, pausing via
`pause-runs.sh`, the exit trap's cleanup and reaper, and the dashboard's
notion of "the running job". Each has a replacement above: PR and page
locks, the result store, the `refresh` lock, the `pause` region, per-run
cgroup scoping, and per-run session attribution.

1. **Recorded replay** of the deterministic parts with fake external
   services.
2. **Read-only shadow**: the old path stays the only writer. The new path
   runs from cron with its own state, its own reference clones, publishes
   to a staging URL, and has posting and board writes disabled at the
   adapter boundary, including in the exit trap. The old reaper is taught to
   leave the new path's cgroup alone before this step. Compare pages and
   verdicts on identical heads.
3. **First canary: rsync mode.** Its repository, page and manifest are
   disjoint from every other mode, so nothing in the old path can select
   its PRs. It is removed from the old cron line.
4. **Second canary: AIReview.** The old path is told, in every mode
   including followup, to skip any PR carrying the `AIReview` label, so a
   PR under two labels has one writer. The old path keeps its global lock
   for its own modes; the new path takes PR locks. The old refresh script is
   replaced by the locked one before this step, since old jobs would
   otherwise run while only new locks are held.
5. **Expand**: the other labels, followup, author, then PR mode. Review
   artefacts and delivery receipts survive a rollback, and the old path can
   read the new manifests.
6. Retire the orchestration parts of `commands/reviewprs.md`, keeping the
   review rules as the four prompts.

## Decisions taken

- Reconciliation is Claude. Codex does the cold pass and the validation
  pass.
- A PR with any failed pass is deferred, never posted from a subset.
- Comments post per PR as each is published; one post per review
  generation whatever the number of labels.
- Board rows feed followup eligibility, not label reports.
- No re-sweep and no end-of-run head recheck; discovery once per phase.
- Locks are OFD byte-range locks in one file, striped, one description per
  lock; no global run lock.
- Default limits: 4 Claude, 4 Codex and 4 heavy permits, machine-wide.
- Sections are markdown from the reconciler, rendered by Python; the
  marker, verdict and head lines and all tables are generated by Python.
- Archive paths become per label under the date, with a landing page at
  the old location.
- The first canary is rsync mode.

## Out of scope, deliberately

- A PR labelled and then unlabelled between two discoveries, with no AI
  comment and no board row, is not reviewed. Recording such requests
  durably would be a separate feature.
- Exactly-once comment delivery is not promised by locking; it is
  approximated by intent-before-write plus thread reconciliation, which is
  what is specified.
- Input and artefact digests, and semantic cross-checks between fields of
  one result, are not required: Python generates every line that could
  disagree with the structured result.

## Open questions

- Whether validation should also run against the cold pass's findings, or
  only the primary's as today.
- The bound on delivery retries before a human is told, and whether the
  telling is the dashboard, the run summary, or both.
