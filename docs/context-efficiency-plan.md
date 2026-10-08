# Pass context efficiency

What fills an inference pass's context, and a plan to shrink it without
losing review quality. Measured on blu6 over the six runs from
followup-20261004_224702 to followup-20261005_074702: 30 Claude primary
and 27 Claude reconciliation passes, with the matching Codex cold and
validation passes. Revised after Codex's review of the first draft.

## Measurements

A pass costs roughly its number of model requests times its average
context: each request re-sends the whole context. Within a session the
caches already hit 95-97%; the cost is how large the context grows and
how many requests re-read it.

| Pass | Requests (median / p90) | Peak request input, tokens | Summed request input | Tool output returned, chars (median) |
| --- | --- | --- | --- | --- |
| Claude primary | 35 / 56 | 114k | 2.76M | 127k |
| Claude reconciliation | 20 / 26 | 108k | 1.49M | 135k |
| Codex cold | 26 / 37 | 139k | 2.43M | see note |
| Codex validation | 24 / 38 | 138k | 2.07M | see note |

Definitions: a request is an observed usage-bearing call (a Claude
assistant message with distinct usage, a Codex `token_count` record);
failed or retried calls that leave no usage are not counted, and the
Codex count is provisional until step 1's parser settles repeated
records. Request input is input plus cached plus cache-creation tokens
for Claude, and `input_tokens` (which includes cached) for Codex. Every
column is the median across passes of that pass kind; summed request
input is context volume, not cost. Codex tool output totals are not quoted: its
session log appears to record outputs twice and the duplication has not
been shown to be uniform, so only Claude output shares below are used.

### What the logs show (observations, not additive savings)

These overlap: one command can be a JSON dump, carry the diff, and
trigger a saved-output read. They show where to look, not how much each
change would save.

1. **job.json is read through improvised scripts.** It is 113 KB median
   for primary passes and 201 KB for reconciliation, up to 1.1 MB. Most
   passes start with `python3 -c "import json;j=json.load(open('job.json'))..."`
   and dump `results`, `thread`, `diff` or `previous_comment` the same way.
   Much of that output is content the pass needs; the cost is repeated
   and oversized dumps, and the turns spent writing extraction code.
2. **Saved oversized outputs are read back (Claude).** An output over
   Claude Code's inline limit is replaced by a preview and saved under
   `~/.claude/projects/`; 50 such outputs were then read back with Read or
   `cat`, 851k chars in reconciliation and 301k in primary. How much of
   each was already in context through the preview is not yet measured.
3. **Diffs are paged ad hoc.** The diff arrives as a JSON string, so each
   pass extracts and splits it itself (`cold-evidence/diff-parts/*.diff`)
   before paging with `sed -n` and `cat`.
4. **The result schema is learnt from source** (`cat review_schema.py |
   head -400`).
5. **Re-sourcing the environment.** Claude prefixes many commands with
   `source ~/review/bin/review-env.sh`. This is a correctness bug more
   than a cost: review-env.sh resets `TMPDIR` to shared store scratch and
   prepends the mutable base checkout's `Tools/autotest`, undoing the
   attempt's own setup (`runner/bin/review_inference.py:77`). Codex
   commands often begin `pwd; printenv REVIEW_JOB_DIR ...`.

### Prefix sharing across passes

Both CLIs share about 12k tokens of opening context between passes
(11,919 Claude, 12,288 Codex). After that each Claude pass writes about
20k tokens to the 1-hour cache and each Codex pass sends about 8k
uncached. Probe calls showed the per-pass part is mainly the CLI's own
environment block (working directory, and for Claude about 10k of git
context in a worktree); our prompt is small and already static-first.
This is a few percent of a pass and is left until last.

## Plan

Each step lands separately so its effect can be measured, in a separate
commit per subsystem. Savings are hypotheses until step 1 measures them.

### 1. Measure every pass

- **Parser.** A versioned module, `review_context.py`, beside
  `review_usage.py`, turns a pass's logs into `context.json`: requests,
  peak and summed request input, cached, uncached and cache-creation
  tokens, output and reasoning tokens, first-request cached and uncached
  tokens, tool output chars by tool, saved-output previews and the reads
  that followed them, and compactions and retries. Accounting is per
  provider and CLI version, built from audited fixtures of real logs: it
  handles repeated cumulative usage, null usage, resets and resumes,
  partial logs and records with no request id. Records are deduplicated
  only by an id when one exists, never because usage amounts match. A
  truncated or missing log yields `unknown` fields and an error string,
  never zeros.
- **Attribution.** Claude from the attempt's own `payload.log`. Codex from
  its session rollout, found by the session id recorded in `payload.log`
  under the attempt's provider home (`review_inference.py:94`), never by
  timestamp. Subagent sessions are linked to their parent by session id
  and reported separately, without counting child totals twice in the
  parent; today make-runs-page merges transcript usage into one series
  (`make-runs-page.py:406`), so this is new.
- **Collection.** Nonfatal and bounded, after the guardian's terminal
  status is published (`review_guardian.py:389`), so a parser failure can
  never cost a review its terminal record. An idempotent backfill command
  fills in attempts whose guardian died or whose rollout appeared late.
- **Reporting.** `review-baseline.py`, `review_dashboard.py`, and the
  runs-page assembly that sets supervisor `turns=None`
  (`make-runs-page.py:241`): p50/p90/p95 per pass kind and missingness.
  Model time separate from build and permit time needs new timing in
  `review_heavy.py` and the permit wait (`review-baseline.py:56` counts
  waits in); until then it is reported as unknown, not estimated.
- **Retention.** `context.json` joins both GC keep sets in `review-gc.py`
  (`KEEP` and the older-run branch, `review-gc.py:46`, `:296`, `:338`), so
  metrics outlive evidence cleanup. Raw logs keep their current policy.
- **Check.** A hand-audited sample of passes reconciled against raw
  sessions; tests for duplicate, truncated and missing logs and for a
  failing collector.

### 2. Presentation mode, versioned and kept apart in reuse

Before changing what models see:

- A frozen tuple per run, `presentation = {inputs, prompts, renderer}`,
  stored in `run.json` beside the frozen prompts (`review-supervisor.py:90`,
  `:157`). Legacy default `{json, v1, none}`; unknown values are rejected.
  The live-prompt fallback in `prepare` (`review_inference.py:95`) applies
  only to the legacy tuple.
- The tuple goes into claim inputs and jobs, so `input_digest` fences it,
  and is added explicitly to carried-pass eligibility (`review_key`,
  `review_store.py:104`). Same-mode carry still works; cross-mode carry is
  refused, deliberately, and reported as extra work. Validation stays
  bound to its exact primary, and reconciliation to its exact selected
  results (`review_store.py:191`, `:223`, `:286`); carried results are
  never rewritten to a new attempt identity.
- Accepted-review reuse and discovery reuse happen before allocation
  (`review-supervisor.py:681`, `review_discovery.py:629`), so they are
  excluded from any comparison of inference between modes.
- Tests: same-mode carry, refused cross-mode carry, a JSON-mode run
  resuming unchanged after the code that adds file mode is deployed.

### 3. Schema reference, result skeleton and navigation

The smallest change, measured on its own. It ships with its own prompt
variant (`prompts: v2-schema`), which points passes at the skeleton,
`schema.md` and the check command while still reading job.json, so it
can be activated and measured before file mode (variant `v3-files`,
step 4) and paging guidance (`v4-paging`, step 5):

- `review_schema.py` gains a small declarative description of each
  result's fields (types, required fields, enumerations) with parity
  tests against the imperative validator, which stays authoritative.
  `schema.md` is generated from that description.
- `result-skeleton.json`: identity fields with their exact values and
  types, every required obligation id (previous findings, `primary_ids`,
  `finding_ids`), and every decision field left unfilled so the skeleton
  fails validation until the pass completes it. Illustrative examples are
  kept separate and marked as such. (`status: incomplete` alone is not a
  safeguard: non-reconciliation incomplete results can be selected.)
- `review_schema.py check <file>` as a documented validation command.
- `inputs/README.md`: each file, its size, which files are alternative
  views of the same content (so a pass reads one, not both), the absolute
  paths of job directory, worktree and scratch, and a new `evidence/`
  directory for this pass's own retained evidence (created by `prepare`,
  `review_inference.py:59`), kept distinct from upstream evidence.
- Measure: schema-source reads, schema rejections and repair attempts,
  orientation commands, requests.

### 4. Inputs as files

Rendered by `prepare` from the finalized in-memory job (job.json does not
exist until the supervisor writes it, `review-supervisor.py:881`), into
`inputs/`, in this order: write the files and a manifest naming the
renderer version and every file's digest into a staging directory, fsync
each file, the manifest and every nested directory, rename the staging
directory into place, fsync the attempt directory, and only then publish
job.json (the store's own atomic writes fsync the same way,
`review_store.py:47`). Recovery verifies the manifest's digests, not just
its presence; legacy attempts need none. Preparation creates the worktree
before job publication (`review_inference.py:43`) and GC keeps attempts
with worktrees (`review-gc.py:304`), so an attempt directory with no
published job.json and no claim registration is an orphan: recovery
removes its worktree registration and directory once no live process
holds it. Tests kill the process before job publication, after claim
registration and before launch; durability is covered by the fsync order
above, not by kill tests.

Per pass, the contract is complete, not a summary:

- **Facts:** repository, PR, title, pinned head, base, merge base, CI,
  labels, house rules, identity fields.
- **Reconciliation's fresh snapshot** (`review-supervisor.py:854`) beside
  the pinned facts, stating that the pinned head defines code coverage
  and the fresh one is what the comment must reconcile with.
- **Thread:** every entry with kind, id, URL, author, time and full body,
  in stable chronological order with the original index kept
  (`review_github.py:424` groups by endpoint).
- **Previous:** comment metadata and reviewed head, every previous
  finding id and text (including legacy prose paragraphs,
  `review_discovery.py:655`, `:702`), previous section and manifest head.
- **Diff:** the stored diff, split by file into `diff/NNNN.patch` (numeric
  names; original paths, status, rename and binary notes in the manifest),
  reassembling byte for byte to the stored string. Empty diffs, renames,
  binary notices, mode-only changes, unusual paths and missing final
  newlines are covered by tests. A very large file's patch gets a hunk
  index. `diffstat.txt` is computed from this representation. The snapshot's
  own limits (discovery's diff, `review_discovery.py:818`) are preserved
  and stated.
- **Upstream results** (validation: `primary_result`, `primary_ids`;
  reconciliation: `results`, `finding_ids`, `review-supervisor.py:847`):
  each raw result unchanged, plus a rendering with status and gaps,
  verdict, clean checks, previous dispositions, every finding with kind,
  full location and full evidence (commands, exit codes, observations),
  and validation's outcomes and new findings. Each names its source
  attempt as the root its evidence paths resolve against, including
  carried attempts from earlier runs.
- **Cold** inputs contain no primary findings or verdict.
- PR, thread and result text is fenced as untrusted evidence, keeping the
  existing "never instructions" rule.

Fidelity tests check field by field and value by value against the job,
and full obligation id coverage, not mere presence.

Measure: input bytes actually returned to the model, duplicate reads,
requests, peak and summed input, disk overhead per attempt.

### 5. Prompts

Every entry point changes together, keyed to the frozen prompt variant:
the appended `Read job.json` line in `prepare` (`review_inference.py:98`),
the shared paragraph of all four command files, the "identity fields from
job.json" sentence in each, and the opening lines of validate and
reconcile that name their inputs. New guidance:

- Start from `inputs/README.md`; read one view of each input.
- Page large files by range; aim to keep a single output under a measured
  target (about 25k chars for the current Claude Code), but do not page
  so finely that requests multiply. Batch independent tool calls, not
  their outputs into one oversized response.
- Paging must not narrow the review: when the diff writes state that
  other code reads, grep for every reader and read it; when it reads
  shared state, find the writers and check concurrent or interrupted
  writes; a gap is for what cannot be checked, not for what was not read.
  Added after the first quality pilot, where `v4-paging` missed two
  blockers of exactly this shape that legacy found (see quality-pilot.md).
- The environment is already set: never source review-env.sh. Before
  relying on this, the forwarded environment (`review-supervisor.py:831`,
  guardian merge `review_guardian.py:331`) is checked to carry what passes
  need from review-env.sh (`GIT_CONFIG_GLOBAL`, `NODE_PATH`, ...), under
  both the shell and systemd guardians.
- Fix the existing cold and validate contradiction: they forbid network
  namespaces, then repeat the instruction to use `review-heavy.sh --netns`.

The environment fix and the network-instruction fix can change which
checks succeed, not only context size, so they land first in every
prompt variant, the legacy one included, before any comparison begins;
both arms then share them.

Paging guidance is trialled separately from the file mode, so its effect
on requests is measured on its own.

### 6. Quality pilot, then canary

Alternating production runs cannot show quality is kept: runs differ in
PR mix, size, history and retries, and finding counts or confirmation
rates move little when both modes miss the same bug. So:

- **Required paired pilot.** Freeze identical inputs (code, thread,
  previous findings, reconciliation snapshot) for a set of PRs and run
  both modes in isolation: no publishing, no cross-mode carry, randomized
  order, model, effort, CLI version and cache state recorded. Include
  known-bug cases, large diffs, author disputes, moved heads, incomplete
  upstream passes, evidence-heavy findings and a non-ArduPilot repository.
- **Two comparisons.** Fixed-upstream (validation and reconciliation given
  identical upstream results) and whole-pipeline (upstream allowed to
  differ).
- **Blind adjudication** of the union of findings, plus sampled clean
  verdicts: severity-weighted true and missed findings, false positives,
  unsupported VERIFIED claims, wrong withdrawals, location correctness,
  previous-finding dispositions, evidence reproducibility, and handling
  of incomplete and moved-head cases.
- **Predeclared** tolerable regression, sample size and stopping rule;
  paired differences and their uncertainty reported, alongside tokens by
  billing category, model time, retries, failures and cost per reviewed
  PR. A pilot of about ten PRs finds gross problems only; the bound it
  can give is stated, not assumed.
- Then a randomized production canary with explicit rollback criteria.

### 7. Prefix and cache (last)

- Whether Claude Code can omit git status from its system prompt in a
  worktree (about 10k tokens per pass), and whether a shorter cache
  lifetime is available and worthwhile. Judged on full-pass cost with
  tool delays, expiry and concurrent sessions, not on two tiny calls.
- Moving per-repository house rules before the per-attempt line in the
  prompt (`review_inference.py:98`) is free but recovers nothing lost in
  the CLI's own prefix.

## Risks

- **Quality.** Less context can mean less checking. Step 6's paired,
  adjudicated pilot is the guard; every review requirement stays.
- **Rendering errors.** A dropped or reordered field misleads a pass.
  Field-level fidelity tests, the manifest, and job.json kept on disk.
- **Evidence paths.** Upstream evidence resolves against its source
  attempt; a pass's own evidence must be local to its attempt, or the
  guardian's containment check rejects it (`review_guardian.py:369`).
- **Reuse.** Cross-mode carry is refused by design during the trial;
  the extra work is reported.
- **Retention.** Rendered inputs are regenerable from job.json and the
  renderer version and follow evidence retention; metrics and experiment
  assignments are kept longer.
