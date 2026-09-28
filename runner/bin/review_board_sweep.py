"""Ordinary finite sweep: credentials then board, never a PR claim or drain."""

import os
from pathlib import Path
import subprocess
import sys
from review_lock import try_lock


def main():
    root = Path(os.environ["REVIEW_ROOT"])
    data = Path(os.environ.get("REVIEW_DATA", str(root / "data")))
    data.mkdir(parents=True, exist_ok=True)
    lock_path = data / "locks"
    try:
        account = try_lock(
            lock_path,
            "account:github/" + os.environ.get("REVIEW_PROJECT_ACCOUNT", "project"),
            shared=True,
        )
        if account is None:
            return 75
        with account:
            lock = try_lock(lock_path, "board")
            if lock is None:
                print("skipped: another project sync is running")
                return 75
            with lock:
                return subprocess.run(
                    [sys.executable, str(root / "bin" / "project-sync.py"), *sys.argv[1:]],
                    pass_fds=(account.fd, lock.fd),
                    timeout=300,
                ).returncode
    except (OSError, subprocess.TimeoutExpired) as error:
        print("cannot open the sync lock or complete sync: " + str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
