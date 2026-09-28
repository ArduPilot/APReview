#!/usr/bin/env python3
"""Print a route, or filter JSON candidate rows on stdin for the old prompt."""
import argparse
import json
import sys

import repos
from review_routing import candidates, load, route, target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode")
    parser.add_argument("--root")
    parser.add_argument("--filter", choices=("old", "new"))
    parser.add_argument("--expect", choices=("old", "new"))
    args = parser.parse_args()
    routing, config = load(args.root), repos.load()
    selected = route(args.mode, routing, config)
    if args.expect and selected != args.expect:
        parser.exit(75, "ownership changed; retry through run-reviewprs.sh\n")
    if args.filter:
        print(json.dumps(candidates(json.load(sys.stdin), routing, target(args.mode, config)[0], args.filter)))
    else:
        print(selected)


if __name__ == "__main__":
    main()
