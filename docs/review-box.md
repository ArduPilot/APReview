# The review runner

A machine that runs reviews unattended. Nothing here is required to use
`/reviewprs` by hand — it is what turns the command into a service that reviews
every labelled PR around the clock and publishes the results.

The reference deployment is a dedicated box: 32 cores, 30G RAM, a few hundred GB
free, Ubuntu under WSL2. Cores and RAM matter because a run fans out to parallel
per-PR agents, several of which build and fly SITL.

## Layout

Everything lives under `~/review`, which is `$REVIEW_ROOT`:

| path | what |
|---|---|
| `~/review/bin/` | the scripts from `runner/bin/` in this repo |
| `~/review/etc/` | `local.conf`, the run lock, the crontab, per-account config |
| `~/review/data/` | all scratch: checkouts, clones, build trees. `$REVIEW_DATA` |
| `~/review/repositories/` | maintained base clones of every reviewed repo. `$REVIEW_REPOS` |
| `~/review/work/` | working dir for a run; reports land here, base checkouts stay clean |
| `~/review/logs/` | run logs, 30-day retention. `$REVIEW_LOGS` |

`review-env.sh` points `TMPDIR` into `$REVIEW_DATA/tmp`. On a box where `/tmp` is
a tmpfs, a bare `mktemp -d` there fills RAM and takes the machine down with it —
this has happened, so the redirection is not optional.

## Install

```sh
git clone https://github.com/ArduPilot/APReview.git
mkdir -p ~/review/etc
cp -r APReview/runner/bin ~/review/bin
cp APReview/runner/etc/crontab.reviewprs ~/review/etc/
cp APReview/runner/etc/local.conf.example ~/review/etc/local.conf   # then edit
cp APReview/commands/reviewprs.md ~/.claude/commands/

~/review/bin/clone-ardupilot.sh      # base clone with submodules
~/review/bin/clone-repos.sh          # the other repos, submodules included
~/review/bin/base-build.sh           # proves the toolchain, warms ccache
crontab ~/review/etc/crontab.reviewprs
```

`local.conf` is where the site-specific settings go: the rsync destination, its
credentials, the public URL reports appear at, and optionally a separate Claude
account for one mode. It is never committed.

## Scripts

- `bin/review-env.sh` — source this first. Sets `$REVIEW_*`, `TMPDIR`, `CCACHE_DIR`,
  the toolchain PATHs, `GIT_CONFIG_GLOBAL`, the publish variables, and reads `local.conf`.
- `bin/run-reviewprs.sh <mode>` — the cron entry point. `followup` | `all` | `rsync` | any label.
- `bin/netns-run.sh` — run a command with its own network namespace, so parallel SITL
  work keeps the default TCP ports instead of needing `--uds` (which changes SITL UART
  timing and produces false findings). `--session <dir>` gives one agent a namespace
  shared by all its commands. `autotest-netns.sh` is a symlink to it.
- `bin/refresh-repos.sh` — nightly refresh of the base clones, lock-guarded.
- `bin/clone-ardupilot.sh`, `bin/clone-repos.sh` — the base clones, submodules included.
- `bin/base-build.sh` — SITL + a ChibiOS board; proves the toolchain and warms ccache.
- `bin/claude-usage-probe.sh` — samples the real usage meter, tagged by account.
- `bin/reap-orphans.sh` — kills processes a run left behind under `$REVIEW_DATA`.
- `bin/make-runs-page.py`, `bin/publish-runs-page.sh` — the runs dashboard.
- `bin/pause-runs.sh`, `bin/review-now.sh` — hold the lock; run a mode on demand.
- `bin/fmt-stream.py` — turns `--output-format stream-json` into a readable log.

## Things the deployment gets wrong if you skip them

**One lock for everything.** All review runs and the repo refresh share
`etc/reviewprs.lock`. The label sub-runs share a report file and follow-up reads
what they publish, so overlap corrupts output. A blocked slot logs `SKIPPED`
rather than queueing, and only the exit path of a run that *held* the lock is
allowed to reap processes.

**The agent pool needs its own cgroup scope.** `setsid` alone leaves the pool in
the launching terminal's cgroup, so when it runs the box out of memory, systemd-oomd
kills that whole cgroup — editor included. `run-reviewprs.sh` launches the pool in a
transient `systemd-run --user --scope` with `MemoryMax`, `TasksMax` and, where the
`cpu` controller is delegated, `CPUQuota`.

**Every agent gets a two-level working directory.** `<scratch>/agents/<pr>/wt` for
the checkout, so `autotest.py`'s `../buildlogs` — and the `autotest.lck` in it —
stays inside that agent's own root. Sibling checkouts sharing a parent serialise on
one lock, however well their networks are isolated.

**Never `--uds`.** See the SITL section of `commands/reviewprs.md`: AF_UNIX changes
the anti-lag throttle from 1024 to 65536 bytes of outqueue, so tests behave
differently from what the PR author sees. Wrap the test in `netns-run.sh` instead —
and wrap the *test*, not the agent: inside the namespace `lo` is the only
interface, so an agent started there cannot reach its own model API.

**Base clones carry their submodules.** `clone-repos.sh` populates them, taking the
objects from another base where the submodule is a repo already kept locally. Agents
then clone with `--shared` plus `submodule.alternateLocation=superproject`, which
costs seconds and transfers no objects.

## Publishing

Reports go out with `rsync`, either to an rsync daemon (`rsync://user@host` plus a
password file) or over ssh (`host:path`, no auth option). Set `REVIEW_PUBLISH`,
`RSYNC_AUTH` and `REVIEW_PUBLIC_URL` in `local.conf`; the command reads its own
previous reports back from `REVIEW_PUBLIC_URL` and links to it from the comments it
posts. With `REVIEW_PUBLISH` unset, a run keeps its report local and says so.
