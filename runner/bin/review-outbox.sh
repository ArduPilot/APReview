#!/bin/bash
# Delivery has its own bounded admission: quota and manual pause only stop
# inference. Each debt retains the configuration which originally created it.
set -eu
. "$HOME/review/bin/review-env.sh"
exec python3 "$REVIEW_ROOT/bin/review-drain.py" --data "$REVIEW_DATA" "$@"
