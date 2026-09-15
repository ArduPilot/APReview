#!/bin/bash
#
# Mark pre-switch AI comments deprecated, as the account that wrote them.
#
# Commenting moved to a bot account (see REVIEW_COMMENT_ACCOUNTS). A GitHub
# comment can only be edited by its author, so the bot cannot collapse the
# comments the previous account left behind: it posts its new review and the old
# one stands alongside it, with nothing on the page saying which is current.
# This job runs as the OLD account and does only that one edit.
#
# It is meant to exist for the handover window and then stop. Past $EXPIRY it
# does nothing but say so, so a forgotten cron entry costs nothing and the old
# account's credentials stop being needed on the box.
#
#   deprecate-legacy-comments.sh [--dry-run] [--accounts "<new> <old>"]
#
set -u
. "$HOME/review/bin/review-env.sh"

# --accounts overrides local.conf, which is sourced by review-env.sh and would
# otherwise win over the environment - without it the guards below cannot be
# exercised on a box that has the real pair configured.
DRY=0; ACCOUNTS="${REVIEW_COMMENT_ACCOUNTS:-}"
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)  DRY=1 ;;
        --accounts) shift; ACCOUNTS="${1:-}" ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

# Last day this job does anything. After it, the bot is the only account.
EXPIRY="${REVIEW_DEPRECATE_UNTIL:-2026-09-30}"
TODAY=$(date +%F)
if [ "$TODAY" \> "$EXPIRY" ]; then
    echo "handover window closed on $EXPIRY - nothing to do."
    echo "Remove this job from the crontab; the old account is no longer used."
    exit 0
fi

# newest first in REVIEW_COMMENT_ACCOUNTS: the bot posts now, the last entry is
# whoever posted before it.
set -- $ACCOUNTS
NEW_ACCOUNT="${1:-}"
LEGACY_ACCOUNT=""
[ $# -gt 0 ] && eval "LEGACY_ACCOUNT=\${$#}"
if [ -z "$NEW_ACCOUNT" ] || [ "$NEW_ACCOUNT" = "$LEGACY_ACCOUNT" ]; then
    echo "REVIEW_COMMENT_ACCOUNTS needs both accounts, newest first - nothing to do."
    exit 0
fi

# This job is the one thing that must NOT run as the bot: only the author may
# edit their own comment. Drop the bot token and use the box's own gh login.
unset GH_TOKEN GITHUB_TOKEN
WHOAMI=$(gh api user --jq .login 2>/dev/null)
if [ "$WHOAMI" != "$LEGACY_ACCOUNT" ]; then
    echo "FATAL: this job must run as $LEGACY_ACCOUNT, but gh is authenticated as ${WHOAMI:-nobody}."
    echo "       It edits that account's own comments; as anyone else every PATCH is a 403."
    exit 1
fi
echo "running as $WHOAMI, marking comments superseded by $NEW_ACCOUNT (until $EXPIRY)"

LABELS="${REVIEW_LABELS:-AIReview DevCallTopic DevCallEU}"
SCOPES="${REVIEW_DEPRECATE_SCOPES:-org:ArduPilot repo:RsyncProject/rsync}"

# Candidates: open PRs under a review label that the old account has commented
# on. The search API covers every repo in one query, which is far cheaper than
# walking the repo list.
CANDS=$(mktemp -p "${TMPDIR:-/tmp}" deprecate-XXXX)
trap 'rm -f "$CANDS"' EXIT
for scope in $SCOPES; do
    for label in $LABELS; do
        gh api -X GET search/issues --paginate \
           -f q="$scope is:pr is:open label:$label commenter:$LEGACY_ACCOUNT" \
           --jq '.items[] | .repository_url + " " + (.number|tostring)' 2>/dev/null
    done
done | sed 's|https://api.github.com/repos/||' | sort -u > "$CANDS"
echo "candidates: $(wc -l < "$CANDS")"

done_n=0; skip_n=0; fail_n=0
while read -r repo num; do
    [ -n "${repo:-}" ] || continue
    body=$(gh api --paginate "repos/$repo/issues/$num/comments" 2>/dev/null | python3 - \
        "$LEGACY_ACCOUNT" "$NEW_ACCOUNT" <<'PY'
import json, sys
legacy, new = sys.argv[1], sys.argv[2]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if isinstance(data, dict):
    data = [data]
ai = [c for c in data if "AI-generated" in (c.get("body") or "")]
mine = [c for c in ai if (c.get("user") or {}).get("login") == legacy]
theirs = [c for c in ai if (c.get("user") or {}).get("login") == new]
if not mine or not theirs:
    sys.exit(0)
last_mine = mine[-1]
# only deprecate once the bot has actually replaced it
if theirs[-1]["created_at"] <= last_mine["created_at"]:
    sys.exit(0)
if last_mine["body"].lstrip().startswith("> **Deprecated"):
    sys.exit(0)
new_body = ("> **Deprecated — see below for the updated review.**\n\n"
            "<details><summary>Previous review (%s)</summary>\n\n%s\n\n</details>\n"
            % (last_mine["created_at"][:10], last_mine["body"]))
print(json.dumps({"id": last_mine["id"], "body": new_body}))
PY
    )
    if [ -z "$body" ]; then skip_n=$((skip_n+1)); continue; fi
    cid=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$body")
    if [ "$DRY" -eq 1 ]; then
        echo "  would deprecate $repo#$num comment $cid"
        done_n=$((done_n+1))
        continue
    fi
    if python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({"body": d["body"]}))' <<<"$body" \
       | gh api -X PATCH "repos/$repo/issues/comments/$cid" --input - >/dev/null 2>&1; then
        echo "  deprecated $repo#$num comment $cid"
        done_n=$((done_n+1))
    else
        echo "  FAILED $repo#$num comment $cid"
        fail_n=$((fail_n+1))
    fi
done < "$CANDS"

echo "deprecated=$done_n unchanged=$skip_n failed=$fail_n$([ "$DRY" -eq 1 ] && echo ' (dry run)')"
