#!/usr/bin/env python3
"""Pause both admissions; taking pause first also covers an old-lock wait."""
import argparse
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from review_control import signal_identity
from review_guardian import alive, identity
from review_lock import acquire
from review_store import atomic, read


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", nargs="?", default="status")
    parser.add_argument("--hold", action="store_true")
    args = parser.parse_args()
    root, data = Path(os.environ["REVIEW_ROOT"]), Path(os.environ["REVIEW_DATA"])
    record = root / "etc/pause.json"
    previous = read(record, {})
    if args.action == "status":
        print("PAUSED until " + time.ctime(previous["until"]) if alive(previous) else "not paused")
        return 0
    if args.action == "resume":
        signal_identity(previous, signal.SIGTERM)
        return 0
    seconds = int(args.action) * 60
    if seconds <= 0:
        parser.error("positive minutes required")
    if not args.hold:
        if alive(previous):
            print("already paused")
            return 0
        child = subprocess.Popen([sys.executable, __file__, args.action, "--hold"],
                                 start_new_session=True, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if alive(read(record, {})):
                print("runs paused; pause-runs.sh resume ends the pause")
                return 0
            if child.poll() is not None:
                return child.returncode or 1
            time.sleep(0.05)
        child.terminate()
        child.wait(timeout=5)
        return 1
    lock = acquire(data / "locks", "pause", time.monotonic() + 2)
    if lock is None:
        return 75
    with lock, open(root / "etc/reviewprs.lock", "a") as old:
        atomic(record, dict(identity(), until=time.time() + seconds))
        deadline = time.monotonic() + seconds
        held = False
        while time.monotonic() < deadline:
            if not held:
                try:
                    fcntl.flock(old, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = True
                except BlockingIOError:
                    pass
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
