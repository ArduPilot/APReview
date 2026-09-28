#!/bin/bash
# Hold the run lock for N minutes so no review run can start. Scheduled runs hit
# the lock and either skip or wait, exactly as they do behind a real run - no
# crontab edits, nothing to remember to restore, and it expires on its own.
#
#   pause-runs.sh 60      # pause for 60 minutes
#   pause-runs.sh status
#   pause-runs.sh resume  # end the pause early
# The pause region is acquired before waiting for the legacy lock. Otherwise a
# long old run would leave new admission open throughout the requested pause.
set -u
. "$HOME/review/bin/review-env.sh" 2>/dev/null || exit 1
exec python3 "$REVIEW_ROOT/bin/review-pause.py" "$@"
