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
case "$MODE" in
    rsync|RsyncProject/rsync\#*) ROLE=rsync ;;
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
           echo "finish=$(date -Is) status=wrong-claude-account"
           exit 1 ;;
        1) unset "$var"; return 0 ;;                 # the tool's own default
    esac
    # Setting the variable to the tool's own directory is not a no-op: `claude
    # auth status` then reports loggedIn with no address, because that record
    # only exists in directories created by `claude auth login` under it. Leave
    # it unset in that case - but only when the path is genuinely that directory
    # and not a symlink that could be repointed underneath us.
    local own="$HOME/.$tool"
    if [ "$d" = "$(readlink -f "$own" 2>/dev/null)" ] && [ ! -L "$own" ]; then
        unset "$var"
    else
        export "$var=$d"
    fi
}
unset CLAUDE_CONFIG_DIR CODEX_HOME 2>/dev/null || true
select_account claude CLAUDE_CONFIG_DIR
select_account codex  CODEX_HOME
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"

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
    echo "  claude config:  ${CLAUDE_CONFIG_DIR:-$HOME/.claude} (role $ROLE)"
    echo "  codex home:     ${CODEX_HOME:-$HOME/.codex}"
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

cd "$REVIEW_ROOT/work" || { echo "FATAL: no $REVIEW_ROOT/work"; exit 1; }

clear_stale_oauth_lock "$CLAUDE_DIR"

# An environment token overrides the config directory entirely, so the account we
# selected would not be the account that pays. Refuse rather than guess.
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    echo "FATAL: CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY is set; it would"
    echo "       override the account directory chosen for role $ROLE"
    echo "finish=$(date -Is) status=wrong-claude-account"
    exit 1
fi

# Which account is this? Two sources: the directory's own record, and the CLI.
# Where both answer they must agree - matching one local record against another
# does not establish which subscription pays, and a disagreement is exactly the
# case worth stopping for.
read -r CLAUDE_LOGGED CLAUDE_CLI_ACCOUNT <<EOS
$(claude auth status --json 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("no -"); raise SystemExit
print(("yes" if d.get("loggedIn") else "no"), (d.get("email") or "-"))
' 2>/dev/null || echo "no -")
EOS
CLAUDE_REC_ACCOUNT=$(python3 - "$CLAUDE_DIR" <<'PYA' 2>/dev/null
import json, os, sys
try:
    acct = json.load(open(os.path.join(sys.argv[1], ".claude.json"))).get("oauthAccount") or {}
    print(acct.get("emailAddress") or "-")
except Exception:
    print("-")
PYA
)
if [ "$CLAUDE_LOGGED" != "yes" ]; then
    echo "FATAL: $CLAUDE_DIR is not signed in (role $ROLE)"
    echo "       review-auth.sh login claude <account>   says how"
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
if [ -f "$CLAUDE_DIR/ACCOUNT" ] && [ -n "$ACCOUNT" ]; then
    WANT=$(read_account_file "$CLAUDE_DIR/ACCOUNT") || {
        echo "FATAL: $CLAUDE_DIR/ACCOUNT is not a plain account record"
        echo "finish=$(date -Is) status=wrong-claude-account"
        exit 1; }
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
CODEX_ACCOUNT=$(python3 - "$CODEX_DIR/auth.json" <<'PYB' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit
tok = d.get("tokens") or {}
print(tok.get("account_id") or d.get("account_id")
      or ("api-key" if d.get("OPENAI_API_KEY") else ""))
PYB
)
if [ -z "$CODEX_ACCOUNT" ]; then
    echo "FATAL: $CODEX_DIR has no usable codex credentials (role $ROLE)"
    echo "       review-auth.sh login codex <account>   says how"
    echo "finish=$(date -Is) status=wrong-codex-account"
    exit 1
fi
if [ -f "$CODEX_DIR/ACCOUNT" ]; then
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
if ! python3 - "$SETTINGS" <<'PYCHK'
import json,sys
try:
    d=json.load(open(sys.argv[1]))
except Exception as e:
    print("FATAL: cannot read settings.json: %s" % e); sys.exit(1)
deny=d.get("permissions",{}).get("deny",[])
need=["Bash(git push)","Bash(git push:*)"]
missing=[r for r in need if r not in deny]
if missing:
    print("FATAL: settings.json is missing deny rules: %s" % missing); sys.exit(1)
if d.get("permissions",{}).get("defaultMode")!="auto":
    print("FATAL: permissions.defaultMode is not 'auto'"); sys.exit(1)
print("permission pre-flight OK: git push denied, defaultMode=auto")
PYCHK
then
    echo "ABORTING: permission pre-flight failed"
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
