#!/usr/bin/env python3
import argparse
import json
import os
import shlex
from review_handoff import cutover, handoff

p = argparse.ArgumentParser(description="Drained rsync handoff; pages must be a complete publication mirror")
p.add_argument("direction", choices=("new", "old"))
p.add_argument("--repository", default="RsyncProject/rsync")
p.add_argument("--pages", required=True)
p.add_argument("--dry-run", action="store_true")
p.add_argument("--wait", type=float, default=300)
p.add_argument("--full", action="store_true",
               help="transfer every mode, label and repository at once, importing all shared pages")
a = p.parse_args()
if a.full:
    if a.direction != "new":
        p.error("--full only transfers to new; there is no automated reverse")
    from review_github import GitHub
    print(json.dumps(cutover(os.environ["REVIEW_ROOT"], os.environ["REVIEW_DATA"], a.pages,
                             dry=a.dry_run, wait=a.wait, publish=os.environ.get("REVIEW_PUBLISH"),
                             rsync_args=shlex.split(os.environ.get("RSYNC_AUTH", "")),
                             github=GitHub()), indent=2))
    raise SystemExit(0)
print(json.dumps(handoff(os.environ["REVIEW_ROOT"], os.environ["REVIEW_DATA"],
                         a.repository, a.direction, a.pages, dry=a.dry_run, wait=a.wait,
                         publish=os.environ.get("REVIEW_PUBLISH"),
                         rsync_args=shlex.split(os.environ.get("RSYNC_AUTH", ""))), indent=2))
