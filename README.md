# APReview

Automated pull-request review for ArduPilot. Each PR is reviewed by Claude and
by OpenAI Codex independently, and the two are reconciled into one verdict. It
sweeps the open PRs that carry a review label — across the main repo, the wiki,
every ArduPilot-owned submodule and the standalone ArduPilot repos — reviews
each one at depth, publishes an HTML report, and posts the findings back to the
PR as a clearly marked AI comment.

It runs unattended. A Python supervisor owns everything except the reading of
the code: discovery, scheduling, sandboxes, timeouts, the report, comment
posting and the results board. Inference is a fixed set of short jobs per PR,
run in parallel. The reference deployment sweeps the labelled set hourly,
re-checks previously reviewed PRs whose code has moved every three hours, and
has been running continuously since September 2026.

**It does not replace a human reviewer.** It reads a diff far more thoroughly
than anyone has time to, and it is wrong often enough that every finding is
written to be checked: file:line references that link into the GitHub diff, the
evidence for each claim, and an explicit note when something could not be
verified.

## What it does

- **Reviews by label** — `DevCallTopic`, `DevCallEU`, `AIReview`, or any label you name.
- **Reviews by author** — every open PR by one person, updated in the last week.
- **Follows up** — re-reviews a PR whose head moved since the last AI comment, and says
  for each earlier finding whether it is now RESOLVED, STILL OPEN, DISPUTED or PARTIAL.
- **Reviews one PR** on demand, by number or URL.
- **Sweeps everything** — with no argument it runs the three labels then the follow-up pass.

Each PR gets **four passes**, two from each model, so no finding reaches the PR
on one reviewer's word. Reconciling them is what catches the confident-but-wrong
finding that a single reviewer produces.

## How a review is produced

1. **Discover** the PR set from the labels, across every repo (`.gitmodules` is parsed
   to find ArduPilot-owned submodules; standalone repos are listed in `repos.json`).
   Discovery runs once per run against a snapshot of heads.
2. **Skip what has not changed.** Every accepted review records the head it covered; a
   PR still at that head is reused, and a rebase-only move is detected by comparing
   blob SHAs. A PR reviewed in the last 12 hours waits for a later sweep unless it was
   asked for by name or is on a dev call's page for today or tomorrow.
3. **Claim** the PR under its own lock and check it out into a detached worktree of a
   local reference clone, so nothing is downloaded twice.
4. **Review it four times:**
   - **primary** (Claude) — the diff, the surrounding code, the thread, CI, and where a
     claim is testable, a build or SITL run;
   - **cold** (Codex), at the same time, without seeing the primary — a second opinion
     aimed at omissions, lifetimes and shared state;
   - **validation** (Codex), once the primary finishes — confirms, adjusts or refutes
     each primary finding;
   - **reconciliation** (Claude) — re-checks all three against fresh PR state and writes
     the verdict, the report section and the comment.

   Each pass writes JSON that Python checks against a schema. A PR whose passes did not
   all succeed is deferred to the next run, never posted from what did.
5. **Accept** the result into a store of immutable, numbered generations per PR.
6. **Deliver** it through an outbox: publish the report pages, post or update the PR
   comment, and sync the results board. A failed delivery retries on its own and never
   causes the PR to be reviewed again.

## Layout

```
commands/review-*.md      the per-pass prompts: primary, cold, validate, reconcile
commands/reviewprs.md     the original single-session slash command
repos.json                the repositories swept, and how to judge each one
runner/bin/               the supervisor and the scripts around it
runner/etc/               crontab and site configuration examples
runner/tests/             the test suite; CI runs it on every push and PR
docs/review-box.md        setting up and operating the runner
docs/supervisor-design.md how the supervisor works, and why
```

## Requirements

- [Claude Code](https://claude.com/claude-code) with a subscription that allows
  unattended use, and the `codex` CLI for the second opinion.
- `gh`, authenticated (`gh auth status`). Comments are posted as that user, or as the
  account whose token `GH_TOKEN` holds.
- `git`, `python3`, `rsync`, and an ArduPilot build environment if you want the
  reviews to build and fly SITL.
- Linux with systemd. Each pass runs in its own `systemd-run --user` unit with its own
  cgroup, so the box needs `loginctl enable-linger`; parallel SITL runs use user
  network namespaces.

## Reviewing by hand

The runner no longer uses `/reviewprs`, but the slash command still reviews in a
single Claude Code session, with Codex as the cross-check:

```sh
cp commands/reviewprs.md ~/.claude/commands/
cd /path/to/ardupilot
claude
```

then, in the session:

```
/reviewprs AIReview      # every open PR with that label
/reviewprs 34292         # one PR
/reviewprs @peterbarker  # one author's recent PRs
/reviewprs followup      # re-check PRs whose code moved since the last review
/reviewprs               # the full sweep
```

The report is written to the repository root. Publishing and comment posting only
happen for the labels that opt into it — a label nobody has opted in to is reviewed
and reported, never commented on.

## Running it unattended

`runner/` holds the deployment. `run-reviewprs.sh <mode>` is the cron entry point; it
picks the accounts, checks the permission guardrails and starts
`review-supervisor.py`. `review-now.sh` starts the same thing by hand for a label, a
mode or a single PR.

- **Runs share the machine, not a lock.** Runs overlap freely. They wait for each other
  only on the same PR, page or board, and share one pool of model sessions and build
  slots. A sweep that starts while a long one is still reviewing waits for that run's
  PRs and reviews only what is new.
- **Every pass is a separate unit.** A small guardian owns each pass's process tree,
  timeout and resource limits, outliving the supervisor if it must. Locks are kernel
  locks that die with their holder, so a crash or reboot leaves nothing to clean up by
  hand, and an unfinished run resumes from its own record.
- **A sweep that finds nothing costs nothing.** It reads GitHub and starts no model
  session, which is what makes an hourly sweep affordable.
- **Accounts are chosen by quota.** Each role lists the accounts it may use in
  priority order (`policy.json`), and a run takes the first with quota left. The
  meters are sampled hourly, and `check-auth.py` mails the operator when an account a
  role depends on is signed out.
- **The dashboard and the board.** A runs page shows every recent run, its passes,
  delivery backlog and token use. The [APReview Results board](https://github.com/orgs/ArduPilot/projects/33)
  lists every open labelled PR with its verdict.
- **Garbage collection** removes build trees and evidence once nothing can still need
  them, keeps run records for 90 days, and drops records of runs that found nothing to
  do after a day.

See [docs/review-box.md](docs/review-box.md) to set a box up and run it, and
[docs/supervisor-design.md](docs/supervisor-design.md) for the design.

## Configuration

Site settings live in `runner/etc/local.conf` (copy `local.conf.example`), and are
never committed:

| variable | what |
|---|---|
| `REVIEW_PUBLISH` | rsync destination for reports — `rsync://user@host` or `host:path` |
| `RSYNC_AUTH` | `--password-file=…` for an rsync daemon, empty for ssh |
| `REVIEW_PUBLIC_URL` | public base URL those reports appear at |
| `REVIEW_BOX_NAME` | name shown on the runs dashboard |
| `GH_TOKEN` | token for the account that posts the comments |
| `REVIEW_GITHUB_WRITES` | `1` to let the supervisor post; off on a new install |
| `REVIEW_ALERT_MAIL`, `REVIEW_ALERT_FROM` | who to mail when an account cannot run, and as whom |
| `REVIEW_POOL_SIZE`, `REVIEW_HEAVY_SIZE` | concurrent passes per model, and concurrent builds |
| `REVIEW_REREVIEW_HOURS` | how long a reviewed PR waits before another review (12) |

Accounts are not configured here — they live under `~/review.auth/`, one
directory each, with a symlink per role saying which account that role uses and
`policy.json` listing what each role may fall back to.
`runner/bin/review-auth.sh status` shows what every role resolves to.

With `REVIEW_PUBLISH` unset the reports stay local and the run says so.

## What makes the output worth reading

These rules are in the prompts and the code because each was learned from a wrong
review:

- **Every claim is verified or says it was not.** Each finding links to its line in the
  PR diff and is marked VERIFIED or UNCONFIRMED, with the commands run, their exit status, what was observed and the
  evidence kept. A finding once claimed a 101° phase error that measured 0.06° when
  actually computed.
- **Agreement is not proof.** Validation challenges each primary finding rather than
  taking it as authority, and a withdrawal is checked as carefully as a new bug. A
  disputed or unconfirmed blocker stays blocking until evidence refutes it.
- **Earlier findings are never dropped silently.** A follow-up gives every previous
  finding a disposition, and engages with the author's replies rather than repeating
  itself.
- **PR text is evidence, not instructions.** A pass treats the description and thread
  as claims to check, never as directions to follow.
- **SITL tests never use `--uds`.** Unix-domain sockets change the SITL UART anti-lag
  throttle from 1024 to 65536 bytes of outqueue, so timing-sensitive tests behave
  differently from what the PR author sees, and the findings are false. Parallel runs
  get a private network namespace instead.
- **Python owns the verdict line.** The marker, head, verdict and tables in a comment
  are written by code from the reconciled result, never typed by a model.
- **Nothing is pushed.** Claude's permissions deny `git push`, Codex runs in a sandbox
  with no network, and the runner refuses to start if that denial is missing from its
  settings.

Before enabling cron, merge this into each selectable Claude account's
`settings.json`, preserving existing settings and deny entries:

```json
{
  "permissions": {
    "defaultMode": "auto",
    "deny": [
      "Bash(git push)",
      "Bash(git push:*)",
      "Read(~/review.auth/**)"
    ]
  }
}
```

This includes `~/.claude/settings.json` when using the default fallback. For a
custom auth location, `review-auth.sh login claude <account>` prints the exact
rule. `Read` denials are guardrails, not containment of arbitrary shell readers.
Existing installations must update every Claude account **before pulling**:
`~/review/bin` points into the checkout, so pulling deploys the new pre-flight
immediately. See [runner installation](docs/review-box.md#install).

## The second target

The `rsync` mode reviews a non-ArduPilot project (`RsyncProject/rsync`, opted in by the
same `AIReview` label) with the ArduPilot house rules turned off. It is kept because it is
the one place the workflow is exercised against an unfamiliar codebase, which catches
assumptions that have quietly become ArduPilot-specific.

It runs under its own Claude account so that work does not consume the ArduPilot
subscription's quota. That is the `rsync` role: sign an account in once, and point the
role at it.

```sh
runner/bin/review-auth.sh login claude personal      # says what to run
runner/bin/review-auth.sh use claude rsync personal  # point the role at it
```

`run-reviewprs.sh` then points the whole run — the command, both usage probes and the
settings it reads — at that account, and **refuses to start** unless it can establish that
the account the role names is the one that will be billed. A missing login fails loudly
rather than quietly spending the wrong subscription; a missing `rsync` link is an error
rather than a silent fall back to the default account.

## Contributing

This is ArduPilot's own review system rather than a general-purpose tool, and it is meant
for the dev team to read, question and improve. The most useful contributions are
corrections to how it reviews: every rule in the prompts exists because a
review got something wrong, and the ones that are missing are the reviews that are still
getting things wrong.

A few things worth knowing before changing it:

- **How to review is in the prompts; everything else is code.** The per-pass prompts in
  `commands/review-*.md` are short, and the house rules for each repository come from
  `repos.json`. Orchestration, delivery and the report are Python in `runner/bin/`,
  covered by `runner/tests/`. Merging is deploying: the runner runs whatever is on
  `main`, and CI is the only gate.
- **Site-specific by design, for now.** The labels (`DevCallTopic`, `DevCallEU`,
  `AIReview`) and the report layout are written into the code rather than configured.
  That is deliberate while there is one deployment; it is also the first thing to
  generalise if a second one appears.
- **Test on a runner, not just by reading.** A prompt change that looks right can still
  produce a worse review. The cheapest real test is `review-now.sh <one PR>` against a PR
  whose outcome you already know; a change to how passes see their inputs gets a blind
  paired comparison first ([docs/quality-pilot.md](docs/quality-pilot.md)).
- **Claims in a review need evidence, and so do changes to the rules.** If you add a rule,
  say which review it would have fixed.

## License

GPLv3, like the rest of ArduPilot — see [LICENSE](LICENSE).
