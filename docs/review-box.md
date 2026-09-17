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
git clone https://github.com/ArduPilot/APReview.git ~/APReview
mkdir -p ~/review/etc

# bin is a symlink into the checkout, not a copy: `git pull` is then the whole
# deploy step, and the scripts find repos.json beside themselves. A copied bin
# has no checkout to resolve back to, and clone-repos.sh will say so and stop.
ln -s ~/APReview/runner/bin ~/review/bin
ln -sf ~/APReview/commands/reviewprs.md ~/.claude/commands/reviewprs.md

cp ~/APReview/runner/etc/crontab.reviewprs ~/review/etc/
cp ~/APReview/runner/etc/local.conf.example ~/review/etc/local.conf   # then edit

~/review/bin/clone-ardupilot.sh      # base clone with submodules
~/review/bin/clone-repos.sh          # everything in repos.json, submodules included
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
- `bin/post-comments.py` — posts or updates the AI review comment on each PR in a
  plan, deciding post / edit / deprecate-and-repost. Tested in `runner/tests/`.
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

## Accounts

Each account is a directory under `~/review/auth`, and a symlink per role says
which account that role uses. Nothing here is in git — these hold credentials.

```
auth/claude-ardupilot/   a CLAUDE_CONFIG_DIR   auth/claude-default -> claude-ardupilot
auth/claude-personal/                          auth/claude-rsync   -> claude-personal
auth/codex-ardupilot/    a CODEX_HOME          auth/codex-default  -> codex-personal
auth/codex-personal/                           auth/codex-rsync    -> codex-personal
```

`default` covers ArduPilot work; `rsync` covers the non-ArduPilot target, which
is reviewed on a personal subscription so the project's quota is not spent on it.

```sh
review-auth.sh status                        # what every role resolves to
review-auth.sh list                          # the accounts available
review-auth.sh login claude personal         # how to sign one in
review-auth.sh use claude default personal   # move a role to another account
```

**Switching account is repointing a symlink.** When the ArduPilot subscription
runs out of weekly quota, `use claude default personal` moves the whole workload
across and the next run picks it up; `use claude default ardupilot` moves it back.
Nothing is edited and no run is interrupted.

A directory may record the address it is meant to hold, in a file called
`ACCOUNT` — an address or an id, nothing else, so a token put there by mistake is
refused rather than echoed into a run log. A run refuses to start if:

- the role has no link, and it is not `default` (a missing `claude-rsync` must
  not quietly become the project's subscription);
- the link dangles, resolves outside `auth/`, or points somewhere other users
  can read;
- the directory holds no credentials;
- its `ACCOUNT` disagrees with what it is signed in as;
- the directory's own record and the CLI disagree about the address — two local
  records agreeing proves nothing about which subscription pays;
- the Codex directory authenticates with an API key. An account id left in
  `auth.json` by an earlier subscription login still matches `ACCOUNT`, but the
  key is what gets billed;
- `auth/` itself is writable by other users, or not yours — private account
  directories protect nothing if anyone can repoint the links that choose
  between them;
- an `ACCOUNT` exists but cannot be read, or the signed-in address cannot be
  determined to check it against. A constraint must not disappear because
  reading it failed.

An inherited credential or provider selector decides the account whatever the
role says, so before anything else the run clears every `ANTHROPIC_*` and
`OPENAI_*` variable and every `CLAUDE_*`/`CODEX_*` one whose name contains
`TOKEN`, `KEY`, `AUTH`, `SECRET`, `CREDENTIAL`, `CONFIG_DIR`, `STORAGE`,
`BASE_URL`, `HOME` or `USE_`, anywhere in the name, and prints what it cleared -
names only. Naming them one at a time missed `CLAUDE_SECURESTORAGE_CONFIG_DIR`,
which points the CLI at another credential store while the selected directory
still reports its own address; matching only the end of the name missed
`CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`. It clears rather than refuses because
a manual run from a terminal inside Claude Code carries `CLAUDE_CODE_*` variables
that are not credentials.

A variable declared `readonly` survives `unset`, and bash reports that only on
stderr - so the sweep checks each name is really gone afterwards and the run
refuses if one is not, rather than printing it as cleared.

No sweep can be proved complete, so the run also asks the CLI what it actually
did. `claude auth status --json` reports `authMethod`, `apiProvider` and
`configDirectory`: the run refuses anything but a `claude.ai` login on the
`firstParty` provider reading the directory the role selected. A token, a cloud
provider or another config directory all show up there whatever the environment
looked like. All three or none: a CLI too old to report any of them still runs,
but one reporting some and not others is not a version, it is an answer that has
lost the part that would have failed.

Codex is asked the same question a different way. `auth.json` names the account
that signed in; `config.toml` decides where the request goes and which key pays
for it. Enumerating the settings that redirect it is a losing game -
`chatgpt_base_url` sent the account's own OAuth token to another host with
`model_provider` still `openai` - so the check is the other way round: any URL in
`config.toml` whose host is not OpenAI's, any `env_key`/`api_key`/header
override, any `model_provider` that is not `openai`, anywhere in the file, and
the run refuses naming the setting. A `config.toml` that cannot be parsed is
refused too; an absent one is fine. If a legitimate entry ever trips this - an
MCP server over http, say - the refusal names the key it objected to.

Record the account **id** for Codex and the **address** for Claude — that is what
each tool reports, and the runner compares like with like.

Both tools are checked, not just Claude: a missing `auth.json` used to surface
only when the validation pool failed, well into a run.

`status` clears the same variables before it asks, and then makes exactly the
judgements the runner makes - including a variable it could not clear, a partial
answer from the CLI, and a redirected Codex config - so a role that reads as
healthy there is one a run will accept. That includes the fallback below: a
default role with no link is reported as the tool's own account, with the same
checks applied, rather than as "not set".

Two things worth knowing. An account directory keeps its address in
`<dir>/.claude.json`, but only if it was created by `claude auth login` under
`CLAUDE_CONFIG_DIR`; the tool's own `~/.claude` keeps it elsewhere, so a run
whose role resolves there leaves the variable unset rather than setting it to the
same path, which would make `claude auth status` report no address at all. That
applies only when the path is genuinely that directory: if `~/.claude` is itself
a symlink the variable is pinned to what it resolves to now, so repointing it
mid-run cannot move the work to another account. And a role with no symlink falls
back to `default`, so a new role costs nothing until it needs its own account.

Switching takes a per-role lock. Two switches of the same role could otherwise
interleave, and a switch that turned out to be invalid would revert over one that
had already succeeded. If a revert cannot be made, `use` says so and names the
account the role has been left on rather than reporting success.

## Publishing

Reports go out with `rsync`, either to an rsync daemon (`rsync://user@host` plus a
password file) or over ssh (`host:path`, no auth option). Set `REVIEW_PUBLISH`,
`RSYNC_AUTH` and `REVIEW_PUBLIC_URL` in `local.conf`; the command reads its own
previous reports back from `REVIEW_PUBLIC_URL` and links to it from the comments it
posts. With `REVIEW_PUBLISH` unset, a run keeps its report local and says so.
