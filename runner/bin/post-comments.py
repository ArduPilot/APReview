#!/usr/bin/env python3
"""Post or update the AI review comment on each PR in a plan.

The command used to write this logic from scratch into a scratch directory on
every run - 16 copies of a post.py in ten days, each one re-deriving the
edit-versus-repost rules and hardcoding the posting account at the top. That is
work with one right answer, so it belongs here, tested once, where a fix to the
identity rules cannot silently fail to reach the next run.

  post-comments.py plan.json [--dry-run]

The plan is written by the review run:

  {
    "accounts": ["AP-Review", "tridge"],     # optional, newest first; default
                                             # $REVIEW_COMMENT_ACCOUNTS
    "hold":     ["mavlink/mavlink"],         # never post here, only report
    "comments": [
      {"key": "34292", "repo": "ArduPilot/ardupilot", "number": 34292,
       "body_file": "bodies/34292.md"}
    ]
  }

Decisions, one per PR, taken by decide() below so they can be tested without a
network: post a new comment, edit ours in place when it is still the last word,
or deprecate ours and repost so the author gets a notification. A body that is
byte-identical to what is already there is left alone, so a re-run of a
half-finished run does not add noise.
"""
import argparse
import json
import os
import subprocess
import sys

MARKER = "AI-generated"
DEPRECATED_PREFIX = "> **Deprecated"
DEPRECATION = ("> **Deprecated — see below for the updated review.**\n\n"
               "<details><summary>Previous review (%s)</summary>\n\n%s\n\n</details>\n")


def gh_json(*args):
    """gh api ... --paginate, parsed. [] when the call fails."""
    out = subprocess.run(["gh", "api", "--paginate", *args],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return []
    try:
        data = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        # --paginate concatenates arrays as separate documents on some versions
        data = []
        for chunk in out.stdout.replace("][", "]\x00[").split("\x00"):
            try:
                data.extend(json.loads(chunk))
            except json.JSONDecodeError:
                pass
    return data if isinstance(data, list) else [data]


def thread_of(repo, number):
    """Everything a human could have added since our comment, in one list."""
    items = []
    for c in gh_json("repos/%s/issues/%d/comments" % (repo, number)):
        items.append({"kind": "comment", "id": c.get("id"),
                      "login": (c.get("user") or {}).get("login"),
                      "at": c.get("created_at"), "body": c.get("body") or ""})
    for c in gh_json("repos/%s/pulls/%d/comments" % (repo, number)):
        items.append({"kind": "review_comment", "id": c.get("id"),
                      "login": (c.get("user") or {}).get("login"),
                      "at": c.get("created_at"), "body": c.get("body") or ""})
    for r in gh_json("repos/%s/pulls/%d/reviews" % (repo, number)):
        items.append({"kind": "review", "id": r.get("id"),
                      "login": (r.get("user") or {}).get("login"),
                      "at": r.get("submitted_at"), "body": r.get("body") or ""})
    return items


def decide(thread, body, accounts):
    """What to do with `body` given the PR's thread. Pure; see the tests.

    Returns (action, comment_id) where action is one of:
      post       - we have never commented here
      unchanged  - our newest comment already says exactly this
      edit       - ours is still the last word, so update it in place
      repost     - somebody has spoken since; deprecate ours and post afresh
    """
    ours = [c for c in thread
            if c["kind"] == "comment" and c["login"] in accounts
            and MARKER in c["body"]]
    if not ours:
        return "post", None
    mine = max(ours, key=lambda c: c["at"] or "")
    if mine["body"].strip() == body.strip():
        return "unchanged", mine["id"]
    newer = [c for c in thread
             if c["login"] not in accounts and (c["at"] or "") > (mine["at"] or "")]
    return ("repost" if newer else "edit"), mine["id"]


def deprecate_body(original, at):
    return DEPRECATION % ((at or "")[:10], original)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    plan = json.load(open(args.plan))
    accounts = plan.get("accounts") or (
        os.environ.get("REVIEW_COMMENT_ACCOUNTS", "").split())
    if not accounts:
        who = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                             capture_output=True, text=True)
        accounts = [who.stdout.strip()] if who.returncode == 0 else []
    if not accounts:
        sys.exit("post-comments: no posting account known "
                 "(set REVIEW_COMMENT_ACCOUNTS or plan.accounts)")
    hold = set(plan.get("hold") or [])
    base = os.path.dirname(os.path.abspath(args.plan))
    tally = {}
    failed = 0

    for entry in plan.get("comments", []):
        repo, num = entry["repo"], int(entry["number"])
        path = entry["body_file"]
        if not os.path.isabs(path):
            path = os.path.join(base, path)
        body = open(path, encoding="utf-8").read()
        what = "%s#%d" % (repo, num)

        # Every comment we post says it is machine-written. A body that does not
        # is a bug in the run, not something to publish and apologise for later.
        if MARKER not in body:
            print("  REFUSED %s: body carries no %r marker" % (what, MARKER))
            failed += 1
            continue
        if repo in hold:
            print("  held    %s (upstream repo, needs a human)" % what)
            tally["held"] = tally.get("held", 0) + 1
            continue

        action, cid = decide(thread_of(repo, num), body, accounts)
        if args.dry_run:
            print("  would %-9s %s%s" % (action, what,
                                         "" if cid is None else " (comment %s)" % cid))
            tally[action] = tally.get(action, 0) + 1
            continue

        ok = True
        if action == "unchanged":
            pass
        elif action == "edit":
            ok = patch(repo, cid, body)
        else:
            if action == "repost":
                old = [c for c in thread_of(repo, num) if c["id"] == cid]
                if old and not old[0]["body"].lstrip().startswith(DEPRECATED_PREFIX):
                    # Only the author may edit a comment. After an account
                    # switch the old one belongs to somebody else, so this fails
                    # by design - post the new review and say so.
                    if not patch(repo, cid, deprecate_body(old[0]["body"], old[0]["at"])):
                        print("  note    %s: could not deprecate comment %s "
                              "(not ours to edit)" % (what, cid))
            ok = post(repo, num, body)
        print("  %-9s %s" % (action if ok else "FAILED", what))
        tally[action if ok else "failed"] = tally.get(action if ok else "failed", 0) + 1
        failed += 0 if ok else 1

    print("post-comments: " + "  ".join("%s=%d" % kv for kv in sorted(tally.items()))
          + ("  (dry run)" if args.dry_run else ""))
    return 1 if failed else 0


def patch(repo, cid, body):
    return _gh_write(["-X", "PATCH", "repos/%s/issues/comments/%s" % (repo, cid)], body)


def post(repo, num, body):
    return _gh_write(["repos/%s/issues/%d/comments" % (repo, num)], body)


def _gh_write(args, body):
    out = subprocess.run(["gh", "api", *args, "--input", "-"],
                         input=json.dumps({"body": body}),
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.stderr.write("    gh: %s\n" % (out.stderr.strip().splitlines() or [""])[0])
    return out.returncode == 0


if __name__ == "__main__":
    sys.exit(main())
