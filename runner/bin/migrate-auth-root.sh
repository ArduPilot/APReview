#!/bin/bash
#
# Move the accounts out of $REVIEW_ROOT/auth to $REVIEW_AUTH, once.
#
# The reviewing agent is started with --add-dir "$REVIEW_ROOT", so while the
# accounts lived inside it every credential was reachable by a relative path
# from the work it was doing. This moves the directory and repoints the deny
# rule each account's settings.json carries, which the pre-flight requires.
#
# Run it under the run lock, before deploying the code that expects the new
# location. Idempotent: with nothing to move it says so and stops.
#
#   migrate-auth-root.sh [--dry-run]
#
set -u
. "$HOME/review/bin/review-env.sh"

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

OLD="$REVIEW_ROOT/auth"
NEW="$REVIEW_AUTH"

case "$NEW/" in
    "$REVIEW_ROOT"/*) echo "FATAL: $NEW is still inside $REVIEW_ROOT"; exit 1 ;;
esac

if [ ! -d "$OLD" ]; then
    echo "nothing to move: $OLD does not exist"
    [ -d "$NEW" ] && echo "accounts are already at $NEW"
    exit 0
fi
if [ -e "$NEW" ]; then
    echo "FATAL: $NEW already exists; refusing to merge two account roots"
    exit 1
fi

echo "move:  $OLD"
echo "  ->   $NEW"
if [ "$DRY" = 0 ]; then
    # Same filesystem, so this is a rename: the directory never exists in two
    # places, and the role symlinks inside it are relative and stay valid.
    mv -T "$OLD" "$NEW" || { echo "FATAL: move failed"; exit 1; }
    chmod 700 "$NEW"
fi

# Every Claude account carries the deny rule naming the old path. In a dry run
# nothing has moved yet, so look where the accounts still are.
DRY="$DRY" OLD="$OLD" NEW="$NEW" python3 - <<'PY'
import glob, json, os, shutil
dry = os.environ["DRY"] == "1"
old, new = os.environ["OLD"], os.environ["NEW"]
root = old if dry else new
want = "Read(~/%s/**)" % os.path.relpath(new, os.path.expanduser("~"))

def stale_rules(deny):
    # by shape, not by exact string: the rule may be absolute or ~-relative,
    # and an ancestor of the old root no longer covers the accounts
    return [r for r in deny if isinstance(r, str)
            and (old in r or "review/auth" in r or r == "Read(~/review/**)")]

seen = set()
for p in sorted(glob.glob(os.path.join(root, "claude-*", "settings.json"))) + \
         [os.path.expanduser("~/.claude/settings.json")]:
    # a role link resolves onto an account directory already in the list
    real = os.path.realpath(p)
    if real in seen or not os.path.isfile(real):
        continue
    seen.add(real)
    try:
        d = json.load(open(real))
        deny = d["permissions"]["deny"]
        assert isinstance(deny, list)
    except Exception as e:
        print("  SKIP %s: %s" % (p, e)); continue
    stale = stale_rules(deny)
    if want in deny and not stale:
        print("  ok   %s" % p); continue
    print("  edit %s: %s -> %s" % (p, stale or "(add)", want))
    if dry:
        continue
    shutil.copy2(real, real + ".pre-authmove")
    d["permissions"]["deny"] = [r for r in deny if r not in stale] + [want]
    tmp = real + ".new"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2); f.write("\n")
    shutil.copymode(real, tmp)
    os.replace(tmp, real)
PY

echo
if [ "$DRY" = 1 ]; then
    echo "dry run: nothing was moved or edited."
else
    echo "done. Check with:  review-auth.sh status"
fi
