# Common environment for all review work on a review runner.
# Source this from cron wrappers and interactive shells alike.
export REVIEW_ROOT="$HOME/review"
export REVIEW_DATA="$REVIEW_ROOT/data"
export REVIEW_LOGS="$REVIEW_ROOT/logs"
export REVIEW_REPOS="$REVIEW_ROOT/repositories"

# --- accounts ------------------------------------------------------------------
# One directory per account under auth/, and a symlink per role saying which
# account that role uses - see review-auth.sh. Never in git: these hold
# credentials. A task resolves its own directories rather than inheriting
# whatever the box happens to be signed in as.
export REVIEW_AUTH="$REVIEW_ROOT/auth"

# read_account_file <path> - the address or id a directory records, validated.
#
# Never prints the file's contents unchecked: an ACCOUNT symlinked at a
# credentials file would otherwise have its token echoed into a run log. One
# short line, no whitespace, and a plausible address or id, or nothing.
read_account_file() {
    local f="$1" v
    [ -f "$f" ] && [ ! -L "$f" ] || return 1
    [ "$(wc -c < "$f")" -le 200 ] || return 1
    v=$(head -1 "$f" | tr -d '\r')
    # An address or a uuid, nothing else. A token in this file is a mistake, and
    # echoing it into a run log would turn that mistake into a disclosure.
    case "$v" in
        *@*.*) case "$v" in *[!A-Za-z0-9@._+-]*) return 1 ;; esac ;;
        [0-9a-fA-F]*-[0-9a-fA-F]*-*)
            case "$v" in *[!0-9a-fA-F-]*) return 1 ;; esac ;;
        *) return 1 ;;
    esac
    printf '%s\n' "$v"
}

# review_auth <claude|codex> <role> - the account directory for that role.
#
#   0  printed a directory
#   1  no link for this role and none for default: the caller leaves the tool's
#      own default in place
#   2  the role is configured but unusable - a dangling link, a target that is
#      not a directory, or one outside the auth root
#
# A role other than `default` never falls back. Falling back is how "the rsync
# target never spends the project's subscription" would quietly stop being true:
# the guarantee used to be a hardcoded address, and a symlink that is missing or
# broken must fail loudly rather than silently becoming default.
review_auth() {
    local tool="$1" role="${2:-default}" link target root
    root=$(readlink -f "$REVIEW_AUTH" 2>/dev/null) || root="$REVIEW_AUTH"
    link="$REVIEW_AUTH/$tool-$role"
    if [ ! -e "$link" ] && [ -L "$link" ]; then
        echo "review_auth: $tool-$role is a dangling symlink" >&2
        return 2
    fi
    if [ ! -e "$link" ]; then
        [ "$role" = "default" ] || {
            echo "review_auth: no account configured for $tool-$role" >&2
            return 2
        }
        return 1
    fi
    target=$(readlink -f "$link") || return 2
    [ -d "$target" ] || {
        echo "review_auth: $tool-$role does not resolve to a directory" >&2
        return 2
    }
    # Containment: an account directory lives under the auth root, or is the
    # tool's own default directory, which auth/<tool>-<name> may symlink to.
    case "$target/" in
        "$root"/*) ;;
        "$(readlink -f "$HOME/.$tool" 2>/dev/null)"/) ;;
        *) echo "review_auth: $tool-$role resolves outside $REVIEW_AUTH" >&2
           return 2 ;;
    esac
    # Credentials must not be reachable by other users. Enforced for the
    # directories we create under auth/; the tool's own ~/.claude or ~/.codex is
    # made by the tool - often group- and world-readable - and is not ours to
    # refuse, so that is reported and allowed. Group access is ignored either
    # way: this box uses private per-user groups, so enforcing it would fail on
    # every directory made with the default umask while protecting nobody.
    if [ -n "$(find "$target" -maxdepth 0 -perm /o+rwx 2>/dev/null)" ]; then
        case "$target/" in
            "$root"/*) echo "review_auth: $target is accessible by other users" >&2
                       return 2 ;;
            *) echo "review_auth: note - $target is accessible by other users" >&2 ;;
        esac
    fi
    printf '%s\n' "$target"
}

# Isolated git config: no https->ssh rewrite, so HTTPS clones work without a key.
export GIT_CONFIG_GLOBAL="$REVIEW_ROOT/etc/gitconfig"

# Never let anything default into /tmp - it is a 16G tmpfs on this box and
# filling it takes the machine down.
export TMPDIR="$REVIEW_DATA/tmp"
mkdir -p "$TMPDIR" "$REVIEW_DATA"

export CCACHE_DIR="$REVIEW_ROOT/ccache"
# ccache first, then the ARM toolchain and ArduPilot's autotest dir. cron does
# not source ~/.profile, where install-prereqs-ubuntu.sh put these, so they are
# repeated here or every ChibiOS build fails with "arm-none-eabi-gcc not found".
export PATH="/usr/lib/ccache:$PATH"
export PATH="/opt/gcc-arm-none-eabi-10-2020-q4-major/bin:$PATH"
export PATH="$REVIEW_REPOS/ardupilot/Tools/autotest:$PATH"
export PATH="$REVIEW_ROOT/bin:$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"

# --- per-mode Claude account ------------------------------------------------
# rsync reviews are tridge's own project, so they run on a separate Claude
# subscription and leave the main account's quota for ArduPilot work.
# CLAUDE_CONFIG_DIR relocates the whole config dir, credentials included; the
# shared parts (commands, skills, CLAUDE.md, plugins) are symlinks back into
# ~/.claude so the skill can never drift between the two accounts.

# A Claude Code OAuth refresh that dies mid-flight leaves .oauth_refresh.lock
# behind in the config dir, and every later invocation then refuses to refresh
# with "another Claude Code process is refreshing it or exited mid-refresh".
# Nothing clears it on its own: the 05:25 rsync run on 2026-09-15 failed that
# way, 15h after its token expired, and would have failed every day since.
# Clear it only when no live claude process is actually using that dir.
clear_stale_oauth_lock() {
    local dir="${1:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}}"
    local lock="$dir/.oauth_refresh.lock" p env inuse=0
    [ -e "$lock" ] || return 0
    for p in $(pgrep -x claude 2>/dev/null); do
        env=$(tr "\0" "\n" < "/proc/$p/environ" 2>/dev/null) || continue
        if printf %s "$env" | grep -qx "CLAUDE_CONFIG_DIR=$dir"; then
            inuse=1
        elif [ "$dir" = "$HOME/.claude" ] && \
             ! printf %s "$env" | grep -q "^CLAUDE_CONFIG_DIR="; then
            inuse=1                      # no override means the default dir
        fi
    done
    if [ "$inuse" -eq 0 ]; then
        rm -rf "$lock" && echo "cleared a stale OAuth refresh lock in $dir"
    else
        echo "NOTE: OAuth refresh lock held in $dir by a running claude"
    fi
}

# Which GitHub accounts' comments count as ours: the account posting now, plus
# any it replaced. When commenting moves from one account to another, every
# comment posted before the switch would otherwise stop being recognised as ours,
# and the next run would post a duplicate instead of updating what is already
# there. Set REVIEW_COMMENT_ACCOUNTS in local.conf as a space-separated list,
# newest first. Prints a JSON array for jq: ["AP-Review","tridge"]
review_comment_accounts() {
    local list="${REVIEW_COMMENT_ACCOUNTS:-}" a out=
    [ -n "$list" ] || list=$(gh api user --jq .login 2>/dev/null)
    for a in $list; do out="$out\"$a\","; done
    printf '[%s]' "${out%,}"
}

# --- publishing ---------------------------------------------------------------
# Where finished reports are rsynced, and the public URL they end up at. Both are
# site-specific: set them in etc/local.conf (see local.conf.example), which is not
# in git. REVIEW_PUBLISH is an rsync destination - either an rsync-daemon URL
# (rsync://user@host) with RSYNC_AUTH pointing at a password file, or an ssh
# destination (host:path) with RSYNC_AUTH empty.
export REVIEW_PUBLISH="${REVIEW_PUBLISH:-}"
export RSYNC_AUTH="${RSYNC_AUTH:-}"
export REVIEW_PUBLIC_URL="${REVIEW_PUBLIC_URL:-}"

# Shown on the runs dashboard, so several runners can publish side by side.
export REVIEW_BOX_NAME="${REVIEW_BOX_NAME:-$(hostname -s 2>/dev/null || echo runner)}"

# Site configuration: publishing target, and the Claude account a given mode must
# run as. Kept out of git because it names hosts, paths and an account.
# --- publishing ---------------------------------------------------------------
# Where finished reports are rsynced, and the public URL they end up at. Both are
# site-specific: set them in etc/local.conf (see local.conf.example), which is not
# in git. REVIEW_PUBLISH is an rsync destination - either an rsync-daemon URL
# (rsync://user@host) with RSYNC_AUTH pointing at a password file, or an ssh
# destination (host:path) with RSYNC_AUTH empty.
export REVIEW_PUBLISH="${REVIEW_PUBLISH:-}"
export RSYNC_AUTH="${RSYNC_AUTH:-}"
export REVIEW_PUBLIC_URL="${REVIEW_PUBLIC_URL:-}"

# Shown on the runs dashboard, so several runners can publish side by side.
export REVIEW_BOX_NAME="${REVIEW_BOX_NAME:-$(hostname -s 2>/dev/null || echo runner)}"

# --- site configuration --------------------------------------------------------
# local.conf holds the publishing target, the runner's name and the Claude
# account a given mode runs as. It sets plain shell variables, and a variable
# that is set but not exported is invisible to every child process - gh, python,
# the review tools. That has bitten twice: GH_TOKEN, so runs kept posting as the
# keyring account, and REVIEW_COMMENT_ACCOUNTS, so post-comments.py saw only the
# current account and would have posted a duplicate on every PR the old one had
# reviewed. `set -a` marks everything the file assigns for export and lets the
# shell do the parsing: a regex over the file misses `export FOO=`,
# `FIRST=one FOO=...`, an assignment inside an if, and anything else a shell
# would accept. Sourced exactly once - doing it twice re-runs any assignment
# that appends or counts.
if [ -f "$REVIEW_ROOT/etc/local.conf" ]; then
    set -a
    . "$REVIEW_ROOT/etc/local.conf"
    set +a
fi

# Globally-installed npm modules (jsdom, used by the wiki JS test harnesses) are
# not found by a bare require() from an arbitrary cwd without this.
export NODE_PATH="$(npm root -g 2>/dev/null)"
