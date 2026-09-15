# Common environment for all review work on a review runner.
# Source this from cron wrappers and interactive shells alike.
export REVIEW_ROOT="$HOME/review"
export REVIEW_DATA="$REVIEW_ROOT/data"
export REVIEW_LOGS="$REVIEW_ROOT/logs"
export REVIEW_REPOS="$REVIEW_ROOT/repositories"

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
# A mode can run as its own Claude account, so that work does not eat the main
# account's quota. CLAUDE_CONFIG_DIR relocates the whole config dir, credentials
# included; the shared parts (commands, skills, CLAUDE.md, plugins) are symlinks
# back into ~/.claude so the command can never drift between accounts. The
# address is set in etc/local.conf; an env value wins, for testing.
export REVIEW_RSYNC_CLAUDE_DIR="$REVIEW_ROOT/etc/claude-rsync"
export REVIEW_RSYNC_CLAUDE_ACCOUNT="${REVIEW_RSYNC_CLAUDE_ACCOUNT:-}"

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
[ -f "$REVIEW_ROOT/etc/local.conf" ] && . "$REVIEW_ROOT/etc/local.conf"

# Globally-installed npm modules (jsdom, used by the wiki JS test harnesses) are
# not found by a bare require() from an arbitrary cwd without this.
export NODE_PATH="$(npm root -g 2>/dev/null)"
