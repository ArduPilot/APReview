#!/bin/bash
# Move the accounts outside the granted tree, under the run lock.
# No deployed environment is sourced: it may still name the old root, and
# sourcing it would create scratch directories even during a dry run.
exec python3 - "$@" <<'PYMIG'
import argparse
import glob
import json
import os
import re
import stat
import sys
import tempfile


def inside(path, root):
    return os.path.commonpath([path, root]) == root


def regular(path):
    s = os.lstat(path)
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
        raise ValueError("not a plain file: " + path)
    return s


def check_destination(path):
    if os.path.lexists(path):
        regular(path)


def unreadable(error):
    raise error


def write_settings(path, original, updated, mode):
    backup = path + ".pre-authmove"
    check_destination(backup)
    # Exclusive creation keeps a leftover or planted name from redirecting us.
    fd, tmp = tempfile.mkstemp(prefix=".authmove-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(original)
            f.flush()
            os.fsync(f.fileno())
        if not os.path.lexists(backup):
            os.replace(tmp, backup)
        if os.path.lexists(tmp):
            os.unlink(tmp)
        fd, tmp = tempfile.mkstemp(prefix=".authmove-", dir=os.path.dirname(path))
        with os.fdopen(fd, "wb") as f:
            f.write(updated)
            f.flush()
            os.fsync(f.fileno())
            os.fchmod(f.fileno(), mode)
        regular(path)
        os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


def migrate():
    parser = argparse.ArgumentParser(description="Move review accounts under the run lock")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--auth-root", default=os.environ.get("REVIEW_AUTH"),
                        help="destination; defaults to $HOME/review.auth")
    args = parser.parse_args()
    home = os.path.realpath(os.path.expanduser("~"))
    review = os.path.realpath(os.path.join(home, "review"))
    old = os.path.join(review, "auth")
    destination = os.path.abspath(os.path.expanduser(args.auth_root or home + "/review.auth"))
    new = os.path.realpath(destination)
    if inside(new, review):
        raise ValueError("destination is inside the granted review root: " + destination)
    if inside(review, new):
        raise ValueError("destination contains the review root: " + destination)
    if any(ord(c) < 32 for c in new):
        raise ValueError("destination cannot be represented by a supported permission rule")
    if os.path.islink(old) or os.path.islink(destination):
        raise ValueError("auth roots must be directories, not symlinks")
    moving = os.path.lexists(old)
    if moving and os.path.lexists(destination):
        raise ValueError("destination already exists; refusing to merge two account roots")
    root = old if moving else new
    if not os.path.isdir(root):
        raise ValueError("account root is not a directory: " + root)
    if moving and os.stat(root).st_dev != os.stat(os.path.dirname(new)).st_dev:
        raise ValueError("cross-filesystem move refused; credentials require a rename")

    # Refuse links that need a coordinated rewrite rather than strand them if
    # the process stops between the rename and a link update.
    links = []
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
        links.extend(os.path.join(directory, n) for n in dirs + files
                     if os.path.islink(os.path.join(directory, n)))
    links.extend(os.path.join(home, "." + tool) for tool in ("claude", "codex")
                 if os.path.islink(os.path.join(home, "." + tool)))
    for link in links:
        target = os.path.realpath(link)
        if not os.path.exists(link):
            raise ValueError("dangling symlink: " + link)
        if inside(target, review) and not (moving and inside(target, old)):
            raise ValueError("symlink reaches inside the granted review root: " + link)
        if moving:
            value = os.readlink(link)
            direct = os.path.abspath(os.path.join(os.path.dirname(link), value))
            if inside(link, old):
                if os.path.isabs(value) and inside(target, old):
                    raise ValueError("absolute symlink needs repointing before migration: " + link)
                if not os.path.isabs(value) and not inside(direct, old):
                    raise ValueError("relative symlink leaves the moving root: " + link)
            elif inside(target, old):
                raise ValueError("tool home symlink requires coordinated manual migration: " + link)

    pattern = "~/" + os.path.relpath(new, home) if inside(new, home) and new != home else "/" + new
    pattern = re.sub(r"([\\*?\[\]])", r"\\\1", pattern)
    pattern = pattern.replace("\\\\", "\\\\\\\\")
    want = "Read(%s/**)" % pattern
    accounts = sorted(glob.glob(os.path.join(glob.escape(root), "claude-*")))
    own = os.path.join(home, ".claude")
    if os.path.exists(own):
        accounts.append(own)
    edits, seen = [], set()
    for account in accounts:
        if not os.path.isdir(account):
            raise ValueError("account is not a directory: " + account)
        directory = os.path.realpath(account)
        if directory in seen:
            continue
        seen.add(directory)
        path = os.path.join(directory, "settings.json")
        mode = stat.S_IMODE(regular(path).st_mode)
        check_destination(path + ".pre-authmove")
        check_destination(path + ".new")
        with open(path, "rb") as f:
            original = f.read()
        try:
            d = json.loads(original)
            permissions = d["permissions"]
            deny = permissions["deny"]
            if not isinstance(deny, list) or not all(isinstance(r, str) for r in deny):
                raise ValueError("permissions.deny must be an array of rules")
            if permissions.get("defaultMode") != "auto":
                raise ValueError("permissions.defaultMode must be auto")
            if not all(r in deny for r in ("Bash(git push)", "Bash(git push:*)")):
                raise ValueError("git push denials are missing")
        except (ValueError, KeyError, TypeError) as e:
            raise ValueError("invalid settings: %s (%s)" % (path, e)) from None
        if want in deny:
            continue
        # An ancestor rule may protect other secrets as well as these accounts.
        permissions["deny"] = deny + [want]
        updated = (json.dumps(d, indent=2) + "\n").encode()
        final = os.path.join(new, os.path.relpath(path, old)) if moving and inside(path, old) else path
        edits.append((final, original, updated, mode))

    print("move: %s -> %s" % (old, new) if moving else "accounts are already at " + new)
    for path, _, _, _ in edits:
        print("  add %s: %s" % (path, want))
    if args.dry_run:
        print("dry run: nothing was moved or edited.")
        return
    if moving:
        os.rename(old, new)
    if inside(os.path.realpath(new), review):
        raise ValueError("moved root is inside the granted review root")
    if stat.S_IMODE(os.stat(new).st_mode) != 0o700:
        os.chmod(new, 0o700)
    for edit in edits:
        write_settings(*edit)
    print("done. Account settings validated; check role identities with review-auth.sh status")


try:
    migrate()
except (OSError, ValueError) as e:
    print("FATAL: %s" % e, file=sys.stderr)
    sys.exit(1)
PYMIG
