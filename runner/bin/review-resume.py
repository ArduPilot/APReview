#!/usr/bin/env python3
"""Resume only an unfinished, still-owned run using its frozen configuration."""
import argparse
import os
from pathlib import Path
import sys

from review_routing import load, owner, route
from review_store import read


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--dry", default="0")
    args = parser.parse_args()
    directory = args.run.resolve()
    data = Path(os.environ["REVIEW_DATA"]).resolve()
    if directory.parent != data / "runs":
        parser.error("run must be in REVIEW_DATA/runs")
    config = read(directory / "run.json")
    if not config or config.get("schema") != 1 or config.get("data") != str(data):
        parser.error("missing or incompatible frozen run")
    routing = load()
    if route(config["mode"], routing, config["configuration"]["repos"]) != "new":
        parser.error("run mode is now old-owned; finish the handoff first")
    if any(owner(routing, c.get("mode", config["mode"]), c["repository"]) != "new"
           for c in config["candidates"]):
        parser.error("run contains an old-owned target")
    if read(directory / "summary.json", {}).get("state") == "complete":
        print("run is already complete")
        return
    command = [sys.executable, str(Path(__file__).with_name("review-supervisor.py")),
               "--data", str(data), "--resume", str(directory)]
    if args.dry == "1":
        print("would resume " + str(directory))
    else:
        os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
