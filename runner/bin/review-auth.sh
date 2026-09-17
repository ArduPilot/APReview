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
# Source the sibling, not a deployed path: this has to work from any checkout,
# not only from ~/review/bin.
_self=$(readlink -f "${BASH_SOURCE[0]:-$0}")
. "$(dirname "$_self")/review-env.sh" 2>/dev/null || { echo "no review environment"; exit 1; }
unset _self

AUTH="$REVIEW_AUTH"
ROLES="default rsync"
TOOLS="claude codex"

account_of() {   # tool dir -> the account signed in there, or empty
    local tool="$1" dir="$2"
    case "$tool" in
        claude) python3 - "$dir" <<'PYC' 2>/dev/null
import json, os, subprocess, sys
d = sys.argv[1]
# Ask the CLI whether this directory authenticates anyone. A .claude.json left
# behind by a past login names an account the directory can no longer use, and
# reporting it as "signed in as" is the reassuring half of a broken setup.
env = dict(os.environ)
if os.path.realpath(d) == os.path.realpath(os.path.expanduser("~/.claude")):
    env.pop("CLAUDE_CONFIG_DIR", None)
else:
    env["CLAUDE_CONFIG_DIR"] = d
logged, email = False, ""
try:
    out = subprocess.run(["claude", "auth", "status", "--json"],
                         capture_output=True, text=True, env=env)
    got = json.loads(out.stdout)
    logged, email = bool(got.get("loggedIn")), got.get("email") or ""
except Exception:
    pass
if not logged:
    raise SystemExit
if not email:
    # signed in, but this directory only knows its own address when a login
    # created it under CLAUDE_CONFIG_DIR
    try:
        acct = json.load(open(os.path.join(d, ".claude.json"))).get("oauthAccount") or {}
        email = acct.get("emailAddress") or ""
    except Exception:
        pass
print(email or "signed in")
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
            dir=$(readlink -f "$link")
            want=$(read_account_file "$link/ACCOUNT" 2>/dev/null) || want=""
            got=$(account_of "$tool" "$dir")
            note=""
            # Credentials, not metadata: a directory can carry an address it was
            # once signed in as and hold no credentials at all.
            [ -n "$got" ] || note="  NOT SIGNED IN"
            if [ -z "$note" ] && [ -n "$want" ] && [ -n "$got" ]; then
                # An email and a uuid are both identities but not the same one;
                # comparing them reported MISMATCH on a correct setup.
                case "$want:$got" in
                    *@*:*@*|*-*-*:*-*-*) [ "$want" != "$got" ] && note="  MISMATCH: expected $want" ;;
                    *) note="  (recorded $want, reported differently)" ;;
                esac
            fi
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
    link="$AUTH/$tool-$role"
    [ -d "$dir" ] || { echo "no such account directory: $dir"; exit 1; }
    # `use claude default default` would point the role at itself: exit 0, and a
    # role nothing can resolve. The account must be a real account directory,
    # not another role link.
    if [ "$acct" = "$role" ] || [ -L "$dir" ] && [ "$(readlink -f "$dir")" = "$(readlink -f "$link")" ]; then
        echo "$tool-$acct is the role itself - that would leave $tool-$role unresolvable"
        exit 1
    fi
    case "$acct" in
        default|rsync) echo "$tool-$acct is a role name, not an account"; exit 1 ;;
    esac
    # `ln -sfn` into a path that is a real directory creates the link INSIDE it
    # and reports success, leaving the role pointing where it always did.
    if [ -e "$link" ] && [ ! -L "$link" ]; then
        echo "$link is a directory, not a role symlink - refusing to write inside it"
        exit 1
    fi
    got=$(account_of "$tool" "$dir")
    [ -n "$got" ] || echo "warning: $dir is not signed in - runs using it will refuse to start"
    # Atomic: a run starting mid-switch sees the old link or the new one, never
    # the gap that unlink-then-symlink leaves.
    tmp="$AUTH/.$tool-$role.$$"
    ln -sfn "$tool-$acct" "$tmp" || { echo "could not create the new link"; exit 1; }
    mv -T "$tmp" "$link" || { rm -f "$tmp"; echo "could not replace $link"; exit 1; }
    [ "$(readlink "$link")" = "$tool-$acct" ] || { echo "switch did not take effect"; exit 1; }
    echo "$tool-$role -> $tool-$acct${got:+  ($got)}"
    ;;
login)
    [ $# -eq 3 ] || { echo "usage: review-auth.sh login <claude|codex> <account>"; exit 2; }
    tool="$2"; acct="$3"; dir="$AUTH/$tool-$acct"
    # Credentials go in here: not readable by anyone else, and neither is the root.
    mkdir -p "$dir" && chmod 700 "$dir" "$AUTH"
    case "$tool" in
        claude) echo "Run this, then answer in a browser:"
                echo "  CLAUDE_CONFIG_DIR=$dir claude auth login"
                echo "and record the account so a wrong sign-in is caught:"
                echo "  echo <address> > $dir/ACCOUNT" ;;
        codex)  echo "Run this, then answer in a browser:"
                echo "  CODEX_HOME=$dir codex login"
                echo "then record the account id it reports - not an address, which"
                echo "is what the runner compares against:"
                echo "  review-auth.sh status            # shows the id"
                echo "  echo <account-id> > $dir/ACCOUNT" ;;
    esac
    ;;
*)  sed -n '3,12p' "$0" | sed 's/^# \?//'; exit 2 ;;
esac
