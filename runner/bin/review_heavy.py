"""The heavy command and every descendant retain their own inherited OFD permit."""

import os
from pathlib import Path
import subprocess
import sys
import time

from review_lock import adopt, permit


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    network = bool(args and args[0] == "--netns")
    if network:
        args.pop(0)
    if args[:1] == ["--"]:
        args.pop(0)
    if not args:
        raise ValueError("heavy command required")
    lockfile = Path(os.environ["REVIEW_DATA"]) / "locks"
    inherited = os.environ.get("REVIEW_HEAVY_FD")
    if inherited:
        lock = adopt(lockfile, os.environ["REVIEW_HEAVY_KEY"], int(inherited))
        if not lock.key.startswith("permit:heavy:"):
            raise ValueError("inherited descriptor is not a heavy permit")
    else:
        deadline = time.monotonic() + float(os.environ.get("REVIEW_HEAVY_WAIT", "120"))
        lock = None
        while time.monotonic() < deadline:
            lock = permit(lockfile, "heavy", int(os.environ.get("REVIEW_HEAVY_SIZE", "4")))
            if lock:
                break
            time.sleep(0.05)
        if lock is None:
            return 75
    with lock:
        env = dict(os.environ, REVIEW_HEAVY_FD=str(lock.fd), REVIEW_HEAVY_KEY=lock.key)
        if network:
            args = [str(Path(__file__).with_name("netns-run.sh")), *args]
        return subprocess.run(args, env=env, pass_fds=(lock.fd,)).returncode


if __name__ == "__main__":
    raise SystemExit(main())
