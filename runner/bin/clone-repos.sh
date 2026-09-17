#!/bin/bash
# Base clones of every repo the reviewprs workflow touches.
# ardupilot itself is cloned separately (it needs --recurse-submodules).
. "$HOME/review/bin/review-env.sh"
cd "$REVIEW_REPOS" || exit 1

# The list lives in repos.json, so adding a repo is one edit and both the sweep
# and the base clones follow it. The main repo is cloned separately because it
# needs --recurse-submodules.
REPOS=$("$(dirname "$0")/repos.py" --clone) || {
    echo "FATAL: cannot read the repo list from repos.json"; exit 1; }
[ -n "$REPOS" ] || { echo "FATAL: the repo list is empty"; exit 1; }

# Submodules. A base clone without them is one the agents cannot use for a
# submodule-touching PR: they fall back to fetching from the network, which is
# slow and - inside a review netns, which has no route off lo - impossible.
# Init them here, once, and take the objects from a base we already keep
# whenever the submodule is one of them: WebTools embeds ArduPilot/ardupilot,
# and a second 2.4G copy of it is not worth downloading.
init_submodules() {
    [ -f .gitmodules ] || return 0
    git submodule init >/dev/null 2>&1
    git config --file .gitmodules --get-regexp '^submodule\..*\.path' |
    while read -r key path; do
        name=${key#submodule.}; name=${name%.path}
        url=$(git config --file .gitmodules --get "submodule.$name.url")
        base=$(basename "${url%.git}")
        ref=""; rec="--recursive"
        if [ -d "$REVIEW_REPOS/$base/.git" ]; then
            ref="--reference $REVIEW_REPOS/$base"
            # Do not recurse into a submodule we are referencing: its own
            # submodules are already complete in the base it points at, and
            # recursing here downloads a second copy of them (237M of ardupilot
            # nested modules inside WebTools before this was fixed).
            rec=""
        fi
        # shellcheck disable=SC2086
        if git submodule update --init $rec $ref -- "$path" >/dev/null 2>&1; then
            [ -n "$ref" ] && echo "    submodule $path (referenced $base)"
        else
            echo "    WARN: submodule $path failed"
        fi
    done
}

for r in $REPOS; do
    # The directory comes from the config: the basename, except where an entry
    # says otherwise. mavlink/mavlink lands as upstream-mavlink so it cannot
    # collide with the ardupilot mavlink submodule.
    d=$("$(dirname "$0")/repos.py" --clone-dirs | awk -v r="$r" -F'\t' '$1==r {print $2}')
    # No silent fallback to basename: that is how mavlink/mavlink would land in
    # mavlink/ and collide with the ardupilot submodule of the same name.
    [ -n "$d" ] || { echo "  ERROR: no clone directory for $r"; continue; }
    if [ -d "$d/.git" ]; then
        echo "=== $r: updating $d"
        ( cd "$d" && git fetch --all --prune -q && \
          git reset --hard "origin/$(git symbolic-ref --short HEAD 2>/dev/null || echo master)" -q ) \
          || echo "  WARN: update failed for $d"
    else
        echo "=== $r: cloning into $d"
        git clone -q "https://github.com/$r.git" "$d" || echo "  ERROR: clone failed for $r"
    fi
    [ -d "$d/.git" ] && ( cd "$d" && init_submodules )
done
echo "REPOS_DONE"
