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

python3 "$REVIEW_ROOT/bin/project-sync.py" "$@" >>"$REVIEW_LOGS/project-sync.log" 2>&1
exit 0
