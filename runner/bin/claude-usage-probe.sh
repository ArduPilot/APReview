#!/bin/bash
# Read Claude's real usage meter and append it to a history file.
#
# `claude -p /usage` works headlessly and reports the actual session and weekly
# percentages, so no calibration guesswork is needed. Called before and after
# each run (for a true per-run delta) and hourly for a background trace.
#
# Must never fail the run that invoked it.
set -u
. "$HOME/review/bin/review-env.sh" 2>/dev/null || exit 0
OUT="$REVIEW_LOGS/claude-usage.jsonl"
TAG="${1:-probe}"

# Which account to read. A run exports CLAUDE_CONFIG_DIR for the role it chose
# (or leaves it unset when the role is the tool's own directory) along with
# REVIEW_ROLE, so inside a run there is nothing to decide. The hourly trace has
# neither and must resolve the role itself - otherwise it keeps sampling
# ~/.claude after `review-auth.sh use claude default personal`, freezing one
# meter and never starting the other.
if [ -z "${CLAUDE_CONFIG_DIR:-}" ] && [ -z "${REVIEW_ROLE:-}" ]; then
    PROBE_DIR=$(review_auth claude default); PROBE_RC=$?
    if [ "$PROBE_RC" -eq 2 ]; then
        echo "usage probe: cannot use the claude account for role default" >&2
        exit 1
    fi
    PROBE_DIR=$(role_config_dir claude default "$PROBE_DIR")
    [ -z "$PROBE_DIR" ] || export CLAUDE_CONFIG_DIR="$PROBE_DIR"
fi

clear_stale_oauth_lock >/dev/null 2>&1 || true

# Which account this reading belongs to. Runs can use different subscriptions
# (rsync reviews have their own), and two accounts' percentages in one file
# would read as a meter that jumps about for no reason.
ACCOUNT=$(claude auth status --json 2>/dev/null | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    sys.exit(0)
print(d.get("email","") if d.get("loggedIn") else "")
' 2>/dev/null)

# /usage just prints the meter; no need to spend a big model on it.
# Never fail the run that invoked this, but never hide it either: a probe that
# comes back with nothing is usually the OAuth refresh, and the agent is about
# to meet the same thing. `|| exit 0` used to swallow it without a word.
if ! RAW=$(timeout 180 claude -p "/usage" --model claude-haiku-4-5-20251001 \
             --permission-mode auto 2>/dev/null); then
    echo "claude -p /usage exited non-zero - no reading taken" >&2
    exit 0
fi
if [ -z "$RAW" ]; then
    echo "no output from claude -p /usage (auth or network problem?)" >&2
    exit 0
fi

# "You've hit your weekly limit - resets ..." is what /usage prints once the
# allowance is gone. Surface it as exit status 2 so callers can pre-flight.
LIMITED=0
case "$RAW" in *"hit your weekly limit"*|*"hit your usage limit"*|*"hit your session limit"*) LIMITED=1;; esac

printf '%s' "$RAW" | ACCOUNT="$ACCOUNT" python3 -c '
import sys, re, json, datetime
raw = sys.stdin.read()
rec = {"at": datetime.datetime.now().astimezone().isoformat(), "tag": sys.argv[1]}
import os
if os.environ.get("ACCOUNT"):
    rec["account"] = os.environ["ACCOUNT"]
pats = {
    "session_pct":     r"Current session:\s*(\d+)%",
    "week_pct":        r"Current week \(all models\):\s*(\d+)%",
    "week_model_pct":  r"Current week \(([^)]+)\):\s*(\d+)%",
}
m = re.search(pats["session_pct"], raw)
if m: rec["session_pct"] = int(m.group(1))
m = re.search(pats["week_pct"], raw)
if m: rec["week_pct"] = int(m.group(1))
for name, pct in re.findall(r"Current week \(([^)]+)\):\s*(\d+)%", raw):
    if name != "all models":
        rec["week_%s_pct" % name.lower().replace(" ", "_")] = int(pct)
m = re.search(r"Current session:.*?resets ([^(\n]+)", raw)
if m: rec["session_resets"] = m.group(1).strip()
m = re.search(r"Current week \(all models\):.*?resets ([^(\n]+)", raw)
if m: rec["week_resets"] = m.group(1).strip()
m = re.search(r"Top skills: /reviewprs (\d+)%", raw)
if m: rec["reviewprs_share_pct"] = int(m.group(1))
m = re.search(r"(hit your (?:weekly|usage|session) limit[^\n]*)", raw)
if m:
    rec["limited"] = True
    rec["limit_msg"] = m.group(1).strip()
if rec.get("limited") or "week_pct" in rec or "session_pct" in rec:
    print(json.dumps(rec))
' "$TAG" >> "$OUT" 2>/dev/null

# It can also answer with prose and exit 0 - the refresh failure arrives that
# way - in which case nothing was parsed and nothing was recorded. Say so
# rather than leaving the run log with no trace of a probe at all.
if [ "$LIMITED" -eq 0 ] && ! tail -1 "$OUT" 2>/dev/null | grep -q '"tag": *"'"$TAG"'"'; then
    echo "no usage figures in the reply to /usage - $(printf %s "$RAW" \
        | tr '\n' ' ' | cut -c1-90)" >&2
fi

if [ "$LIMITED" -eq 1 ]; then
    printf '%s' "$RAW" | grep -oE "hit your (weekly|usage|session) limit[^\n]*" | head -1
    exit 2
fi
exit 0
