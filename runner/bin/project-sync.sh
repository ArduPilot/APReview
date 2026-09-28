#!/bin/bash
# Update the APReview Results project. Safe to call from anywhere, including a
# run's exit path (which ignores its status). It takes no arguments because the
# sync works out for itself what is labelled, open and reviewed. Exits 75 when
# another sync holds the board, non-zero on failure, so a caller that cares
# can tell.
. "${REVIEW_ENV:-$HOME/review/bin/review-env.sh}" 2>/dev/null || exit 2

# Not as the bot. review-env.sh exports GH_TOKEN so reviews are posted as
# AP-Review, and that token is deliberately narrow - public_repo, nothing else.
# Writing to a project needs the `project` scope, and granting it to the
# commenting token widens what a compromised review run could reach for the
# sake of a board. The box's own login already has repo-wide access, so this is
# the smaller privilege, and nothing here needs to be attributed to the bot:
# project items carry no author.
unset GH_TOKEN GITHUB_TOKEN

# Called from a run's EXIT trap, fd 9 arrives already open on the run lock,
# inherited from run-reviewprs.sh. Close this copy first: it must not be
# mistaken for anything here, and closing it does not release the run's hold.
exec 9>&-

# One sync at a time, on the `board` region of the shared lock file rather
# than a flock of its own, so the review supervisor's deliveries and this
# sweep exclude each other. The sweep takes only that leaf; a drain claims PRs
# before the board and so runs through review-drain.py, never under the board
# lock, or a PR owner waiting for the board would deadlock against it.
if [ "${1:-}" = --drain ]; then
    shift
    exec python3 "$REVIEW_ROOT/bin/review-drain.py" "$@" >>"$REVIEW_LOGS/project-sync.log" 2>&1
fi
exec python3 "$REVIEW_ROOT/bin/review_board_sweep.py" "$@" >>"$REVIEW_LOGS/project-sync.log" 2>&1
