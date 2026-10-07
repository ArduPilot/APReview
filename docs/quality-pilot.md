# Quality pilot: legacy against v4-paging

Step 6 of `docs/context-efficiency-plan.md`. Before any presentation other
than legacy is used for real, both review the same frozen PRs in
isolation, and the findings are judged blind. Everything below was fixed
on 2026-10-05, before any pilot review ran.

## Question

Does `v4-paging` (inputs as files, the schema skeleton and check, paging
guidance) review as well as `legacy` (job.json, v1 prompts), at lower
context cost?

## PRs

Ten recent open PRs by tridge, in repositories the system reviews and
has reference clones for:

| PR | What |
| --- | --- |
| ArduPilot/ardupilot#34657 | preserve zero-initialised allocations, verify allocator wrapping |
| ArduPilot/ardupilot#34650 | renode fixes |
| ArduPilot/ardupilot#34623 | Tools: static stack usage analysis and CI check |
| ArduPilot/ardupilot#34606 | AP_HAL_ChibiOS: optional STM32H7 USB GDB debugging |
| ArduPilot/ardupilot#34604 | AP_HAL_ChibiOS: enable H7 SRAM before the first stack access |
| ArduPilot/ardupilot#34597 | BLHeli passthrough interrupt storm lockup |
| ArduPilot/ardupilot#34519 | GCS_MAVLink: full-width 32-bit parameters |
| ArduPilot/MAVProxy#1770 | misseditor: survey planning and draft mission maps |
| ArduPilot/MAVProxy#1769 | wp: fix Draw altitude and add remembered frame selection |
| mavlink/mavlink#2637 | common: add stream ID and vertical FOV extensions |

(AM32#78 and AM32#417 were first chosen, and replaced before any review
ran: the system has no AM32 reference clone, so they could not be frozen.
ardupilot#34626, #34619 and #34618 were merged or closed before the freeze
on 2026-10-08; freeze refused them, and they were replaced, before any
review ran, by the three newest open non-WIP ardupilot PRs by tridge:
#34657, #34650 and #34519.)

## Procedure

1. **Freeze.** `review-pilot.py freeze` reads each PR once, as discovery
   does (head, base, diff, thread, our previous comment), into
   `candidates.json`, and freezes one configuration for both arms (models,
   efforts, accounts), checked by digest when each arm starts. A PR
   discovery could not read completely is refused, not reviewed. Both arms
   review exactly these inputs; reconciliation's "PR as it is now" is the
   same frozen snapshot for both.
2. **Run, overnight.** `review-pilot.py overnight` checks no production run
   is going, pauses production review runs (`pause-runs.sh`; the drainer
   keeps delivering), starts both arms together, and ends the pause when
   they finish. Each arm has a store of its own, with its own locks, so
   nothing carries between arms or from production, and no earlier review
   can be reused; its `quota.json` is production's, so a quota pause stops
   the pilot too. `frozen_inputs` keeps the supervisor from reading GitHub
   again and keeps all delivery in the pilot store: nothing is published,
   posted or synced. Passes share production's reference clones (no
   production pass runs meanwhile; the 02:30 refresh only fetches),
   provider homes and caches.
3. **Collect.** `review-pilot.py collect` first checks that every frozen PR
   was accepted at its frozen head in both arms, under the arm's
   presentation; otherwise nothing is pooled. It then pools, per PR, every
   finding each arm's reconciliation kept (upstream findings and previous
   findings not refuted or merged, with reconciliation's rationale where it
   adjusted one), shuffles them, gives each a random id, and hides the arm
   behind `adjudication-key.json`. Text that could reveal the arm (paths
   into the job directory or rendered inputs) is replaced by `[path]`;
   evidence commands are left out, since the adjudicator checks the code
   itself. Context figures for every attempt, retries included, go to
   `contexts.json`.
4. **Adjudicate.** Codex judges every finding in `adjudication-pack.json`
   against the PR's code at the frozen head, without the key: `real`
   (true/false); for a real one, `blocking` (whether it should block) and
   `issue` (a name shared by every finding, in either arm, that describes
   the same problem). tridge then reviews a sample of ten findings chosen
   at random, and every real blocker found by only one arm.
5. **Score.** `review-pilot.py score` refuses incomplete or inconsistent
   verdicts, unblinds the rest and applies the bound below. A false
   blocker is a finding raised as blocking that is not a real problem
   that should block.

## Bound (fixed before any pilot review ran)

`v4-paging` fails if, across the ten PRs, any of these holds:

- it misses any real blocking problem that legacy found;
- it misses more than 2 real non-blocking problems that legacy found;
- it raises more than 2 more false blockers than legacy does.

Problems `v4-paging` finds that legacy did not are reported, but do not
offset misses.

## What a pass does not show

Ten PRs find gross problems only: a pass says no large regression showed
on these PRs, not that none exists. Model reviews vary from run to run,
so some differences between arms would appear even between two runs of
the same presentation. A failure is investigated by finding (which step
of the change could have caused each miss) before anything is decided.

## Cost

About 20 full reviews (10 PRs, two arms, four passes each), roughly one
all-run's quota, over several hours with a pool of 2 per arm, overnight
while production review runs are paused.

## Not covered by this phase

Comparing validation and reconciliation alone on identical upstream
results, and a moved head, are a second phase, run only if this one
passes.
