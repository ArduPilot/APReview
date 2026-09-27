#!/bin/bash
# Update the APReview Results project. Safe to call from anywhere, including a
# run's exit path - like publish-runs-page.sh, it must never fail the run that
# invoked it, and it takes no arguments because the sync works out for itself
# what is labelled, open and reviewed.
. "$HOME/review/bin/review-env.sh" 2>/dev/null || exit 0

# Not as the bot. review-env.sh exports GH_TOKEN so reviews are posted as
# AP-Review, and that token is deliberately narrow - public_repo, nothing else.
# Writing to a project needs the `project` scope, and granting it to the
# commenting token widens what a compromised review run could reach for the
# sake of a board. The box's own login already has repo-wide access, so this is
# the smaller privilege, and nothing here needs to be attributed to the bot:
# project items carry no author.
unset GH_TOKEN GITHUB_TOKEN

# One sync at a time. The cron sweep and a run's exit path can fire together,
# and two in flight interleave: the first reads the board, the second adds a
# row, the first then deletes it as "not in my search". -n rather than a wait,
# because the next sweep is fifteen minutes away and this must never hold up
# the run that called it.
exec 9>"$REVIEW_ROOT/etc/project-sync.lock"
if ! flock -n 9; then
    echo "$(date -Is) skipped: another project sync is running" \
        >>"$REVIEW_LOGS/project-sync.log"
    exit 0
fi

python3 "$REVIEW_ROOT/bin/project-sync.py" "$@" >>"$REVIEW_LOGS/project-sync.log" 2>&1 9>&-
exit 0
