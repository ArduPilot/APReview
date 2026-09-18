# APReview

Automated pull-request review for ArduPilot, driven by a Claude Code slash
command and cross-checked by a second, independent AI reviewer. It sweeps the
open PRs that carry a review label — across the main repo, the wiki, every
ArduPilot-owned submodule and the standalone ArduPilot repos — reviews each one
at depth, publishes an HTML report, and posts the findings back to the PR as a
clearly marked AI comment.

It runs unattended. The reference deployment reviews the labelled set every six
hours, re-checks previously reviewed PRs whose code has moved every three, and
has been doing so continuously since September 2026.

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

Each PR is reviewed **twice, independently**: once by the command itself, and once
by a per-PR agent running OpenAI Codex that has not seen the first review. The two
passes are reconciled before anything is published, which is what catches the
confident-but-wrong finding that a single reviewer produces.

## How a review is produced

1. **Discover** the PR set from the labels, across every repo (`.gitmodules` is parsed
   to find ArduPilot-owned submodules; standalone repos are swept explicitly).
2. **Skip what has not changed.** Each report carries a manifest of the head SHA it
   reviewed; a PR still at that head is reused verbatim. Rebase-only moves are detected
   by comparing blob SHAs and skipped too.
3. **Check out** each PR into its own sandbox, referenced against a local base clone so
   nothing is downloaded twice.
4. **Review** it — the diff, the surrounding code, the thread, the CI result, and where a
   claim is testable, a test run. Numeric claims are reproduced rather than asserted.
5. **Cross-check** with a parallel pool of per-PR Codex agents.
6. **Reconcile**, write the HTML report, publish it, and post or update the PR comment.

## Layout

```
commands/reviewprs.md     the slash command: the whole review workflow
runner/bin/               scripts that run it unattended on a dedicated box
runner/etc/               crontab and site configuration example
docs/review-box.md        setting up and operating the runner
```

## Requirements

- [Claude Code](https://claude.com/claude-code) with a subscription that allows
  unattended use, and the `codex` CLI for the second opinion.
- `gh`, authenticated (`gh auth status`) — the command posts comments as that user.
- `git`, `python3`, `rsync`, and an ArduPilot build environment if you want the
  reviews to build and fly SITL.
- Linux. The runner uses flock, cgroups via systemd, and user network namespaces.

## Quick start

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

`runner/` holds the deployment: a cron entry point that serialises every run on one
lock, refreshes base clones nightly, samples the account's quota meter before and
after each run, publishes a dashboard of recent runs, and reaps anything a run leaves
behind. See [docs/review-box.md](docs/review-box.md).

## Configuration

Site settings live in `runner/etc/local.conf` (copy `local.conf.example`), and are
never committed:

| variable | what |
|---|---|
| `REVIEW_PUBLISH` | rsync destination for reports — `rsync://user@host` or `host:path` |
| `RSYNC_AUTH` | `--password-file=…` for an rsync daemon, empty for ssh |
| `REVIEW_PUBLIC_URL` | public base URL those reports appear at |
| `REVIEW_BOX_NAME` | name shown on the runs dashboard |

Accounts are not configured here — they live under `~/review/auth/`, one
directory each, with a symlink per role saying which account that role uses.
`runner/bin/review-auth.sh status` shows what every role resolves to.

With `REVIEW_PUBLISH` unset the reports stay local and the run says so.

## What makes the output worth reading

These rules are in the command because each was learned from a wrong review:

- **Every finding carries evidence** — file:line at the current head, and how it was
  verified. A claim that could not be checked says so rather than sounding certain.
- **Claimed fixes are proved by mutation**, not by reading: revert the fix, re-run the
  test, confirm it fails. Reading a test has twice over-claimed coverage that was not there.
- **Numbers are reproduced.** A finding once claimed a 101° phase error that measured
  0.06° when actually computed.
- **The author's own comments are read first.** A finding that argues against a
  deliberate, commented change must engage with the reasoning or be dropped.
- **SITL tests never use `--uds`.** Unix-domain sockets change the SITL UART anti-lag
  throttle from 1024 to 65536 bytes of outqueue, so timing-sensitive tests behave
  differently from what the PR author sees, and the findings are false. Parallel runs
  get a private network namespace instead.
- **Nothing is pushed.** The command's permissions deny `git push`, and the runner
  refuses to start if that denial is missing from its settings.

## The second target

`/reviewprs rsync` reviews a non-ArduPilot project (`RsyncProject/rsync`, opted in by the
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
corrections to how it reviews: every rule in `commands/reviewprs.md` exists because a
review got something wrong, and the ones that are missing are the reviews that are still
getting things wrong.

A few things worth knowing before changing it:

- **The command is one markdown file.** `commands/reviewprs.md` is the entire workflow —
  discovery, review, cross-check, report, comment. Editing it changes behaviour on the next
  run, with no build step.
- **Site-specific by design, for now.** The labels (`DevCallTopic`, `DevCallEU`,
  `AIReview`), the repo list and the report layout are written into the command rather than
  configured. That is deliberate while there is one deployment; it is also the first thing
  to generalise if a second one appears.
- **Test on a runner, not just by reading.** A change that looks right in the markdown can
  still produce a worse review. The cheapest real test is `/reviewprs <one PR number>`
  against a PR whose outcome you already know.
- **Claims in a review need evidence, and so do changes to the rules.** If you add a rule,
  say which review it would have fixed.

## License

GPLv3, like the rest of ArduPilot — see [LICENSE](LICENSE).
