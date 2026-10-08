#!/usr/bin/env python3
"""Give bounded shell leaf operations the same lock domain as the scheduler."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from review_lock import acquire


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=os.environ.get("REVIEW_DATA"))
    parser.add_argument("--wait", type=float, default=5)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("action", choices=["hold"])
    parser.add_argument("key")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.data:
        parser.error("--data or REVIEW_DATA required")
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("command required")
    lock = acquire(Path(args.data) / "locks", args.key, time.monotonic() + args.wait)
    if lock is None:
        return 75
    with lock:
        child = subprocess.Popen(command, pass_fds=(lock.fd,), process_group=0)
        try:
            return child.wait(timeout=args.timeout)
        except BaseException as exc:
            # Killing the command alone leaves what it started working on the
            # region after the lock is gone: take its whole process group.
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            if not isinstance(exc, subprocess.TimeoutExpired):
                raise
            print("review-lock: %s: command killed after %gs" % (args.key, args.timeout), file=sys.stderr)
            return 124


if __name__ == "__main__":
    raise SystemExit(main())
