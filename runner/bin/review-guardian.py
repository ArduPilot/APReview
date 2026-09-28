#!/usr/bin/env python3
"""Entry point kept small so the guardian protocol is usable by recovery too."""
import argparse
from pathlib import Path

from review_guardian import manager, receive_fd, run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--attempt", type=Path, required=True)
    parser.add_argument("--pr-fd", type=int)
    parser.add_argument("--socket")
    parser.add_argument("--manager", action="store_true")
    args = parser.parse_args()
    fd = receive_fd(args.socket) if args.socket else args.pr_fd
    if fd is None:
        parser.error("inherited PR descriptor required")
    return (manager if args.manager else run)(args.data, args.attempt, fd)


if __name__ == "__main__":
    raise SystemExit(main())
