#!/bin/bash
#
# Which account each task runs as.
#
#   review-auth.sh status                    what every role resolves to
#   review-auth.sh list                      the account directories available
#   review-auth.sh use claude default personal    point a role at an account
#   review-auth.sh login claude personal     how to sign that account in
#
# Accounts live in $REVIEW_ROOT/auth, one directory each, never in git:
#
#   auth/claude-ardupilot/   a CLAUDE_CONFIG_DIR - credentials, settings, projects
#   auth/claude-personal/
#   auth/codex-ardupilot/    a CODEX_HOME - auth.json and friends
#   auth/codex-personal/
#
# and a symlink per role says which account that role uses:
#
#   auth/claude-default -> claude-ardupilot    every ArduPilot task
#   auth/claude-rsync   -> claude-personal     the non-ArduPilot target
#   auth/codex-default  -> codex-personal
#   auth/codex-rsync    -> codex-personal
#
# Switching account is repointing a symlink, which is the point: when the
# ArduPilot subscription runs out of weekly quota, `use claude default personal`
# moves the whole workload across without touching a script or a config file.
#
# A directory may record the account it is supposed to hold, in a file named
# ACCOUNT. run-reviewprs.sh checks it, so a directory that gets signed in as the
# wrong account is caught before a run spends the wrong subscription.
set -u
. "$HOME/review/bin/review-env.sh" 2>/dev/null || { echo "no review environment"; exit 1; }

AUTH="$REVIEW_AUTH"
ROLES="default rsync"
TOOLS="claude codex"

account_of() {   # tool dir -> the account signed in there, or empty
    local tool="$1" dir="$2"
    case "$tool" in
        claude) python3 - "$dir" <<'PYC' 2>/dev/null
import json, os, subprocess, sys
d = sys.argv[1]
# <dir>/.claude.json carries the address for a directory created by
# `claude auth login` under CLAUDE_CONFIG_DIR. The tool's own default dir keeps
# it elsewhere, so fall back to asking with the variable unset.
try:
    acct = json.load(open(os.path.join(d, ".claude.json"))).get("oauthAccount") or {}
    if acct.get("emailAddress"):
        print(acct["emailAddress"]); raise SystemExit
except SystemExit:
    raise
except Exception:
    pass
env = dict(os.environ)
if os.path.realpath(d) == os.path.realpath(os.path.expanduser("~/.claude")):
    env.pop("CLAUDE_CONFIG_DIR", None)
else:
    env["CLAUDE_CONFIG_DIR"] = d
try:
    out = subprocess.run(["claude", "auth", "status", "--json"],
                         capture_output=True, text=True, env=env)
    got = json.loads(out.stdout)
    print(got.get("email") or ("signed in" if got.get("loggedIn") else ""))
except Exception:
    pass
PYC
                ;;
        codex)  python3 - "$dir/auth.json" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit
tok = d.get("tokens") or {}
print(tok.get("account_id") or d.get("account_id")
      or ("signed in" if d.get("OPENAI_API_KEY") or tok else ""))
PY
                ;;
    esac
}

case "${1:-status}" in
status)
    printf '%-16s %-26s %s\n' ROLE ACCOUNT-DIR "SIGNED IN AS"
    for tool in $TOOLS; do
        for role in $ROLES; do
            link="$AUTH/$tool-$role"
            [ -e "$link" ] || { printf '%-16s %-26s %s\n' "$tool-$role" "-" "(not set)"; continue; }
            target=$(basename "$(readlink -f "$link")")
            want=""; [ -f "$link/ACCOUNT" ] && want=$(cat "$link/ACCOUNT")
            got=$(account_of "$tool" "$(readlink -f "$link")")
            note=""
            [ -n "$want" ] && [ -n "$got" ] && [ "$want" != "$got" ] && note="  MISMATCH: expected $want"
            [ -z "$got" ] && note="  NOT SIGNED IN"
            printf '%-16s %-26s %s%s\n' "$tool-$role" "$target" "${got:-none}" "$note"
        done
    done
    ;;
list)
    for d in "$AUTH"/*/; do
        [ -d "$d" ] || continue
        b=$(basename "$d")
        case "$b" in *-default|*-rsync) continue;; esac     # those are the role links
        tool=${b%%-*}
        printf '%-26s %s\n' "$b" "$(account_of "$tool" "$d")"
    done
    ;;
use)
    [ $# -eq 4 ] || { echo "usage: review-auth.sh use <claude|codex> <role> <account>"; exit 2; }
    tool="$2"; role="$3"; acct="$4"
    dir="$AUTH/$tool-$acct"
    [ -d "$dir" ] || { echo "no such account directory: $dir"; exit 1; }
    got=$(account_of "$tool" "$dir")
    [ -n "$got" ] || echo "warning: $dir is not signed in - runs using it will refuse to start"
    ln -sfn "$tool-$acct" "$AUTH/$tool-$role"
    echo "$tool-$role -> $tool-$acct${got:+  ($got)}"
    ;;
login)
    [ $# -eq 3 ] || { echo "usage: review-auth.sh login <claude|codex> <account>"; exit 2; }
    tool="$2"; acct="$3"; dir="$AUTH/$tool-$acct"
    mkdir -p "$dir"
    case "$tool" in
        claude) echo "Run this, then answer in a browser:"
                echo "  CLAUDE_CONFIG_DIR=$dir claude auth login"
                echo "and record the account so a wrong sign-in is caught:"
                echo "  echo <address> > $dir/ACCOUNT" ;;
        codex)  echo "Run this, then answer in a browser:"
                echo "  CODEX_HOME=$dir codex login"
                echo "and record the account:"
                echo "  echo <address> > $dir/ACCOUNT" ;;
    esac
    ;;
*)  sed -n '3,12p' "$0" | sed 's/^# \?//'; exit 2 ;;
esac
