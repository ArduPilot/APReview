#!/usr/bin/env python3
"""Record context.json for finished inference passes (review_context.py).

Runs from cron, apart from the passes: it measures an attempt only once its
guardian has exited, so measuring can never hold a pass's locks or delay
what follows it. Idempotent: an attempt already measured by the current
parser is left alone, unless its measurement failed in a way a later try
could fix (a rollout not yet written)."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_context import PARSER, record  # noqa: E402
from review_guardian import alive  # noqa: E402


def load(path):
    try:
        with open(path) as f:
            value = json.load(f)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


QUIET = 900        # a guardian dead this long without a terminal status was killed


def finished(attempt, status):
    """The pass is over: terminal, or its guardian died before saying so."""
    try:
        if alive(status):               # its guardian may still hold locks
            return False
        if status.get("state") == "terminal":
            return True
        return time.time() - (attempt / "status.json").stat().st_mtime > QUIET
    except Exception:
        return False


def due(attempt, force):
    status = load(attempt / "status.json")
    if not status or not status.get("pid") or not finished(attempt, status):
        return False
    old = load(attempt / "context.json")
    if force or not old or old.get("parser") != PARSER:
        return True
    return bool(old.get("error")) and bool(old.get("retry"))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", default=os.environ.get("REVIEW_DATA", os.path.expanduser("~/review/data")))
    p.add_argument("--days", type=float, default=3, help="attempts finished within this many days")
    p.add_argument("--force", action="store_true", help="measure again even if already measured")
    p.add_argument("--seconds", type=float, default=600, help="start no new attempt after this long")
    a = p.parse_args()
    since, deadline = time.time() - a.days * 86400, time.monotonic() + a.seconds
    # one collector at a time: a long run must not overlap the next
    os.makedirs(os.path.join(a.data, "metrics"), exist_ok=True)
    guard = open(os.path.join(a.data, "metrics", "context.lock"), "a")
    try:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("context: another collector is running")
        return
    done = unknown = 0
    for status in sorted(Path(a.data, "runs").glob("*/attempts/*/status.json")):
        if time.monotonic() >= deadline:
            print("context: time budget spent", file=sys.stderr)
            break
        attempt = status.parent
        try:
            if status.stat().st_mtime < since or not due(attempt, a.force):
                continue
            job = load(attempt / "job.json")
            if not job:
                continue
            result = record(attempt, job)
        except Exception as error:      # one attempt's trouble is its own
            print("context: %s: %s" % (attempt, str(error)[:200]), file=sys.stderr)
            continue
        if result.get("error"):
            unknown += 1
        else:
            done += 1
    print("context: %d measured, %d unknown" % (done, unknown))


if __name__ == "__main__":
    main()
