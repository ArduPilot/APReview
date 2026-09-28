#!/usr/bin/env python3
import argparse
import os
import subprocess
import sys
from pathlib import Path

from review_control import abort, reap
from review_store import read


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("abort", "reap"))
    parser.add_argument("run", nargs="?")
    parser.add_argument("--data", default=os.environ.get("REVIEW_DATA"))
    parser.add_argument("--grace", type=float, default=5)
    args = parser.parse_args()
    if args.action == "abort":
        directory = Path(args.run or "")
        if not directory.is_absolute():
            directory = Path(args.data) / "runs" / directory
        if not args.run or directory.resolve().parent != (Path(args.data) / "runs").resolve():
            parser.error("abort needs a run directory under REVIEW_DATA/runs")
        blocked = abort(directory, args.grace)
        if not blocked and "configuration" in read(directory / "run.json", {}):
            # The request preceded signalling; only after the identities stop
            # may a cleanup controller nonblockingly claim run and PR regions.
            try:
                result = subprocess.run([sys.executable, str(Path(__file__).with_name("review-supervisor.py")),
                                         "--data", args.data, "--resume", str(directory), "--no-coordinator"],
                                        timeout=60)
                if result.returncode:
                    return result.returncode
            except subprocess.TimeoutExpired:
                print("abort recorded; cleanup exceeded deadline, resume later")
                return 75
    else:
        blocked = reap(Path(args.data))
    for path in blocked:
        print("cleanup blocked: " + path)
    return bool(blocked)


if __name__ == "__main__":
    raise SystemExit(main())
