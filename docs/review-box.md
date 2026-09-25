# The review runner

A machine that runs reviews unattended. Nothing here is required to use
`/reviewprs` by hand — it is what turns the command into a service that reviews
every labelled PR around the clock and publishes the results.

The reference deployment is a dedicated box: 32 cores, 30G RAM, a few hundred GB
free, Ubuntu under WSL2. Cores and RAM matter because a run fans out to parallel
per-PR agents, several of which build and fly SITL.

## Layout

Run data and scripts live under `~/review`, which is `$REVIEW_ROOT`:

| path | what |
|---|---|
| `~/review/bin/` | the scripts from `runner/bin/` in this repo |
| `~/review/etc/` | `local.conf`, the run lock, the crontab |
| `~/review/data/` | all scratch: checkouts, clones, build trees. `$REVIEW_DATA` |
| `~/review/repositories/` | maintained base clones of every reviewed repo. `$REVIEW_REPOS` |
| `~/review/work/` | working dir for a run; reports land here, base checkouts stay clean |
| `~/review/logs/` | run logs, 30-day retention. `$REVIEW_LOGS` |

`$REVIEW_AUTH` is `~/review.auth/` - one directory per account and a symlink per
role, never in git. It is a **sibling** of `~/review/` rather than a directory
inside it, because the reviewing agent is started with `--add-dir $REVIEW_ROOT`.
A box set up before that changed moves across with
`runner/bin/migrate-auth-root.sh` (`--dry-run` first), which renames the
directory and adds the new deny rule without removing existing protections.
Hold the run lock across the migration and deployment. The migrator has its own
`$HOME/review.auth` default and does not source the deployed environment; for a
custom location pass `--auth-root /absolute/path` explicitly. It validates every
account before moving, refuses links whose meaning would change, and resumes
unfinished settings updates if the directory has already moved. A completed
migration can be run again safely. Inspect a refusal before releasing the lock.

Historical Codex quota deltas recorded under the old home paths stay blank after
the move. Run logs are preserved: a vanished home does not prove ownership of
another directory's usage history.

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
```

Before enabling cron, merge these settings into `settings.json` in **every Claude
account directory** a role can select (including `~/.claude` if using the default
fallback). Preserve existing settings and deny entries:

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

For a custom `REVIEW_AUTH`, `review-auth.sh login claude <account>` prints the
exact absolute rule. The pre-flight accepts `Read(~/path/**)` or
`Read(//absolute/path/**)` covering the auth directory or an ancestor. Bare `Read`
removes that tool but does not deny the path to other readers such as Grep.
Dot and parent-directory segments are refused: resolving a filesystem path does
not prove that the CLI's pattern matches it. A single leading slash is relative to the settings
source, not the filesystem root. Other glob forms are refused if coverage cannot
be established. These rules are guardrails; arbitrary shell readers can still
reach credentials under the same uid.

On an existing box, update **all** Claude accounts before pulling this change:
the `bin` symlink makes that pull the deploy, and missing rules abort every slot.
Then enable cron:

```sh
crontab ~/review/etc/crontab.reviewprs
```

`local.conf` is where the site-specific settings go: the rsync destination, its
credentials and the public URL reports appear at. It is never committed. Accounts
are not in it - they are the directories and role links under `~/review.auth/`,
described below.

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

Each account is a directory under `~/review.auth`, and a symlink per role says
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
looked like. All three are required: a CLI too old to report them must be
upgraded. Empty, null, sentinel or control-bearing fields are invalid, not
missing metadata. The CLI must also exit successfully; a JSON body from a failed
command is not proof of authentication. Paths are preserved without trimming
whitespace.

Codex's `auth.json` must contain subscription tokens and a valid account id;
an id left behind without tokens is not a login. Unsupported authentication
modes are refused. The runner and `status` share the check of `config.toml`:

- inference endpoints must use HTTPS on an OpenAI host, with no userinfo,
  nonstandard port, query or fragment;
- provider credentials, auth commands, headers and query overrides are refused,
  including inline bearer tokens;
- model providers must be `openai`, and the credential store must be `file`
  if explicitly selected. A keyring can authenticate somebody other than the
  account recorded in the checked `auth.json`;
- unused custom provider definitions are allowed; selecting one in any config
  layer or profile is refused. Overrides of the `openai` provider are checked;
- every table is walked, not only the ones that route inference: `[otel]` takes
  an exporter endpoint and its own authorization header. `mcp_servers` is the
  exception - an MCP server's endpoint and headers authenticate that server, and
  a third-party host there is the point of it;
- an unreadable, malformed or dangling config is refused. An absent one is fine;
- `config.toml` is one layer of several. `/etc/codex/config.toml`,
  `managed_config.toml` and `requirements.toml`, and the `<name>.config.toml`
  that `--profile` merges, are all checked, and the refusal names the layer.

The refusal names the setting, including in `status`, without printing its value.
Reported identities are validated before they reach logs.

One layer is deliberately not checked here: Codex also merges project-local
configuration from the tree it runs in, and a run works in checkouts of other
people's pull requests. Nothing in this pre-flight can settle that - the checkout
does not exist yet when it runs - so it needs its own answer in the part of the
system that prepares those trees, not here.

Record the account **id** for Codex and the **address** for Claude — that is what
each tool reports, and the runner compares like with like.

Both tools are checked, not just Claude: a missing `auth.json` used to surface
only when the validation pool failed, well into a run.

The reviewing agent is started with `--add-dir $REVIEW_ROOT` and reads other
people's pull requests. The accounts used to live inside that directory, which
is why they no longer do: `$REVIEW_AUTH` is outside the granted review tree.
Relative paths such as `../review.auth` can still reach it. This reduces exposure
without providing containment - the agent runs as the same user, so
file modes stop nothing and a shell reader is not bound by a tool rule. The
permission pre-flight therefore still requires a deny rule covering
`$REVIEW_AUTH`, alongside the `git push` denials, and refuses to start without
one; the Install block above gives the exact settings. Note that a rule written
for the old layout - `Read(~/review/**)` - no longer covers the accounts, and is
refused. The migrator preserves that rule and adds protection for the new root.
A refusal is written to the run log and shows on the
dashboard: these checks used to run before the log was opened, so under cron a
refused run said nothing anywhere and the slot simply went quiet.

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
mid-run cannot move the work to another account. A missing `default` link uses
the tool's own directory; every other missing role is refused.

Switching takes a per-role lock. Two switches of the same role could otherwise
interleave, and a switch that turned out to be invalid would revert over one that
had already succeeded. If a revert cannot be made, `use` says so and names the
account the role has been left on rather than reporting success.

### Reading what an account has left

`runner/bin/quota.py` asks each account what quota it has, and records it.
Observation only - nothing selects an account on it yet, which is the point:
the readings get checked against reality before anything depends on them.

```
quota.py            a table of every account
quota.py --json     the same as records
quota.py --record   append them to $REVIEW_LOGS/quota.jsonl
```

The dashboard is published, so the identities in it are masked - an address
keeps its domain and an account id its first eight characters, which is enough
to tell the right account from the wrong one and not enough to reuse. The whole
value is on the box, from `review-auth.sh status` or `quota.py`.

Cron records a reading for every account hourly, at :05, and the runs dashboard
has a **Quotas** section showing the newest one per account: what is left, the
windows behind that figure, when the soonest rolls over, and how old the reading
is. The page reads the recorded file - it is rebuilt every ten minutes, and
asking an account for its quota starts the CLI, which is how runs and probes
come to contend for the OAuth refresh.

Both CLIs publish structured figures, which is worth knowing because the
hourly probe still reads Claude's by regex over English prose:

| tool | where | windows |
|---|---|---|
| claude | `claude -p /usage --output-format stream-json`, `usage_report.rate_limits` | session (5h) and weekly, plus per-model |
| codex | `codex app-server`, `account/rateLimits/read` | whatever the plan has - a Pro account reports weekly only |

Two things the structured answers settle that the old sources could not. Codex's
is a live query rather than the last figure a session rollout happened to
record, and it carries `ordinaryUsageAllowed` and a credit balance - which is
what decides whether running out means stopping or starting to cost money.
Claude's arrives as a window list with reset times rather than a sentence.

`free_pct` is what is left of the worst window that gates ordinary work.
Per-model windows are recorded but do not count towards it: a spent model is not
an account that cannot start. The app-server shuts down when its stdin closes,
so the query has to hold the pipe open - writing the request and closing it
loses the reply.

### Choosing an account

`runner/bin/accounts.py` decides which account a role runs on, from what the
accounts have left.

```
accounts.py                what every role would select, and why
accounts.py --role rsync   one role
accounts.py --record       append the decisions to $REVIEW_LOGS/select.jsonl
accounts.py --live         ask the accounts rather than using a recent reading
accounts.py --select --role R   the answer a run acts on: "tool<TAB>name<TAB>dir"
                                per line, the walk on stderr, exit 3 for nothing
                                usable
```

The policy is `$REVIEW_AUTH/policy.json`, copied from
`runner/etc/policy.json.example` - an ordered list of account names per tool and
role. Order is priority, and the list is also the whole of what that role may
reach. That second part is the one that matters: `rsync` reviews a project the
ArduPilot subscription does not pay for, so its lists name only the accounts
that may pay for it, and running out of its own cannot fall through to one that
may not. A missing or invalid policy is refused rather than defaulted.

An account is used when more than `min_free_pct` is left. Unknown is not spare -
a reading that failed, or one carrying no figure, is skipped rather than tried,
because an unattended run cannot check the guess. Nor is credit: an account past
its included allowance (`ordinaryUsageAllowed` false) is passed over however
healthy its window looks, because these runs may stop but may not start billing.
Figures come from the hourly recording while it is younger than `fresh_minutes`,
and from the account itself otherwise: asking starts the CLI, and that is what
makes two processes contend for the OAuth refresh below.

### What a run does with it

`run-reviewprs.sh` selects **after taking the run lock**, not before. A run that
waited two hours for the lock would otherwise choose on a reading taken before
the wait, and a run that never gets the lock would pin - and be charged for - an
account it never spends.

Every check that follows - signed in, subscription login, first-party provider,
the directory the CLI actually read, the `ACCOUNT` record, Codex's `config.toml`
- runs against whatever was selected, so nothing is validated any less than when
the role links decided alone. The runner also re-checks the chosen directory
against the same containment and ownership rules a role link must satisfy
(`account_dir_ok`), rather than taking `accounts.py`'s word for the path.

With every listed account spent, the run **defers**: it prints
`status=deferred-no-quota`, starts nothing, and leaves the slot to the next
scheduled run. That is deliberate - running the review without the Codex
validation pass was the alternative, and a review is not worth having without
it. The dashboard shows those runs as `deferred`, counted under *Blocked on
quota* rather than as failures.

Three ways out of the selector, and they are different:

| situation | what happens |
| --- | --- |
| no `policy.json` | the feature is not configured; the role links decide, as before |
| `policy.json` unreadable or invalid | `status=account-policy-error`, the run stops |
| policy fine, every account spent | `status=deferred-no-quota`, the run defers |

The second is not the first. A policy that is meant to be in force and is not
would leave a run choosing its own account, which is the one thing this must
never do.

### The OAuth refresh lock

`claude` takes a lock in its config directory while it refreshes the OAuth
token, and leaves it behind if the refresh fails - reporting the failure as its
output and exiting zero. Anything that starts next finds it and dies with
"another Claude Code process is refreshing it".

A run clears a stale one before it starts, and again immediately before
launching the agent. The second clear is the one that matters: between them the
run takes its own usage reading, and `claude -p /usage` is exactly the sort of
short invocation that meets an expired token, fails to refresh, and leaves the
lock a few seconds before the agent needs it. That was the 2026-09-19 19:47
followup and three others in the five days before it - each one a whole slot
lost, three hours apart.

The probe says so now when it comes back with nothing, which it did not: a
non-zero exit was swallowed by `|| exit 0`, and a reply carrying prose rather
than a meter simply recorded nothing. Either way the run log showed no probe at
all, so the only sign was a reading missing from the history.

## Compiling what pymavlink generates

A review of a generator change can execute its output rather than read it.
`bin/install-gen-toolchains.sh` installs the compilers; this section is the part
that is not guessable - what each language needs beyond its own compiler.

pymavlink's own CI generates and never compiles, so an output that does not
build is not something upstream would have caught.

The definitions are not in the pymavlink checkout (`message_definitions` is a
submodule that is not fetched). They are in the mavlink clone:

```
D=$HOME/review/repositories/upstream-mavlink/message_definitions/v1.0
export PYTHONPATH=$HOME/review/repositories
cd $HOME/review/repositories/pymavlink
python3 -m pymavlink.tools.mavgen --lang=<L> --wire-protocol=2.0 -o <out> $D/<dialect>.xml
```

| language | compiler | builds |
| --- | --- | --- |
| C, C++11 | gcc, g++ | yes |
| Python, JavaScript | interpreters | yes |
| Lua, WLua | `luac5.4 -p` | yes |
| TypeScript | `tsc --noEmit` | no (see below) |
| Java | `javac` | v1 and v2 |
| Ada | `gprbuild`, then run `obj/test` | v1 and v2, `minimal` and `standard` |
| CS | `dotnet build -f netstandard2.0` | v1 and v2 |
| ObjC | `gcc -x objective-c` + GNUstep | v1 140/144, v2 228/237 (see below) |
| Swift | `swiftc` | v1 only (see below) |
| Spin2 | `flexspin -2` | v1 and v2 |

**Java** needs nothing special:

```
javac -nowarn -d build $(find com -name '*.java')
```

**Ada** builds the test project the generator writes, and the binary runs:

```
cd <out>/tests && gprbuild test.gpr && ./obj/test
```

**CS** is an SDK-style project with NuGet PackageReferences, so the build needs
network the first time. `net461` builds too, via the reference-assemblies
package; `-f netstandard2.0` alone is enough to compile-check the generated code.

**ObjC** needs three things the generated tree does not say:

- the **C** output as well, on the include path: the ObjC is a wrapper over it
  and `MVMessage.h` imports `mavlink.h`
- **every dialect subdirectory** as a `-I`, because `MVMavlink.h` imports each
  dialect's aggregate header by bare name
- **Foundation forced in**, because no generated header imports it - the code
  uses `NSObject`, `NSData` and `NSString` and assumes a prefix header, the way
  Xcode supplies one

```
INC="-I."; for d in <out>/*/; do INC="$INC -I$d"; done
gcc -c -x objective-c $(gnustep-config --objc-flags) -include Foundation/Foundation.h \
    $INC -I<c-out> -I<c-out>/<dialect> <file>.m -o <file>.o
```

**Swift** has no build file; compile the sources as a module:

```
cd <out> && swiftc -emit-module -module-name MAVLink -o /tmp/m.swiftmodule $(find . -name '*.swift')
```

**Spin2** emits one file, named after `--output` rather than placed in it, so
`-o <out>` writes `<out>.spin2`. `-2` selects the P2, and `Lib` has to be on the
include path or nothing resolves:

```
flexspin -2 -L /opt/flexspin/Lib -o /tmp/out.binary <out>.spin2
```

**TypeScript** writes into `<out>/enums` and `<out>/messages` without creating
them, so generation exits 1 with no message unless they exist already. Typecheck
with its own `tsconfig.json` and no file arguments - naming files on the command
line makes `tsc` stop at `TS5112` before checking anything, which looks exactly
like success:

```
mkdir -p <out>/enums <out>/messages
cd <out> && tsc --noEmit --skipLibCheck
```

### Six defects this found

All six are in master, none is new, and none is reachable without a compiler.

- **ObjC, any field with a multi-line description.** `mavgen_objc.py:269` emits
  `//! ${description}` - a one-line comment for text that is not one line, so
  every line after the first lands in the header as code. `MISSION_COUNT`'s
  `opaque_id` produces `This field is used when...` where a declaration belongs.
  Eight of `common.xml`'s generated files will not compile: `MISSION_COUNT`,
  `MISSION_ACK`, `HOME_POSITION`, `RADIO_RC_CHANNELS`, `STORAGE_INFORMATION`,
  `AUTOPILOT_VERSION`, and the two aggregates that import them. The template
  line dates from the original generator in 2013; the descriptions grew
  multi-line later.
- **ObjC, a field called `description`.** `OPEN_DRONE_ID_SELF_ID` has one, and
  the accessor collides with `NSObject`'s own `-description`:
  `redefinition of '-[MVMessageOpenDroneIdSelfId description]'`. Nothing in the
  generator reserves the names the base class already uses.
- **Swift generates MAVLink 2 that cannot compile.** `mavgen_swift.py:148`
  writes `public static let id = UInt8(${id})`. Message IDs are 8-bit in v1 and
  24-bit in v2, so every v2 message numbered above 255 fails with
  `integer literal '12900' overflows when stored into 'UInt8'` - nine of them in
  `common.xml`, all OpenDroneID. The file has no reference to
  `wire_protocol_version` at all: the generator is v1-only and says nothing when
  asked for v2. `test_generate_all.sh` runs `--lang='Swift' --wire-protocol=2.0`
  and passes, because it only generates.
- **TypeScript has the same multi-line description leak as ObjC.** The comment
  is `// ${description}` on one line, so everything after the first line of
  `MAV_CMD_DO_FIGURE_EIGHT`'s description becomes TypeScript. 1761 errors across
  `mav-cmd`, `mav-frame`, `mav-ftp-err`, `mav-protocol-capability`,
  `mav-standard-mode`, `autopilot-version` and `storage-information`.
- **TypeScript does not create its own output directories.** `generate()` opens
  `<out>/enums/...` without `mkdir`, so a first run into a fresh directory dies
  with `FileNotFoundError` that mavgen reports only as exit 1.
- **Ada, `common.xml` and `ardupilotmega.xml`.** `mavgen_ada.py:459` asserts
  `types_size[f.enum] == f.type_length` and raises `Different size for one enum`,
  so generation stops with a traceback before writing anything. `minimal` and
  `standard` generate, build and pass their generated test. Whether
  `generator/Ada/v2/test.sh` ever passed depends on the definitions it was run
  against - the assert is about the XML, not the generator - but against the
  current upstream definitions it stops on its second dialect.

## The APReview Results board

[github.com/orgs/ArduPilot/projects/33](https://github.com/orgs/ArduPilot/projects/33) —
every open PR carrying a trigger label that this system has posted a verdict on,
with a sortable **Result** column: `ACCEPT`, `COMMENT`, `REQUEST CHANGES`.

`bin/project-sync.py` makes the board match reality. It takes no arguments and
is told nothing by the run that calls it: it asks GitHub what is labelled, open
and reviewed, and adds, relabels or removes rows to suit. The same command is
therefore correct at the end of a review and from cron a quarter of an hour
later.

```
project-sync.py                what it does on the two triggers below
project-sync.py --dry-run      say what would change, change nothing
project-sync.py --show         the verdict it reads for each PR, and from where
project-sync.py --prune-only   only remove what no longer belongs
```

Two triggers, both calling `bin/project-sync.sh`, which cannot fail its caller:

- the EXIT trap of every run, beside the dashboard publish
- `*/15 * * * *`, which is what takes a merged or closed PR off — nothing tells
  us a PR closed, it simply stops coming back from the search

### Where the verdict comes from

The posted comment, not the published report. Each label's report page is
overwritten by the next run of that label, so it only ever covers the most
recent sweep: measured on 2026-09-26, the reports held a verdict for **35** of
the 168 open labelled PRs, against **166** from the comments. Where both had one
they disagreed ten times, the report being the stale one.

`apreview_verdict.py` reads it, from two sources:

1. **A marker the review emits**, which is exact and renders as nothing:
   `<!-- apreview: verdict=ACCEPT head=5574a60eb2 -->`. Written by step 8 of
   `commands/reviewprs.md`.
2. **Failing that, the prose.** Seven phrasings appear in the comments already
   posted, so this is not a one-line regex:

   ```
   **Verdict: COMMENT - no blockers.**       Verdict: **COMMENT** - ...
   Verdict: COMMENT                          **REQUEST CHANGES** - ...
   **APPROVE - no blockers.**                ## COMMENT - one real gap
   **... Verdict stays APPROVE.**
   ```

   Every anchor requires something that means *this is the verdict* - a
   "Verdict" label, a heading, or the start of a bold run - because the bare
   words are everywhere in ordinary review prose. Ten of the 168 name a second
   verdict within 400 characters of the right one.

   `the verdict moves from REQUEST CHANGES to COMMENT` is handled on its own and
   first: every other anchor reads it backwards and takes the old verdict. Four
   PRs were wrong that way before it existed.

The verdict is the newest comment that **states** one, not the newest comment. A
followup note says only that the author's code moved and carries no verdict;
treating that as "no review" would drop a reviewed PR off the board. Superseded
comments are skipped, so a stale verdict folded into a deprecation cannot
outrank a live followup that has none. That rule alone took coverage from 77% to
94%; the seven phrasings took it to 166 of 168. The two it cannot read state no
verdict at all - a draft, reviewed as guidance - and are left off rather than
guessed at.

A comment counts as ours only if it carries the `AI-generated` marker *and* was
posted by one of `REVIEW_COMMENT_ACCOUNTS`. Author alone is not enough:
commenting moved from a person's account to the bot, so the older reviews are
authored by someone who also writes ordinary comments, and "I would request
changes here" is not a verdict.

### Refusing to empty the board

A sweep that returns nothing looks exactly like every PR having merged. More
than 25 removals, or more than a quarter of the board, is refused with exit 3
and nothing changed; `--force-prune` overrides it when the news is real.

## Publishing

Reports go out with `rsync`, either to an rsync daemon (`rsync://user@host` plus a
password file) or over ssh (`host:path`, no auth option). Set `REVIEW_PUBLISH`,
`RSYNC_AUTH` and `REVIEW_PUBLIC_URL` in `local.conf`; the command reads its own
previous reports back from `REVIEW_PUBLIC_URL` and links to it from the comments it
posts. With `REVIEW_PUBLISH` unset, a run keeps its report local and says so.
