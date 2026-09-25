#!/bin/bash
# Update the APReview Results project. Safe to call from anywhere, including a
# run's exit path - like publish-runs-page.sh, it must never fail the run that
# invoked it, and it takes no arguments because the sync works out for itself
# what is labelled, open and reviewed.
. "$HOME/review/bin/review-env.sh" 2>/dev/null || exit 0
python3 "$REVIEW_ROOT/bin/project-sync.py" "$@" >>"$REVIEW_LOGS/project-sync.log" 2>&1
exit 0
