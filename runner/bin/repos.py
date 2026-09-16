#!/usr/bin/env python3
"""Query repos.json - the list of repos this command sweeps.

Adding a repo is an edit to repos.json and nothing else: the command reads it
rather than carrying a list, and clone-repos.sh reads it to decide which base
clones to maintain.

  repos.py --sweep            owner/repo of every repo swept by label or author
  repos.py --clone            repos that want a maintained base clone
  repos.py --keys             "<key>\towner/repo" for manifest keys
  repos.py --clone-dirs       "owner/repo\tdirectory" for the base clones
  repos.py --notes <repo>     what a reviewer needs to know about one repo
  repos.py --json             the whole file, for a reader that wants it all
"""
import json
import os
import sys


def config_path():
    env = os.environ.get("REVIEW_REPO_CONFIG")
    if env:
        return env
    # runner/bin/repos.py -> the checkout root
    return os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "repos.json")


def load():
    with open(config_path()) as f:
        return json.load(f)


def main(argv):
    cfg = load()
    repos = cfg["repos"]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    what = argv[0]
    if what == "--sweep":
        for r in repos:
            if r.get("discovery") in ("main", "explicit"):
                print(r["repo"])
    elif what == "--clone":
        # the main repo is cloned separately, with submodules
        for r in repos:
            if r.get("discovery") != "main":
                print(r["repo"])
    elif what == "--clone-dirs":
        for r in repos:
            if r.get("discovery") != "main":
                print("%s\t%s" % (r["repo"],
                                  r.get("clone_dir") or r["repo"].split("/")[-1]))
    elif what == "--keys":
        for r in repos:
            print("%s\t%s" % (r.get("key", ""), r["repo"]))
    elif what == "--notes":
        if len(argv) < 2:
            sys.exit("repos.py --notes <owner/repo or key>")
        want = argv[1]
        for r in repos:
            if want in (r["repo"], r.get("key"), r["repo"].split("/")[-1]):
                print(r.get("notes") or "(no special instructions)")
                return 0
        sys.exit("repos.py: %s is not in %s" % (want, config_path()))
    elif what == "--json":
        json.dump(cfg, sys.stdout, indent=1)
        print()
    else:
        sys.exit("repos.py: unknown option %s" % what)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
