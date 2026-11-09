# Records per review: why the store holds millions of records, and the plan to make it hold thousands

Status: draft 2, 2026-11-07, after a Codex review (summarised at the end). Measured read-only on blu6. Companion to
`scalability-in-time-plan.md`, which mostly bounds how long records are kept.
This plan asks a different question: why there are so many records in the
first place, and which of them need to exist at all.

## The base elements

| Element | Count on blu6 | Rate |
|---|---|---|
| PRs with any delivery record | 705 | ~300-340 in each day's sweep |
| Posted review comments (≈ completed reviews) | 1,973 | ~117 inference launches/day |
| Accepted generations (bundles) | ~2,000 | ~50/day |
| Attempts | 8,766 | 4 passes per review |
| Runs | 635 | 24 `all` + 8 `followup` + 1 `rsync` per day |
| Distinct pages | 3,418 page targets | 715 followup pages (one per followup run), 41 dated copies of the AIReview label page, 44 dated landing pages |

A store sized by these would hold tens of thousands of records. It holds
2.69 M receipts (2.0 M ledger rows, 0.68 M files), growing by about
**80,000 a day**: about 700 receipts per review.

## Where the receipts come from

| Kind | Receipts | Share | What it is |
|---|---|---|---|
| publish | 1,538,212 | 57% | a page upload, recorded per (operation, PR, page) |
| projection | 1,135,419 | 42% | a membership-row merge, recorded per (operation, PR, page) |
| annotation | 10,111 | | per generation and page |
| comment, board, deprecate | 1,973 each | | per generation: the actual review output |

99.8% of the publish and projection receipts are **operation receipts**:
their `generation` is an operation digest, not a generation number.
ardupilot#34277 has 3 generations and 15,765 receipts, 3,152 of them for the
single AIReview label page.

By target, the receipts land on: the AIReview label page (783 k), its dated
copies `YYYY_MM_DD/AIReview` (783 k), per-run followup pages (657 k), dated
landing pages (401 k), and the DevCallEU and DevCallTopic pages (58 k).

Daily, on 2026-11-06: 34.8 k projection and 47.3 k publish operation receipts,
against 1.4 k generation receipts. For each of about 340 PRs, the median was
**53 distinct operations in that one day**.

## The multiplier, step by step

Every hourly run observes every PR in the sweep. That's about 340 PRs, all
open and labelled, inside the 14-day followup window. Each observation
becomes operations, `digest([run, phase, pr])` (`review-supervisor.py:261`):
discovery, then the outcome (`finish`, `review-supervisor.py:505`), with a
reuse or refresh between them on some paths. A reused PR goes through
discovery, then reuse, then finish; one deferred by the interval goes
through discovery, then finish. An `all` run has a label phase and a
separate followup phase (`review-supervisor.py:218`, `:1416`), and the same
PR can be projected in both under different operation ids. 24 `all` runs and
8 `followup` runs a day give the measured median of 53 operations per PR per
day.

For each operation, `_project` (`review-supervisor.py:244`) builds:

- for every destination page of the PR (label page, today's dated copy of it,
  the run's followup page): a **projection** intent and a **publish** intent
  that depends on it;
- for every dated page among those: a **landing publish**
  (`landing_intents`, `review-supervisor.py:1132`).

An operation with *D* destination pages left after examination and *L*
distinct landing pages creates 2*D* + *L* entries. Landing publishes are
deduplicated within the operation, and examination can drop individual
projection and publish pairs. That's why the measured effective multiplier
is about 4.5 entries per operation, not the unfiltered 6-10. Each entry gets its own receipt when it is delivered,
and the receipt is kept for ever: the ledger has no retention rule. The
multiplication is

> 340 PRs × 50 operations/day × ~4.5 entries ≈ 80 k receipts/day

(illustrative: a median times a count, not an exact accounting).

None of the factors is reviews.

Three things make the factors as large as they are, and a fourth makes
each review's own deliveries grow:

1. **Most outcomes are not examined.** `examine()` (`review-supervisor.py:325`)
   skips journalling when the page already shows the row exactly as it would
   now. Only `quiet` observations go through it: discovery, reuse, reused and
   dropped. A PR held back by the 2 h re-review interval is
   `finish(pr, "deferred", "re-review interval")` on **every run**, and
   deferral is not quiet. The same goes for an accepted same-request PR and
   for every `refresh-<observation>`. On 2026-11-06 examine merged
   50.3 k projections unchanged and journalled 19.5 k. About 15 k more were
   journalled without being examined at all.
2. **New pages every run and every day.** Each followup run gets its own page
   (`followups/<run time>`, 715 so far), and each day gets a new dated copy of
   the AIReview label page and a new dated landing page. Every PR on a fresh
   page has no "last uploaded view" on that page, so even a quiet
   re-observation journals a projection and a publish there.
3. **A publish per PR, not per page.** Uploading a page is a page-level act.
   The drain already coalesces: it rendered 11.6 k pages and rsynced 1,177 on
   2026-11-06, and 14.3 k publishes were "settled by a render". But every PR
   that touched the page still has its own publish entry and receipt. One
   upload settles dozens of entries and leaves dozens of receipts.
4. **Inherited destinations.** `inherited()` and `_promote()`
   (`review_store.py:330`, `:407`) carry every mutable destination a PR was
   ever on into each new generation. So a review's own receipts grow with the
   PR's age until dated and followup pages freeze
   (`scalability-in-time-plan.md` step 11). A generation creates
   3*D* + 4 + *L* intents before inheritance: projection, publish and
   annotation per destination, the retained publish, comment, board,
   deprecate, and the landing publishes (`review-supervisor.py:1052`). With
   three destinations and one landing page, that's 14.

Followup pages come from `all` runs too. A followup run with no `REVIEW`
candidates clears its destinations (`review_discovery.py:658`, `:750`), so
"8 followup pages a day" is a ceiling, not a constant.

**A bug found on the way** (Codex reproduced it in memory): discovery and an
outcome projection share one ticket, and `_merge_membership`
(`review_store.py:681`) updates `progress` only for a strictly newer
ticket. So with no claim to override it, a deferred outcome can go on
showing `discovery`.

## Content inside records

The ledger is 4.6 GB again, 2.1 GB of it added since the 2026-11-05 vacuum.
**4.1 GB of it is the `anchors` list** inside publish receipts: every
section id on the page, copied into every publish receipt of that page
(1.55 M receipts × ~2.6 KB). That's page content stored in a record. Its only
reader is the landing backfill scan (`review_delivery.py:170`), and the
landing index (`landing/<endpoint>/<date>/<label>.json`) already holds the
same list once per page.

The same pattern appears outside the ledger:

| Record | Size | Content copied into it |
|---|---|---|
| `runs/*/run.json` | 10.6 GB, ~17 MB each | each of ~180 candidates carries its own copy of the 29 KB run configuration; `configuration.json` already holds it once per run |
| `generations/*/bundle.json` | median 221 KB, max 2.4 MB | `inputs` (thread, rules), `reconciliation_snapshot`, `results` copied in full per generation |
| `attempts/*/job.json` | 2.1 GB, ~100 KB each | the same inputs again, per attempt (four per review) |

These are content-in-records, but bounded by reviews and runs. The ledger's
`anchors` are not: they grow with operations.

## What actually has to be recorded

| State | Natural key | Bounded by |
|---|---|---|
| What a page shows | membership row per (page, PR) | pages × PRs on them |
| Whether a page is uploaded | `pages/<digest>/confirmed.json`, revision | pages |
| A review's deliveries (comment, board, deprecate, annotations, its retained page) | receipt per generation intent | generations × ~6 |
| Ordering of observations | the ticket on each membership row | — |

The operation receipts exist for crash-safety: a journal is fanned out, and
its receipts stop a resumed run or recovery from fanning it out twice.
`scalability-in-time-plan.md` §"Who reads receipts" lists the readers. But a
projection merge is idempotent under the ticket fence (an older ticket never
overwrites a newer row), and a page publish is idempotent by revision. For
both, the right record is the state itself, not a log of every time it was
set.

## Plan

Each step is one commit per subsystem, with its own check. The order follows
the Codex review: the cheap, output-equivalent steps first, then the page
policy, then one combined protocol change with legacy compatibility, and
deletion last.

### R1: examine every outcome projection

Pass `quiet=True` for `deferred`, for `accepted` with an unchanged claim,
and for `refresh-*`, so they journal only when the rendered row differs from
the page's last confirmed upload at the same destination and epoch. That
comparison is what `examine()` (`review-supervisor.py:325`) does, under the
page lock and against the live claim. `finish()` saves a deferred claim
before projecting it (`:512`), so the comparison decides whether a claim
change matters. A claim change does not always change the view: a displayed
generation at least as new as the claim renders `accepted` whatever the
claim's status (`review_render.py:188`). When the output really is
unchanged, suppressing the journal is correct.

Fix the equal-ticket progress bug above in the same change: an outcome must
be able to replace its own discovery's `progress`, while an older ticket
still cannot replace anything.
*Check:* add a metric of intents actually journalled, split by outcome,
destination and examination result. The existing "projection journalled"
counter counts only examined projections (`:354`), so turning examination
on changes what it measures. Tests: claim allocation, deferral and
acceptance; a busy page; changed destinations; an epoch from a failed
upload; two consecutive interval deferrals, the second journalling nothing;
a deferred PR shows `deferred`, not `discovery`.

### R2: landing migration, then no page content in receipts

`scalability-in-time-plan.md` step 6: backfill the landing index for every
date and every endpoint, with a cutover marker, while every delivery writer
is excluded, controller delivery included. Then remove the receipt fallback
(`review_delivery.py:163-190`), add `anchors` to `Store.PAGE_COPY`, slim the
files and the ledger with `review-slim-receipts.py`, and VACUUM. Dated label
uploads already write their anchors into the index (`review_delivery.py:306`).
*As built:* `review-landing-migrate.py` also writes `landing/.migrated` last,
covering endpoints with no dated label upload before the cutover. The
render refuses a landing page until either marker exists (no fallback: new
receipts have no anchors to fall back on). The slim script keeps anchors
until `landing/.migrated` exists.
*Check:* measured ledger size, expected ~0.5 GB; a landing page rendered
after the cutover lists the same labels as before it.

### R3: stable pages; snapshots only for events (product decisions)

Daily or per-run pages make membership grow with PR-days, reviewed or not.
Records only become proportional to reviews if the pages people follow are
stable, with snapshots made only for real events:

- **Followup:** one stable followup page instead of one per followup run
  (715 so far). If a history is wanted, snapshot it when a run finds something
  to follow up. Already decided: old followup pages expire after 30 days or
  more.
- **AIReview:** the stable label page only. Make dated copies, as for
  DevCallEU and DevCallTopic, only for labels with a call and only on call
  days, not daily (41 so far).

These apply to future observations and generations only. Existing pages and
links stay, frozen under `scalability-in-time-plan.md` step 11, whose freeze
predicate has to be restated for the new page identities. Freezing bounds
inherited intents per generation at 3*D* + 4 + *L*; it does not bring that
number down to 6.
*Check:* new pages per day, membership rows per day, intents per new bundle.

### R4: page maintenance as durable page state, not deliveries

This replaces the per-(operation, PR, page) projection and publish entries,
and their receipts, with state owned by the page. It is one protocol change,
because the projection half and the publish half are only safe together.

1. **Page state.** Under the page lock, a merge writes the ticketed
   membership rows and a desired version for the page, together, so a
   committed merge cannot exist without recoverable publication debt.
   Desired version is a counter of its own, not `revision.json`, which counts
   successful uploads (`review_delivery.py:283`). Anything else that changes
   the rendered page also raises it, because membership alone does not decide
   the bytes: claims, and comment receipts through annotations, do too (see
   the rerender in `review_store.py:1014`).
2. **Publication.** A page worker renders and uploads current state under the
   page lock, as the drain does now (`review_store.py:1182`). It records
   destination, epoch, digest and the desired version the upload covered.
   Debt is desired > covered, *per destination*, so a configuration change
   cannot lose an outstanding upload to the old destination
   (`review_store.py:992`). A queue entry is only a scheduling hint:
   recovery finds debt by scanning page state, so a lost entry costs latency,
   not correctness.
3. **Landing.** A dated label upload updates its landing index and raises the
   landing page's desired version before it records its own coverage, so a
   crash cannot leave a complete index with a stale landing page
   (`review_delivery.py:306`). Landing locks are never taken while label locks
   are held; they are taken afterwards, in region order (`review_lock.py:137`).
4. **Journals** stay only for unfinished fan-out, and are unlinked once every
   target page has committed its merge and debt. Replay is idempotent under
   the ticket fence. Removal and `unreachable` tombstones stay, so a replayed
   journal cannot resurrect a row.
5. **Generation deliveries keep their receipts and barriers.** These are the
   retained publish, comment, board, deprecate and annotations, plus the
   generation's own page publishes. A receipt still proves a publication made
   after its dependencies settled, not merely that some older upload exists.
   Generation projections become self-contained before they drop their
   dependency on the discovery operation's projection. Today they carry only
   `{"generation": g}` (`review-supervisor.py:1115`), which on an absent row
   merges into the default removed row (`review_store.py:679`). Without the
   dependency, a listing could publish, and the comment be posted, before
   discovery has added the row. So either the patch carries a complete
   ticketed observation, or the receipt waits for a row watermark proving
   the discovery observation was applied or superseded.
6. **Compatibility.** Existing journals, outbox entries, prepared claims and
   immutable bundles name receipt dependencies, and the old path keeps
   delivering those under their format version until they are drained.
   Consumers that tie debt to one PR are updated, `review_handoff.py:84`
   among them. `drain.lock` is not a substitute for page locks: controllers
   drain without it (`review-supervisor.py:1306`).
*Check:* the crash-point replay suite the store tests already use, extended
to every boundary: merge before debt, debt before queue, upload before
coverage, coverage before queue removal, label index before landing debt.
Then operation receipts per day go to zero, and publish receipts per day to
generation publishes only.

### R5: delete the operation receipts that already exist

This runs only after legacy work has drained, not after a fixed time. Delete
operation receipts, both files and ledger rows, under
`scalability-in-time-plan.md` step 8's exclusions and its coordination rule
(exclusion sets built under the fence): outbox dependants, live journals,
prepared claims, unreceipted chain dependencies, runs not complete, and any
legacy bundle dependency still unsettled. Then VACUUM.
*Check:* "the ledger holds generation receipts only" is a completion
condition, so demonstrate it with a count. Test that no deletion can
recreate an outbox entry.

### R6: one effective configuration per run

The supervisor enriches the configuration after reading it: presentation,
prompts, date and stamp (`review-supervisor.py:122`). Persist that
effective configuration once per run, with its digest, and have candidates,
claim inputs, jobs and bundles carry the digest instead of a copy. Every
reader resolves it before digesting or using inputs: input digests
(`:455`), job creation (`:862`), and delivery's frozen configuration
(`review_delivery.py:625`). Old records keep their inline copies.
*Check:* `run.json` median size halves; a resumed run and a recovery
promotion use the same configuration bytes as before.

### R7: content-addressed blobs (later, separate migration)

Large inputs go to `blobs/<sha256>`: thread, rules, reconciliation
snapshot, bundle `results`, and job `primary_result`/`results`. Records hold
the hash. Deduplication follows identical content, not identical head,
because a thread changes without a push. Requirements:

- install each blob durably before any record names it;
- verify its hash when it is loaded, because `bundle_digest` covers only the
  files inside the generation directory (`review_store.py:127`);
- add `blobs` to GC's `OWNED` (`review-gc.py:37`), or GC deletes the tree as
  litter (`:530`);
- keep blobs by reachability from claims, attempts and generation chains.

It is bounded by reviews already, so it comes after
`scalability-in-time-plan.md` step 12.

## Expected effect

| Measure | Now | After R1-R5 |
|---|---|---|
| Receipts written per day | ~80 k | generation intents only: ~50 generations × ~14 ≈ 0.7-1.5 k |
| Ledger rows | 2.0 M (+80 k/day) | ~30 k (∝ reviews) |
| Ledger size | 4.6 GB | well under 0.1 GB |
| Receipts per review | ~700 | 3*D* + 4 + *L*, ~14 (bounded by freezing, not growing with PR age) |
| Page records | 3,418 pages, rows ∝ PR-days | stable pages plus event snapshots: ∝ PRs + events |
| Page uploads per day | 1,177 | unchanged or fewer |

The residual growth is proportional to PRs, reviews, required page
memberships and outstanding work.

## Codex review (2026-11-07)

Codex confirmed the diagnosis: the store records repeated observations and
page maintenance as deliveries, so it grows with sweeps, not reviews. Its
corrections are folded in above:

1. The operation arithmetic was incomplete. It had to account for label and
   followup phases inside `all` runs, and for paths that differ by outcome.
   The entry formula is 2*D* + *L* after examination.
2. Inherited destinations make each review's own receipts grow with PR age;
   the analysis had left them out.
3. R1: "a changed claim is a changed view" was wrong, the proposed metric
   could not measure the change, and the equal-ticket progress bug was found.
4. R2 (now R4) had no persistence protocol. It needed recovery at each crash
   boundary, a desired version separate from `revision.json`, destination
   fencing, durable landing notification, and the existing lock order.
5. R3 (now part of R4): ticket ordering cannot replace the discovery
   dependency. A generation patch on an absent row merges into the default
   removed row, so a listing could publish before discovery adds the row.
6. R5: deletion needs legacy dependency closure, not just elapsed time.
7. R6 (now R3): a daily page still grows with PR-days, so the pages people
   follow need to be stable, with snapshots only for events.
8. R7: configuration deduplication is separate from blob storage. Blobs need
   durable install, verification on load, reachability retention, and an
   entry in GC's `OWNED`.
9. The per-generation intent count is 3*D* + 4 + *L* (14 for three
   destinations and one landing), not 6.
