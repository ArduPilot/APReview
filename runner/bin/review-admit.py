#!/usr/bin/env python3
"""Bounded shell admission and credential leases on shell-owned descriptions."""
import argparse
import fcntl
import os
from pathlib import Path
import time

from review_lock import _flock, _layout, region, try_lock


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=("pause", "accounts"))
    p.add_argument("homes", nargs="*")
    a = p.parse_args()
    path = Path(os.environ["REVIEW_DATA"]) / "locks"
    if a.action == "pause":
        lock = try_lock(path, "pause", shared=True)
        if lock is None:
            print("admission paused")
            return 75
        lock.close()
        return 0
    leases = sorted((region("account:" + tool + "/" + str(Path(home).resolve())), fd)
                    for tool, home, fd in zip(("claude", "codex"), a.homes, (10, 11)))
    for number, fd in leases:
        st, expected = os.fstat(fd), os.stat(path)
        if (st.st_dev, st.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError("credential lease descriptor is not the shared lock file")
        # The layout flock guards a microsecond of initialisation; a run must
        # not be refused because another opener held it at that instant.
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            _layout(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
        deadline = time.monotonic() + 5
        while True:
            try:
                _flock(fd, number, False)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    print("credential account is busy")
                    return 75
                time.sleep(0.05)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
