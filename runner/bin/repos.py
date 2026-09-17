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
    """repos.json from the checkout this script belongs to.

    realpath, not abspath: the runner's ~/review/bin is a symlink into the
    checkout, and abspath would stop at the symlink and look for repos.json in
    ~/review. REVIEW_REPO_CONFIG overrides, for tests - it deliberately is not
    exported by review-env.sh, because a deployed value would shadow the config
    of whatever checkout a developer was actually editing.
    """
    env = os.environ.get("REVIEW_REPO_CONFIG")
    if env:
        return env
    here = os.path.realpath(__file__)                 # .../runner/bin/repos.py
    return os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(here))), "repos.json")


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
        want = argv[1].lower()
        # Match the full name or the manifest key only. Matching the basename as
        # well made `--notes mavlink` answer with mavlink/mavlink's rules - hold
        # the comment, upstream conventions - when `mavlink` is the manifest key
        # of the ArduPilot fork, which is swept as a submodule and is a different
        # project with different rules. That is the collision key_collisions
        # exists to prevent, arriving through the lookup instead.
        for r in repos:
            if want in (r["repo"].lower(), (r.get("key") or "").lower()):
                print(r.get("notes") or "(no special instructions)")
                return 0
        for r in repos:
            if want == r["repo"].split("/")[-1].lower():
                sys.exit("repos.py: %s is ambiguous - %s has the manifest key "
                         "%r. Ask by full name or by key."
                         % (argv[1], r["repo"], r.get("key")))
        # A repo can be swept without being listed: ardupilot's own submodules are
        # found through .gitmodules. Say so, rather than leaving a reviewer who
        # asked about ArduPilot/mavlink with nothing.
        owners = [o.lower() for o in cfg.get("submodule_sweep", {}).get("owners", [])]
        if "/" in want and want.split("/")[0] in owners:
            print("Not listed in repos.json: ArduPilot-owned submodules of the main "
                  "repo are swept through .gitmodules and keyed by their basename. "
                  "The main repo's rules apply (house_rules: ardupilot), and "
                  "comments post normally.")
            return 0
        sys.exit("repos.py: %s is not in %s" % (argv[1], config_path()))
    elif what == "--json":
        json.dump(cfg, sys.stdout, indent=1)
        print()
    else:
        sys.exit("repos.py: unknown option %s" % what)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
