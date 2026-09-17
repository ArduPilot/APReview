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
own = os.path.expanduser("~/.claude")
# The same rule the runner uses. Unsetting whenever the canonical paths match
# diagnosed a working account as broken: with ~/.claude a symlink to an account
# directory, the unset form reports whatever the home record says.
if os.path.realpath(d) == os.path.realpath(own) and not os.path.islink(own):
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
# The same three questions the runner asks: a subscription login, the first-party
# provider, and the directory we selected - not one an inherited variable chose.
keys = ("authMethod", "apiProvider", "configDirectory")
if sum(1 for k in keys if got.get(k)) not in (0, 3):
    print("partial-answer"); raise SystemExit
if (got.get("authMethod") or "claude.ai") != "claude.ai" or \
        (got.get("apiProvider") or "firstParty") != "firstParty":
    print("not-a-subscription"); raise SystemExit
cd = got.get("configDirectory")
if cd and os.path.realpath(cd) != os.path.realpath(d):
    print("wrong-directory"); raise SystemExit
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
        codex)  # config.toml can send the request to another provider entirely,
                # whatever account auth.json names
                other=$(python3 - "$dir" <<'PYP' 2>/dev/null
import os, re, sys
# config.toml decides where the request goes and which credential pays for it,
# so enumerating the settings that redirect it is a losing game - chatgpt_base_url
# sent the account's own OAuth token to another host with model_provider still
# "openai". Refuse anything that names an endpoint or a key, wherever it appears.
path = os.path.join(sys.argv[1], "config.toml")
if not os.path.exists(path):
    raise SystemExit                       # no config is not a redirected one
try:
    import tomllib
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
except Exception:
    # a file we cannot read is not a file we can vouch for
    print("unreadable-config")
    raise SystemExit
OK_HOSTS = ("api.openai.com", "chatgpt.com", "auth.openai.com")
bad = []
def walk(node, where):
    if isinstance(node, dict):
        for k, v in node.items():
            key = k.lower()
            if key in ("env_key", "api_key", "env_http_headers", "http_headers"):
                bad.append("%s%s" % (where, k))
            elif key == "requires_openai_auth" and v is False:
                bad.append("%s%s" % (where, k))
            elif key == "model_provider" and v != "openai":
                bad.append("%s%s" % (where, k))
            else:
                walk(v, "%s%s." % (where, k))
    elif isinstance(node, list):
        for v in node:
            walk(v, where)
    elif isinstance(node, str):
        m = re.match(r"https?://([^/:]+)", node.strip())
        if m and m.group(1) not in OK_HOSTS:
            bad.append(where.rstrip(".") or "url")
walk(cfg, "")
if bad:
    print("other-provider: " + ", ".join(sorted(set(bad))[:4]))
PYP
)
                [ -z "$other" ] || { echo "$other"; return 0; }
                python3 - "$dir/auth.json" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit
tok = d.get("tokens") or {}
# The runner refuses an API key: it bills whoever owns the key rather than the
# subscription the role names, and it wins over any leftover account id here.
if (d.get("auth_mode") or "").lower() in ("apikey", "api_key") or (
        d.get("OPENAI_API_KEY") and not d.get("auth_mode")):
    print("api-key")
else:
    print(tok.get("account_id") or d.get("account_id")
          or ("signed in" if tok else ""))
PY
                ;;
    esac
}

# The runner clears these before it reads any identity; reading them here with a
# different environment would report an account no run will use.
if ! clear_inherited_credentials; then
    echo "these could not be removed from the environment and would decide the"
    echo "account instead of the role - runs will refuse:$CLEARED_FAILED"
fi

case "${1:-status}" in
status)
    printf '%-16s %-26s %s\n' ROLE ACCOUNT-DIR "SIGNED IN AS"
    for tool in $TOOLS; do
        for role in $ROLES; do
            note=""; fallback=""
            # Ask the resolver rather than reimplementing it: an auth root the
            # runner will not touch, a target outside it, a dangling link - all
            # of those used to read here as a healthy role.
            # stdout is the path, stderr the diagnosis: merging them made a
            # permitted warning part of the directory name, and the row then
            # read NOT SIGNED IN for an account the runner accepts.
            why=$(review_auth "$tool" "$role" 2>&1 >/dev/null)
            dir=$(review_auth "$tool" "$role" 2>/dev/null); rc=$?
            if [ "$rc" -eq 2 ]; then
                why=$(printf '%s' "$why" | sed 's/^review_auth: //' | tr '\n' ';' \
                      | sed 's/;$//; s/;/; /g')
                printf '%-16s %-26s %s\n' "$tool-$role" "-" "$why  RUNS WILL REFUSE"
                continue
            fi
            if [ "$rc" -eq 1 ]; then
                # No link for the default role: the runner falls back to the
                # tool's own directory and runs, so report that account, not
                # "not set" - but judge it by the same rules as any other.
                dir=$(readlink -f "$HOME/.$tool" 2>/dev/null) || dir=""
                if [ -z "$dir" ] || [ ! -d "$dir" ]; then
                    printf '%-16s %-26s %s\n' "$tool-$role" "-" "(not set)"
                    continue
                fi
                target="~/.$tool"
                fallback="  (no link - the tool's own default)"
            else
                target=$(basename "$dir")
            fi
            want=""
            if [ -e "$dir/ACCOUNT" ] || [ -L "$dir/ACCOUNT" ]; then
                # A record the runner cannot read stops a run, so it cannot be
                # quietly treated here as no record at all.
                want=$(read_account_file "$dir/ACCOUNT" 2>/dev/null) \
                    || note="  BAD ACCOUNT RECORD - runs will refuse"
            fi
            got=$(account_of "$tool" "$dir")
            # Credentials, not metadata: a directory can carry an address it was
            # once signed in as and hold no credentials at all.
            if [ -z "$got" ]; then
                note="  NOT SIGNED IN"
            elif [ "$got" = api-key ]; then
                note="  API KEY, not a subscription - runs will refuse"
            elif [ -z "${got##other-provider*}" ] || [ "$got" = unreadable-config ]; then
                note="  config.toml sends this elsewhere - runs will refuse"
                got=${got%%:*}
            elif [ "$got" = partial-answer ]; then
                note="  the CLI answered only in part - runs will refuse"
            elif [ "$got" = not-a-subscription ]; then
                note="  a token or cloud provider, not the subscription - runs will refuse"
            elif [ "$got" = wrong-directory ]; then
                note="  the CLI read another directory - runs will refuse"
            elif [ "$tool" = claude ] && [ -z "$note" ]; then
                # The runner compares the CLI's answer with the directory's own
                # and refuses when they disagree; make the same comparison.
                rec=$(python3 - "$dir" <<'PYD' 2>/dev/null
import json, os, sys
try:
    a = json.load(open(os.path.join(sys.argv[1], ".claude.json"))).get("oauthAccount") or {}
except Exception:
    a = {}
print(a.get("emailAddress") or "")
PYD
)
                [ -n "$rec" ] && [ "$rec" != "$got" ] \
                    && note="  CONFLICT: directory records $rec - runs will refuse"
            fi
            if [ -z "$note" ]; then
                if [ -n "$want" ] && [ -n "$got" ] && [ "$got" != api-key ]; then
                    # An email and a uuid are both identities but not the same
                    # one; comparing them reported MISMATCH on a correct setup.
                    case "$want:$got" in
                        *@*:*@*|*-*-*:*-*-*)
                            [ "$want" != "$got" ] \
                                && note="  MISMATCH: expected $want - runs will refuse" ;;
                        *:"signed in")
                            # the runner refuses this: a record it cannot check
                            # is not a record that holds
                            note="  IDENTITY UNKNOWN, cannot check $want - runs will refuse" ;;
                        *) note="  (recorded $want, reported differently)" ;;
                    esac
                fi
            fi
            printf '%-16s %-26s %s%s%s\n' "$tool-$role" "$target" "${got:-none}" \
                "$note" "$fallback"
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
    # Two switches of the same role can interleave: the loser's revert would
    # otherwise undo - or delete - a switch the operator was told had succeeded.
    lockf="$AUTH/.$tool-$role.lock"
    # Opening it is a write: through a symlink an unsafe root could aim that at
    # any file we can write. Check the root before touching it, and never follow.
    auth_root_ok || { echo "refusing to switch"; exit 1; }
    [ ! -L "$lockf" ] || { echo "$lockf is a symlink - refusing to switch"; exit 1; }
    exec 9>>"$lockf" || { echo "could not lock $tool-$role"; exit 1; }
    flock 9 || { echo "could not lock $tool-$role"; exit 1; }
    was=$(readlink "$link" 2>/dev/null || true)
    tmp="$AUTH/.$tool-$role.$$"; tmp2="$AUTH/.$tool-$role.revert.$$"
    ln -sfn -- "$tool-$acct" "$tmp" || { echo "could not create the new link"; exit 1; }
    mv -T "$tmp" "$link" || { rm -f "$tmp"; echo "could not replace $link"; exit 1; }
    [ "$(readlink "$link")" = "$tool-$acct" ] || { echo "switch did not take effect"; exit 1; }
    # Refusing role names and self-links is not enough: an account that resolves
    # through the role link becomes a cycle only once the switch is made. Ask the
    # resolver, and put the old target back if the answer is no.
    if ! review_auth "$tool" "$role" >/dev/null 2>&1; then
        # "reverted" has to mean it: a target beginning with - is an option to
        # ln without --, and either step can fail.
        back=0
        if [ -n "$was" ]; then
            ln -sfn -- "$was" "$tmp2" 2>/dev/null && mv -T "$tmp2" "$link" 2>/dev/null \
                && [ "$(readlink "$link")" = "$was" ] && back=1
            rm -f "$tmp2"
        else
            rm -f "$link" && [ ! -e "$link" ] && [ ! -L "$link" ] && back=1
        fi
        echo "$tool-$acct does not resolve as $tool-$role"
        if [ "$back" = 1 ]; then
            echo "reverted to ${was:-no link}"
            exit 1
        fi
        echo "COULD NOT REVERT: $tool-$role still points at $tool-$acct"
        echo "                  fix it with: review-auth.sh use $tool $role <account>"
        exit 1
    fi
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
                echo "  review-auth.sh list              # shows the id for every account"
                echo "  echo <account-id> > $dir/ACCOUNT" ;;
    esac
    ;;
*)  sed -n '3,12p' "$0" | sed 's/^# \?//'; exit 2 ;;
esac
