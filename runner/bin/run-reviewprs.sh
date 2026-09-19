#!/bin/bash
#
# Cron entry point for the reviewprs workflow.
#
#   run-reviewprs.sh               -> /reviewprs          (the four-label sweep)
#   run-reviewprs.sh all           -> /reviewprs          (same thing, explicit)
#   run-reviewprs.sh followup      -> /reviewprs followup
#   run-reviewprs.sh rsync         -> /reviewprs rsync
#   run-reviewprs.sh AIReview      -> /reviewprs AIReview  (any label)
#
# No argument means the all-labels sweep, matching the skill's own convention
# that a bare /reviewprs is the four-label run. Do not change this to default to
# followup: the two differ by hours of work and the wrong one is silently plausible.
#
# All runs are serialised on one lock: the three label sub-runs share
# devcall_pr_reviews.html, and followup reads what they publish, so two
# concurrent runs would corrupt each other's output.
#
# The log is line-buffered and streamed, so `tail -f` is useful mid-run.

set -u

# --dry-run: do every pre-flight and then stop, printing the command that would
# have run. Nothing is launched, no log or dashboard row is written, no usage
# reading is taken and the run lock is left alone, so it is safe while a real run
# is in flight. It exists because the only way to exercise the guards used to be
# to start a review and interrupt it - which on 2026-09-16 twice started one for
# real, against an ArduPilot PR and then an rsync PR.
DRY=0
ARGS=""
for a in "$@"; do
    case "$a" in
        --dry-run) DRY=1 ;;
        *) ARGS="${ARGS:+$ARGS }$a" ;;
    esac
done
set -- $ARGS
MODE="${1:-all}"

# The skill accepts `rsync`, `RSYNC`, `--rsync` and `/rsync` as the same mode, so
# the runner must too: `run-reviewprs.sh RSYNC` used to miss the account guard
# below and run tridge's own project on the ArduPilot subscription. Only the
# reserved words are folded - label names like AIReview are case-sensitive.
MODE="${MODE#--}"; MODE="${MODE#/}"
case "$(printf %s "$MODE" | tr "[:upper:]" "[:lower:]")" in
    rsync)    MODE=rsync ;;
    followup) MODE=followup ;;
    all)      MODE=all ;;
esac

. "$HOME/review/bin/review-env.sh"

# ArduPilot's venv carries pymavlink, empy, pexpect etc.
[ -f "$HOME/venv-ardupilot/bin/activate" ] && . "$HOME/venv-ardupilot/bin/activate"

# Per-mode Claude account, chosen before anything can spend quota: the EXIT trap
# below takes a closing usage reading, and on a lock-skipped rsync run that would
# otherwise be charged to - and recorded against - the wrong account.
# Which account this task runs as. `rsync` mode, and a single PR in that project
# asked for by hand, are the non-ArduPilot target and use the rsync role;
# everything else uses the default role. The roles are symlinks under auth/, so
# moving a workload to another subscription - when a weekly quota runs out, say -
# is `review-auth.sh use claude default personal` and nothing else.
ROLE=default
case "$(printf %s "$MODE" | tr "[:upper:]" "[:lower:]")" in
    rsync|rsyncproject/rsync\#*|https://github.com/rsyncproject/rsync/pull/*) ROLE=rsync ;;
esac
# Every path sets or unsets both variables. Leaving an inherited value in place
# was a hole: a CLAUDE_CONFIG_DIR already in the environment survived whenever the
# role resolved to the tool's own directory, and then decided the account.
select_account() {           # tool VAR -> exports VAR, or unsets it
    local tool="$1" var="$2" d rc
    d=$(review_auth "$tool" "$ROLE"); rc=$?
    case "$rc" in
        2) echo "FATAL: cannot use the $tool account for role $ROLE"
           echo "       review-auth.sh status   shows what each role resolves to"
           echo "finish=$(date -Is) status=wrong-$tool-account"
           exit 1 ;;
        1) ;;   # no link for the default role: the tool's own directory
    esac
    # Decide from the path review_auth just returned, not from a second
    # resolution: a switch landing between the two reads would give an answer
    # the return code above was never checked against.
    d=$(role_config_dir "$tool" "$ROLE" "$d")
    if [ -n "$d" ]; then export "$var=$d"; else unset "$var"; fi
}
STAMP=$(date +%Y%m%d_%H%M%S)
# The mode can be a PR reference - ArduPilot/ardupilot#34206 from review-now.sh -
# which cannot go in a filename: the slash names a directory that does not exist.
# `exec >>"$LOG"` then fails, and bash prints the error and CARRIES ON (it exits
# only in posix mode), so the run happens but is invisible: no log, no latest-
# symlink, no row on the dashboard, and under cron its output goes nowhere at
# all. Observed on 2026-09-16 - a review of #33032 ran to completion unlogged.
# Flatten it for the filename only; for every other mode the name is unchanged.
TAG=$(printf %s "$MODE" | tr '/#' '--')
LOG="$REVIEW_LOGS/reviewprs-${TAG}-${STAMP}.log"
LATEST="$REVIEW_LOGS/latest-${TAG}.log"
LOCK="$REVIEW_ROOT/etc/reviewprs.lock"
mkdir -p "$REVIEW_LOGS" "$REVIEW_ROOT/etc"

if [ "$DRY" = 1 ]; then
    echo "DRY RUN: mode=$MODE  host=$(hostname)"
    echo "  log would be:   $LOG"
fi

# Keep 30 days of logs; they are the only record of an unattended run.
[ "$DRY" = 1 ] || find "$REVIEW_LOGS" -name 'reviewprs-*.log' -mtime +30 -delete 2>/dev/null

# A stable name to tail, always pointing at the newest run of this mode. A dry
# run writes no log at all: it must not appear on the runs dashboard as a run
# that happened, and must not move the symlink someone is tailing.
if [ "$DRY" = 0 ]; then
    ln -sfn "$LOG" "$LATEST"
    exec >>"$LOG" 2>&1
fi
echo "=============================================================="
echo "reviewprs mode=$MODE  host=$(hostname)  start=$(date -Is)"
echo "REVIEW_DATA=$REVIEW_DATA  TMPDIR=$TMPDIR"
echo "follow with:  tail -f $LATEST"
echo "=============================================================="

# Account selection comes after the redirect, not before it. These refusals used
# to run at the top of the script, where there is no log yet: under cron, with
# MAILTO empty, a refused run said nothing anywhere and left no row on the
# dashboard - the rsync slot would simply go quiet. Nothing above this point
# spends quota or touches an account, so it loses nothing by waiting.
# The role a run selected, for the children that take their own readings.
export REVIEW_ROLE="$ROLE"
if ! clear_inherited_credentials; then
    echo "FATAL: these could not be removed from the environment and would"
    echo "       decide the account instead of the role:$CLEARED_FAILED"
    echo "finish=$(date -Is) status=wrong-claude-account"
    exit 1
fi
[ -z "$CLEARED_VARS" ] || echo "cleared from the environment: $CLEARED_VARS"
# Belt and braces: select_account unsets on the path where the role resolves to
# the tool's own directory, and the sweep above clears anything inherited.
unset CLAUDE_CONFIG_DIR CODEX_HOME 2>/dev/null || true
select_account claude CLAUDE_CONFIG_DIR
select_account codex  CODEX_HOME
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
if [ "$DRY" = 1 ]; then
    echo "  claude config:  $CLAUDE_DIR (role $ROLE)"
    echo "  codex home:     $CODEX_DIR"
fi

# Lock policy. REVIEWPRS_LOCK_WAIT is seconds to wait for the lock:
#   0 (default)  - skip this slot if busy. Right for the frequent cron jobs,
#                  where the next slot is along soon anyway.
#   >0           - wait up to that long, then give up. Right for an on-demand
#                  run you actually want to happen, and for once-a-day jobs
#                  that would otherwise never run at all if they lose the race.
WAIT="${REVIEWPRS_LOCK_WAIT:-0}"
# Keep runs.html current no matter how this run ends - completed, skipped or
# failed. A skipped slot is exactly the kind of thing the page should show.
# On the way out: reap anything the run left running under $REVIEW_DATA, take a
# closing usage reading, then refresh the dashboard. The reaper runs first and
# its output goes to the run log, so a leak is visible where you would look for
# it rather than discovered days later by the fans.
LOCKED=0
[ "$DRY" = 1 ] || \
trap 'FR=""; [ "$LOCKED" = 1 ] && FR="--from-run"; \
      "$HOME/review/bin/reap-orphans.sh" $FR 2>&1; \
      "$HOME/review/bin/claude-usage-probe.sh" end >/dev/null 2>&1; \
      "$HOME/review/bin/publish-runs-page.sh" >/dev/null 2>&1 || true' EXIT

if [ "$DRY" = 1 ]; then
    flock -n 9 9>"$LOCK" && echo "  run lock: free" || echo "  run lock: held by a run in flight (a real run would wait or skip)"
else
exec 9>"$LOCK"
if [ "$WAIT" -gt 0 ]; then
    echo "waiting up to ${WAIT}s for the run lock..."
    if ! flock -w "$WAIT" 9; then
        echo "GAVE UP: lock still held after ${WAIT}s"
        echo "finish=$(date -Is) status=lock-timeout"
        exit 1
    fi
    echo "lock acquired at $(date -Is)"
else
    if ! flock -n 9; then
        echo "SKIPPED: another reviewprs run holds $LOCK"
        echo "finish=$(date -Is) status=skipped-locked"
        exit 0
    fi
fi
echo $$ >&9
LOCKED=1
fi

case "$MODE" in
    all) PROMPT="/reviewprs" ;;
    *)   PROMPT="/reviewprs $MODE" ;;
esac

cd "$REVIEW_ROOT/work" || {
    echo "FATAL: no $REVIEW_ROOT/work"
    echo "finish=$(date -Is) status=no-work-dir"
    exit 1
}

clear_stale_oauth_lock "$CLAUDE_DIR"

# Which account is this? Two sources: the directory's own record, and the CLI.
# Where both answer they must agree - matching one local record against another
# does not establish which subscription pays, and a disagreement is exactly the
# case worth stopping for.
# It also says which credential it used and where it read it from, which settles
# what no amount of environment sweeping can: an inherited token, a cloud
# provider, or a different config directory all show up here.
# One field per line, not six words: an account directory whose path contains a
# space made the guard read part of the path as the field count.
{ IFS= read -r CLAUDE_LOGGED; IFS= read -r CLAUDE_CLI_ACCOUNT; IFS= read -r CLAUDE_METHOD
  IFS= read -r CLAUDE_PROVIDER; IFS= read -r CLAUDE_CLI_DIR; IFS= read -r CLAUDE_META; } <<EOS
$(reply=$(claude auth status --json 2>/dev/null) || reply=""
  printf '%s\n' "$reply" | python3 -c '
import json, re, sys
keys = ("authMethod", "apiProvider", "configDirectory")
try:
    d = json.load(sys.stdin)
    if not isinstance(d, dict) or type(d.get("loggedIn")) is not bool:
        raise ValueError("invalid login state")
    for k in ("email", "authMethod", "apiProvider", "configDirectory"):
        if k not in d:
            continue
        v = d[k]
        if k == "email" and v is None:
            continue
        if not isinstance(v, str) or any(ord(c) < 32 or ord(c) == 127 for c in v):
            raise ValueError("invalid field")
        if k != "email" and (not v or v == "-"):
            raise ValueError("empty metadata")
        if k == "email" and v and (len(v) > 200 or not re.fullmatch(
                r"[A-Za-z0-9._+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z0-9\-]+", v)):
            raise ValueError("invalid email")
except Exception:
    print("no\n-\n-\n-\n-\n0"); raise SystemExit
out = [("yes" if d.get("loggedIn") else "no"), (d.get("email") or "-"),
       (d.get("authMethod") or "-"), (d.get("apiProvider") or "-"),
       (d.get("configDirectory") or "-"), str(sum(1 for k in keys if d.get(k)))]
print("\n".join(out))
' 2>/dev/null || printf 'no\n-\n-\n-\n-\n0\n')
EOS
CLAUDE_REC_ACCOUNT=$(python3 - "$CLAUDE_DIR" <<'PYA' 2>/dev/null
import json, os, re, sys
try:
    acct = json.load(open(os.path.join(sys.argv[1], ".claude.json"))).get("oauthAccount") or {}
    email = acct.get("emailAddress") or ""
    if not isinstance(email, str) or len(email) > 200 or (email and not re.fullmatch(
            r"[A-Za-z0-9._+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z0-9\-]+", email)):
        print("invalid-record")
    else:
        print(email or "-")
except Exception:
    print("-")
PYA
)
if [ "$CLAUDE_REC_ACCOUNT" = invalid-record ]; then
    echo "FATAL: $CLAUDE_DIR/.claude.json has an invalid identity record"
    echo "finish=$(date -Is) status=wrong-claude-account"
    exit 1
fi
if [ "$CLAUDE_LOGGED" != "yes" ]; then
    echo "FATAL: $CLAUDE_DIR is not signed in (role $ROLE)"
    echo "       review-auth.sh login claude <account>   says how"
    echo "finish=$(date -Is) status=wrong-claude-account"
    exit 1
fi
# claude.ai is a subscription login. oauth_token, apiKey and third_party are not:
# they bill a token or a cloud account, and the directory goes on reporting the
# address it was last signed in as either way.
# Without all three fields, neither the credential store nor billing provider
# has been established. Older CLIs must be upgraded before this runner can run.
if [ "$CLAUDE_META" != 3 ]; then
    echo "FATAL: claude reported only part of its authentication state"
    echo "       ($CLAUDE_META of authMethod, apiProvider, configDirectory)"
    echo "finish=$(date -Is) status=wrong-claude-account"
    exit 1
fi
case "$CLAUDE_METHOD" in
    claude.ai|-) ;;
    *) echo "FATAL: claude authenticated with $CLAUDE_METHOD, not a subscription"
       echo "       login, so role $ROLE would not bill the account it names"
       echo "finish=$(date -Is) status=wrong-claude-account"
       exit 1 ;;
esac
case "$CLAUDE_PROVIDER" in
    firstParty|-) ;;
    *) echo "FATAL: claude is using the $CLAUDE_PROVIDER provider, which bills a"
       echo "       cloud account rather than the subscription role $ROLE names"
       echo "finish=$(date -Is) status=wrong-claude-account"
       exit 1 ;;
esac
# The directory it actually read, not the one we asked for.
if [ "$CLAUDE_CLI_DIR" != "-" ] \
   && [ "$(readlink -f "$CLAUDE_CLI_DIR" 2>/dev/null)" != "$(readlink -f "$CLAUDE_DIR")" ]; then
    echo "FATAL: claude read its credentials from $CLAUDE_CLI_DIR, not the"
    echo "       $CLAUDE_DIR chosen for role $ROLE"
    echo "finish=$(date -Is) status=wrong-claude-account"
    exit 1
fi
if [ "$CLAUDE_CLI_ACCOUNT" != "-" ] && [ "$CLAUDE_REC_ACCOUNT" != "-" ] \
   && [ "$CLAUDE_CLI_ACCOUNT" != "$CLAUDE_REC_ACCOUNT" ]; then
    echo "FATAL: $CLAUDE_DIR records $CLAUDE_REC_ACCOUNT but the CLI reports $CLAUDE_CLI_ACCOUNT"
    echo "finish=$(date -Is) status=wrong-claude-account"
    exit 1
fi
ACCOUNT="$CLAUDE_CLI_ACCOUNT"
[ "$ACCOUNT" = "-" ] && ACCOUNT="$CLAUDE_REC_ACCOUNT"
[ "$ACCOUNT" = "-" ] && ACCOUNT=""
# Existence and validity are separate questions. `-f` is false for a dangling
# symlink and for a directory, so testing it alone let a constraint disappear
# because reading it failed - the reassuring half of a broken setup.
if [ -e "$CLAUDE_DIR/ACCOUNT" ] || [ -L "$CLAUDE_DIR/ACCOUNT" ]; then
    WANT=$(read_account_file "$CLAUDE_DIR/ACCOUNT") || {
        echo "FATAL: $CLAUDE_DIR/ACCOUNT is not a plain account record"
        echo "finish=$(date -Is) status=wrong-claude-account"
        exit 1; }
    if [ -z "$ACCOUNT" ]; then
        echo "FATAL: $CLAUDE_DIR records $WANT but the signed-in address could not"
        echo "       be determined, so the record cannot be checked"
        echo "finish=$(date -Is) status=wrong-claude-account"
        exit 1
    fi
    if [ "$ACCOUNT" != "$WANT" ]; then
        echo "FATAL: $CLAUDE_DIR records $WANT but is signed in as $ACCOUNT"
        echo "       either sign it back in, or update its ACCOUNT file"
        echo "finish=$(date -Is) status=wrong-claude-account"
        exit 1
    fi
fi
echo "claude account: ${ACCOUNT:-signed in, address not reported}  (role $ROLE, config $CLAUDE_DIR)"

# Codex had no check at all: a missing or malformed auth.json only surfaced when
# the validation pool failed, well into the run.
CODEX_ACCOUNT=$(read_codex_account "$CODEX_DIR")

# auth.json says which account signed in; config.toml says where the request
# goes and which key pays for it. A custom provider sends another key to another
# endpoint while auth.json goes on naming the subscription.
CODEX_PROVIDER=$(check_codex_config "$CODEX_DIR")

if [ -n "$CODEX_PROVIDER" ]; then
    echo "FATAL: $CODEX_DIR/config.toml selects $CODEX_PROVIDER, so role $ROLE"
    echo "       would not bill the account auth.json names"
    echo "finish=$(date -Is) status=wrong-codex-account"
    exit 1
fi
if [ "$CODEX_ACCOUNT" = unsupported-auth-mode ]; then
    echo "FATAL: $CODEX_DIR has an unsupported Codex authentication mode"
    echo "finish=$(date -Is) status=wrong-codex-account"
    exit 1
fi
if [ "$CODEX_ACCOUNT" = api-key ]; then
    echo "FATAL: $CODEX_DIR authenticates with an API key, not a subscription"
    echo "       account, so role $ROLE would bill whoever owns that key"
    echo "finish=$(date -Is) status=wrong-codex-account"
    exit 1
fi
if [ -z "$CODEX_ACCOUNT" ]; then
    echo "FATAL: $CODEX_DIR has no usable codex credentials (role $ROLE)"
    echo "       review-auth.sh login codex <account>   says how"
    echo "finish=$(date -Is) status=wrong-codex-account"
    exit 1
fi
if [ -e "$CODEX_DIR/ACCOUNT" ] || [ -L "$CODEX_DIR/ACCOUNT" ]; then
    WANT=$(read_account_file "$CODEX_DIR/ACCOUNT") || {
        echo "FATAL: $CODEX_DIR/ACCOUNT is not a plain account record"
        echo "finish=$(date -Is) status=wrong-codex-account"
        exit 1; }
    if [ "$CODEX_ACCOUNT" != "$WANT" ]; then
        echo "FATAL: $CODEX_DIR records $WANT but is signed in as $CODEX_ACCOUNT"
        echo "finish=$(date -Is) status=wrong-codex-account"
        exit 1
    fi
fi
echo "codex account:  $CODEX_ACCOUNT  (role $ROLE, home $CODEX_DIR)"

# Pre-flight: refuse to run if the permission rules are not in force. This box
# runs unattended, so a settings.json that has lost its deny list must stop the
# run, not silently grant it. "bypassPermissions" is deliberately NOT used
# below: it bypasses deny rules too, which would re-enable git push.
SETTINGS="$CLAUDE_DIR/settings.json"
if ! REVIEW_AUTH="$REVIEW_AUTH" python3 - "$SETTINGS" <<'PYCHK'
import json,os,re,sys
try:
    d=json.load(open(sys.argv[1]))
except Exception as e:
    print("FATAL: cannot read settings.json: %s" % e); sys.exit(1)
permissions = d.get("permissions") if isinstance(d, dict) else None
deny = permissions.get("deny") if isinstance(permissions, dict) else None
if not isinstance(deny, list) or not all(isinstance(r, str) for r in deny):
    print("FATAL: permissions.deny must be an array of rules"); sys.exit(1)
need=["Bash(git push)","Bash(git push:*)"]
missing=[r for r in need if r not in deny]
if missing:
    print("FATAL: settings.json is missing deny rules: %s" % missing); sys.exit(1)
# This is a file-tool guardrail, not containment of arbitrary shell readers.
def denies_auth(rule):
    if not isinstance(rule, str) or not rule.startswith("Read(") or not rule.endswith("/**)"):
        return False
    # The CLI unescapes the rule before compiling the gitignore pattern.
    path = rule[5:-4].replace("\\\\", "\\")
    # Only accept anchored recursive rules whose coverage we can prove. A
    # current-directory rule changes meaning when a subagent changes directory.
    literal = []
    chars = iter(path)
    for c in chars:
        if c == "\\":
            c = next(chars, "")
            if c not in ("\\", "*", "?", "[", "]"):
                return False
        elif c in "*?[]":
            return False
        literal.append(c)
    path = "".join(literal)
    # Extra separators can change which root the CLI anchors the pattern to.
    anchored = path[2:] if path.startswith(("//", "~/")) else path
    if path not in ("/", "~") and (not anchored or anchored.startswith("/") or
                                   anchored.endswith("/") or "//" in anchored):
        return False
    # A filesystem resolver collapses these; a gitignore pattern does not.
    if any(part in (".", "..") for part in path.split("/")) or any(ord(c) < 32 for c in path):
        return False
    if path == "/":
        pass                              # Read(//**) covers the filesystem
    elif path == "~":
        path = os.path.expanduser("~")
    elif path.startswith("//"):
        path = path[1:]
    elif path.startswith("~/"):
        path = os.path.join(os.path.expanduser("~"), path[2:])
    else:
        return False
    root = os.path.realpath(path)
    return os.path.commonpath([root, os.path.realpath(auth)]) == root

auth=os.environ.get("REVIEW_AUTH","")
if auth and not any(denies_auth(r) for r in deny):
    print("FATAL: settings.json does not deny reading %s," % auth)
    print("       which holds every account's credentials.")
    pattern = re.sub(r"([\\*?\[\]])", r"\\\1", os.path.realpath(auth))
    pattern = pattern.replace("\\\\", "\\\\\\\\")
    print("       Add this deny rule: %s" % json.dumps("Read(/%s/**)" % pattern))
    print("       Use Read(//absolute/path/**), Read(~/path/**), or an ancestor.")
    sys.exit(1)
if d.get("permissions",{}).get("defaultMode")!="auto":
    print("FATAL: permissions.defaultMode is not 'auto'"); sys.exit(1)
print("permission pre-flight OK: git push denied, %s denied, defaultMode=auto"
      % os.path.basename(auth.rstrip("/") or "auth"))
PYCHK
then
    echo "ABORTING: permission pre-flight failed"
    echo "finish=$(date -Is) status=preflight-failed"
    exit 1
fi

# gh is what actually gates a run: unauthenticated it is 60 requests/hour and
# the funnel alone needs more than that, so fail fast and clearly.
if ! gh auth status >/dev/null 2>&1; then
    echo "FATAL: gh is not authenticated. Run 'gh auth login' on this box."
    echo "finish=$(date -Is) status=no-gh-auth"
    exit 1
fi
echo "gh pre-flight OK: $(gh auth status 2>&1 | sed -n 's/.*Logged in to [^ ]* account \([^ ]*\).*/\1/p' | head -1)"

# Bracket the run with real usage readings so the dashboard can show what this
# run itself consumed, rather than inferring it from token arithmetic.
# Quota pre-flight. Between 2026-09-11 01:47 and 2026-09-12 06:13, seventeen
# consecutive runs started and died one second later on "You've hit your weekly
# limit". They were harmless - nothing was written, posted or published - but
# they looked like plain rc=1 failures, so ~30h of silence took a human noticing
# that reviews had gone quiet. Check the meter first and say so plainly instead.
if [ "$DRY" = 1 ]; then
    QMSG=""; QRC=0
    echo "  usage probe: skipped"
else
QMSG=$("$HOME/review/bin/claude-usage-probe.sh" start 2>&1)
QRC=$?
if [ "$QRC" -ne 2 ] && [ -n "$QMSG" ]; then
    echo "usage probe: $QMSG"           # non-fatal, but never silent
fi
if [ "$QRC" -eq 2 ]; then
    echo "SKIPPED: Claude quota exhausted -- ${QMSG:-weekly limit reached}"
    echo "finish=$(date -Is) status=quota-exhausted"
    exit 0
fi
fi

if [ "$DRY" = 1 ]; then
    echo "  would run: claude -p \"$PROMPT\" --model claude-opus-5 --effort high"
    echo "             --permission-mode auto --add-dir $REVIEW_ROOT"
    echo "DRY RUN: every pre-flight passed; nothing was started."
    exit 0
fi

# Publish immediately so the dashboard shows this run as soon as it starts.
# Without this a run is invisible until it ends or the periodic refresh fires,
# which is exactly the window in which someone asks "what is it doing?".
"$HOME/review/bin/publish-runs-page.sh" >/dev/null 2>&1 || true

START=$(date +%s)
# stream-json + the formatter gives one flushed line per event, so the log is
# monitorable while the run is in flight rather than only at the end.
# 9>&- closes the lock fd for claude and everything it spawns. Without it every
# descendant inherits fd 9, so a single orphaned child - and these runs do leave
# stray sleep timers and codex agents - keeps the lock held long after the run
# exits, making the *next* run skip for no reason. That silently cost the 07:47
# followup and the 18:13 all run on 2026-09-07.
# Pin the model explicitly rather than via the 'opus' alias, so a future alias
# change cannot silently move these runs onto a different model (and a different
# quota pool). High effort: these reviews are the whole point of the box.
stdbuf -oL -eL claude -p "$PROMPT" \
    --model claude-opus-5 \
    --effort high \
    --permission-mode auto \
    --add-dir "$REVIEW_ROOT" \
    --output-format stream-json \
    --verbose 9>&- \
  | stdbuf -oL python3 "$REVIEW_ROOT/bin/fmt-stream.py" 9>&-
RC=${PIPESTATUS[0]}
END=$(date +%s)

echo
echo "=============================================================="
echo "reviewprs mode=$MODE rc=$RC elapsed=$(( (END-START)/60 ))m finish=$(date -Is)"
echo "=============================================================="
exit $RC
