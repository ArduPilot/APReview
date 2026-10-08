#!/bin/bash
# Regenerate runs.html and publish it. Safe to call from anywhere, including
# from a run's exit path - it must never fail the run that invoked it. What
# goes wrong is written to logs/runs-page.log: cron shows nothing, and a
# publisher that fails silently leaves a page that just stops changing.
. "$HOME/review/bin/review-env.sh" 2>/dev/null || exit 0
LOG="${REVIEW_LOGS:-$HOME/review/logs}/runs-page.log"
mkdir -p "$(dirname "$LOG")"
# The page region covers generation and remote replacement together. A second
# publisher must not replace our local input between rendering and rsync.
# The leaf is passed directly to the lock helper: no flag can bypass ownership.
# On timeout the helper kills the whole command, so a slow build can never
# write the page and leave the upload undone.
python3 "$REVIEW_ROOT/bin/review-lock.py" --wait 5 --timeout 300 hold \
    page:review/DevCallReviews/runs.html -- bash -c '
OUT="$REVIEW_DATA/runs.html"
python3 "$REVIEW_ROOT/bin/make-runs-page.py" "$OUT" >/dev/null || exit 2
rsync -a $RSYNC_AUTH "$OUT" "$REVIEW_PUBLISH/DevCallReviews/runs.html" || exit 3
' 2>>"$LOG"
rc=$?
[ "$rc" = 0 ] || echo "$(date -Is) publish-runs-page: exit $rc (2 build, 3 upload, 75 lock busy, 124 killed)" >> "$LOG"
exit 0
