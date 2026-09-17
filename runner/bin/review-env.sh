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
# An inherited credential or provider selector decides the account whatever the
# role says. Naming them one at a time missed CLAUDE_SECURESTORAGE_CONFIG_DIR,
# which points the CLI at another credential store while the selected directory
# goes on reporting its own address, and CLAUDE_CODE_USE_BEDROCK, which bills a
# cloud account instead of any subscription. Match the shape, with the keyword
# anywhere in the name - CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR is one of these.
# Clearing rather than refusing: a run started from a terminal inside Claude Code
# carries CLAUDE_CODE_* variables that are not credentials.
# Sets CLEARED_VARS to the names it cleared, never the values. Not a function
# that prints them: $(...) is a subshell, so the unsets would not reach the
# caller at all.
clear_inherited_credentials() {
    local v
    CLEARED_VARS=$(env | sed -n 's/^\(\(ANTHROPIC\|OPENAI\)_[A-Z0-9_]*\|\(CLAUDE\|CODEX\)_[A-Z0-9_]*\(TOKEN\|KEY\|AUTH\|SECRET\|CREDENTIAL\|CONFIG_DIR\|STORAGE\|BASE_URL\|HOME\|USE_[A-Z0-9_]\{1,\}\)[A-Z0-9_]*\)=.*/\1/p' \
            | sort -u | tr '\n' ' ')
    CLEARED_FAILED=""
    for v in $CLEARED_VARS; do
        unset "$v" 2>/dev/null || true
        # readonly survives unset, and bash reports the failure only on stderr:
        # "cleared" would otherwise be printed for a variable still in force
        [ -z "${!v+x}" ] || CLEARED_FAILED="$CLEARED_FAILED $v"
    done
    [ -z "$CLEARED_FAILED" ]
}

# An account id alone is metadata, not a usable subscription credential.
read_codex_account() {
    python3 - "$1/auth.json" <<'PYAUTH' 2>/dev/null
import base64, json, re, sys
try:
    d = json.load(open(sys.argv[1]))
    if not isinstance(d, dict):
        raise ValueError("not an object")
    mode = d.get("auth_mode")
    if (mode or "").lower() in ("apikey", "api_key") or (
            d.get("OPENAI_API_KEY") and not mode):
        print("api-key")
        raise SystemExit
    if mode not in (None, "chatgpt"):
        print("unsupported-auth-mode")
        raise SystemExit
    tok = d.get("tokens") or {}
    if not all(isinstance(tok.get(k), str) and tok[k]
               for k in ("access_token", "refresh_token", "id_token")):
        raise ValueError("missing tokens")
    # Codex deserializes this JWT when loading auth.json, even before a request.
    parts = tok["id_token"].split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError("invalid ID token")
    payload = base64.b64decode(parts[1] + "=" * (-len(parts[1]) % 4),
                               altchars=b"-_", validate=True)
    if not isinstance(json.loads(payload), dict):
        raise ValueError("invalid ID token payload")
    account = tok.get("account_id")
    if not isinstance(account, str) or not re.fullmatch(
            r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", account):
        raise ValueError("missing account id")
    print(account)
except Exception:
    pass
PYAUTH
}

# One check for status and the runner, so their routing decisions cannot drift.
check_codex_config() {
    python3 - "$1" <<'PYCONFIG' 2>/dev/null || echo unreadable-config
import glob, os, re, sys
from urllib.parse import urlsplit
# config.toml is one layer of several. codex also merges /etc/codex/*.toml and,
# with --profile, <name>.config.toml beside it; a redirect in any of them is the
# same redirect. The project-local layer is not here - a run works in checkouts
# of other people's pull requests, and that layer needs its own answer.
paths = [os.path.join(sys.argv[1], "config.toml")]
paths += sorted(glob.glob(os.path.join(glob.escape(sys.argv[1]), "*.config.toml")))
paths += ["/etc/codex/config.toml", "/etc/codex/managed_config.toml",
          "/etc/codex/requirements.toml"]
layers = []
for path in paths:
    if not os.path.lexists(path):
        continue                           # absence differs from a broken link
    try:
        import tomllib
        with open(path, "rb") as f:
            layers.append((path, tomllib.load(f)))
    except Exception:
        print("unreadable-config")
        raise SystemExit
if not layers:
    raise SystemExit
OK_HOSTS = ("api.openai.com", "chatgpt.com", "auth.openai.com")
bad = []
def safe_url(value):
    try:
        u = urlsplit(value)
        return (isinstance(value, str) and not re.search(r"[\s\\]", value)
                and u.scheme == "https" and u.hostname in OK_HOSTS
                and u.username is None and u.password is None
                and u.port in (None, 443) and not u.query and not u.fragment)
    except (ValueError, TypeError, AttributeError):
        return False

def walk(node, where="", provider=False):
    if not isinstance(node, dict):
        bad.append(where.rstrip(".") or "config")
        return
    for k, v in node.items():
        key = k.lower()
        setting = where + k
        if key in ("env_key", "api_key", "env_http_headers", "http_headers",
                   "experimental_bearer_token", "auth", "aws", "query_params"):
            bad.append(setting)
        elif key == "requires_openai_auth" and v is not True:
            bad.append(setting)
        elif key == "model_provider" and v != "openai":
            bad.append(setting)
        elif key == "cli_auth_credentials_store" and v != "file":
            bad.append(setting)
        elif key == "forced_login_method" and v != "chatgpt":
            bad.append(setting)
        elif key == "mcp_oauth_callback_url" and not provider:
            continue
        elif key.endswith(("_url", "_endpoint")) or key in ("url", "endpoint"):
            if not safe_url(v):
                bad.append(setting)
        elif key in ("profiles", "model_providers"):
            if not isinstance(v, dict):
                bad.append(setting)
                continue
            for name, entry in v.items():
                walk(entry, setting + "." + name + ".", key == "model_providers")
        elif provider and isinstance(v, (dict, list)):
            # A new provider auth mechanism must not silently evade this check.
            bad.append(setting)
        elif key == "mcp_servers":
            # An MCP server's own endpoint and headers authenticate that server,
            # not inference, so a third-party host there is the point of it.
            continue
        elif isinstance(v, dict):
            # Every other table too: [otel] carries an exporter endpoint and its
            # headers, and inspecting only the tables that route inference left
            # that reachable.
            walk(v, setting + ".", provider)
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    walk(item, "%s[%d]." % (setting, i), provider)
        # Instructions, notifications and project trust entries are not routing.
for path, cfg in layers:
    # name the layer when it is not the account's own config.toml
    walk(cfg, "" if path == paths[0] else os.path.basename(path) + ":")
if bad:
    # TOML keys can contain arbitrary text; keep diagnostics on one safe line.
    print("other-provider: " + ", ".join(ascii(k)[1:-1] for k in sorted(set(bad))[:4]))
PYCONFIG
}

# The root holds the role links. Private leaves protect nothing if anyone can
# repoint the symlink that chooses between them. Separate from review_auth so
# that writing into the root can check it without resolving a role - a role that
# is broken is the usual reason to write there.
auth_root_ok() {
    local root
    root=$(readlink -f "$REVIEW_AUTH" 2>/dev/null) || root="$REVIEW_AUTH"
    if [ -d "$root" ] && [ -n "$(find "$root" -maxdepth 0 \
                                      \( -perm /o+w -o ! -user "$(id -u)" \) 2>/dev/null)" ]; then
        echo "$root is writable by other users or not yours" >&2
        return 1
    fi
    return 0
}

review_auth() {
    local tool="$1" role="${2:-default}" link target root
    root=$(readlink -f "$REVIEW_AUTH" 2>/dev/null) || root="$REVIEW_AUTH"
    auth_root_ok || { echo "review_auth: refusing $tool-$role" >&2; return 2; }
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
