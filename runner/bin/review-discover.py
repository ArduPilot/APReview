#!/usr/bin/env python3
"""Read-only discovery; explicit configuration freezes identities and destinations."""

import argparse
import json
from review_discovery import Discovery
from review_github import GitHub
from review_store import Store, read, atomic


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode")
    parser.add_argument("--config", required=True)
    parser.add_argument("--data", required=True)
    parser.add_argument("--output")
    parser.add_argument("--record")
    parser.add_argument("--replay")
    args = parser.parse_args()
    config = read(args.config)
    github = GitHub(
        args.replay or args.record,
        "replay" if args.replay else "record" if args.record else "live",
        config.get("github_accounts"),
    )
    candidates = Discovery(github, config, Store(args.data)).discover(args.mode)
    if args.output:
        atomic(args.output, candidates)
    else:
        print(json.dumps(candidates, ensure_ascii=False))


if __name__ == "__main__":
    main()
