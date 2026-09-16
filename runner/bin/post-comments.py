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
    "mode":     "label",                     # label | followup | pr
    "accounts": ["AP-Review", "tridge"],     # optional, newest first; default
                                             # $REVIEW_COMMENT_ACCOUNTS
    "hold":     ["mavlink/mavlink"],         # never post here, only report
    "comments": [
      {"key": "34292", "repo": "ArduPilot/ardupilot", "number": 34292,
       "head": "0374a23d84",                 # the head this review is of
       "body_file": "bodies/34292.md"}
    ]
  }

Decisions, one per PR, taken by decide() below so they can be tested without a
network. Editing a comment in place sends the author no notification, so an edit
is only ever right when nothing has changed for them: same head, nobody else
has spoken since, and not a mode that promises a fresh comment.

  post       we have never commented here
  edit       ours is the last word AND the head has not moved
  repost     deprecate ours and post afresh, so the author is notified
  unchanged  our newest comment already says exactly this

followup mode never edits - it exists to tell an author their code moved - and
pr mode always leaves a comment, even at a head we have already reviewed,
because a human asked for that review by name.
"""
import argparse
import json
import os
import re
import subprocess
import sys

MARKER = "AI-generated"
DEPRECATED_PREFIX = "> **Deprecated"
DEPRECATION = ("> **Deprecated — see below for the updated review.**\n\n"
               "<details><summary>Previous review (%s)</summary>\n\n%s\n\n</details>\n")


class GhError(Exception):
    """A GitHub call failed. Never silently an empty thread: a failed comments
    fetch looked exactly like a PR we had never commented on, which posts a
    duplicate and leaves the old comment standing."""


def gh_json(*args):
    """gh api ... --paginate, parsed. Raises GhError rather than returning []."""
    out = subprocess.run(["gh", "api", "--paginate", *args],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise GhError("%s: %s" % (args[0],
                                  (out.stderr.strip().splitlines() or ["failed"])[0]))
    try:
        data = json.loads(out.stdout or "[]")
    except json.JSONDecodeError as e:
        raise GhError("%s: unparseable response (%s)" % (args[0], e))
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


TOLD_HEAD = re.compile(r"head `([0-9a-f]{7,40})`")


def told_head(body):
    """The head our previous comment told the author it had reviewed."""
    m = TOLD_HEAD.search(body or "")
    return m.group(1) if m else None


def decide(thread, body, accounts, head=None, mode="label"):
    """What to do with `body` given the PR's thread. Pure; see the tests.

    head  the head this review is of, so a moved head can be noticed
    mode  label | followup | pr - see the module docstring

    Returns (action, comment_id).
    """
    ours = [c for c in thread
            if c["kind"] == "comment" and c["login"] in accounts
            and MARKER in c["body"]]
    if not ours:
        return "post", None
    mine = max(ours, key=lambda c: c["at"] or "")
    same_text = mine["body"].strip() == body.strip()

    # A human asked for this review by name, so it gets a comment even if the
    # code has not moved and the text is identical.
    if mode == "pr":
        return "repost", mine["id"]
    if same_text:
        return "unchanged", mine["id"]
    # Follow-up exists to tell an author their code moved. An edit notifies
    # nobody, so this mode never edits.
    if mode == "followup":
        return "repost", mine["id"]

    newer = [c for c in thread
             if c["login"] not in accounts and (c["at"] or "") > (mine["at"] or "")]
    if newer:
        return "repost", mine["id"]

    # The head moved since we last told them. Editing would update the page and
    # notify nobody - the one person waiting to hear would never know.
    told = told_head(mine["body"])
    if head and told and not (head.startswith(told) or told.startswith(head)):
        return "repost", mine["id"]

    return "edit", mine["id"]


def deprecate_body(original, at):
    return DEPRECATION % ((at or "")[:10], original)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    plan = json.load(open(args.plan))
    mode = plan.get("mode", "label")
    if mode not in ("label", "followup", "pr"):
        sys.exit("post-comments: unknown mode %r" % mode)
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

    def count(name):
        tally[name] = tally.get(name, 0) + 1

    for entry in plan.get("comments", []):
        repo, num = entry["repo"], int(entry["number"])
        what = "%s#%d" % (repo, num)
        path = entry["body_file"]
        if not os.path.isabs(path):
            path = os.path.join(base, path)

        # One unreadable body must not abandon the rest of the batch: the PRs
        # after it in the plan are the ones nobody would notice were skipped.
        try:
            body = open(path, encoding="utf-8").read()
        except OSError as e:
            print("  FAILED  %s: %s" % (what, e))
            count("failed"); failed += 1
            continue

        # Every comment we post says it is machine-written. A body that does not
        # is a bug in the run, not something to publish and apologise for later.
        if MARKER not in body:
            print("  REFUSED %s: body carries no %r marker" % (what, MARKER))
            count("refused"); failed += 1
            continue
        if repo in hold:
            print("  held    %s (upstream repo, needs a human)" % what)
            count("held")
            continue

        try:
            thread = thread_of(repo, num)
        except GhError as e:
            # Posting now would duplicate a comment we simply could not see.
            print("  FAILED  %s: cannot read the thread (%s)" % (what, e))
            count("failed"); failed += 1
            continue

        action, cid = decide(thread, body, accounts,
                             head=entry.get("head"), mode=mode)
        if args.dry_run:
            print("  would %-9s %s%s" % (action, what,
                                         "" if cid is None else " (comment %s)" % cid))
            count(action)
            continue

        if action == "unchanged":
            print("  unchanged %s" % what)
            count("unchanged")
            continue

        if action == "edit":
            if patch(repo, cid, body):
                print("  edited  %s" % what)
                count("edit")
                continue
            # Only the author may edit a comment. After an account switch the
            # previous one is not ours to touch, so fall back to a fresh comment
            # rather than leaving the author with nothing at all.
            print("  note    %s: cannot edit comment %s, posting afresh" % (what, cid))
            action = "repost"

        # Post first, deprecate second. The other order leaves a PR showing
        # "Deprecated - see below for the updated review" with nothing below it
        # when the post fails.
        if not post(repo, num, body):
            print("  FAILED  %s: could not post" % what)
            count("failed"); failed += 1
            continue
        posted = "posted" if action == "post" else "reposted"
        if cid is not None:
            old = [c for c in thread if c["kind"] == "comment" and c["id"] == cid]
            if old and not old[0]["body"].lstrip().startswith(DEPRECATED_PREFIX):
                if not patch(repo, cid, deprecate_body(old[0]["body"], old[0]["at"])):
                    print("  note    %s: new review posted; could not deprecate "
                          "comment %s (not ours to edit)" % (what, cid))
        print("  %-8s %s" % (posted, what))
        count(posted)

    print("post-comments: " + ("  ".join("%s=%d" % kv for kv in sorted(tally.items()))
                               or "nothing to do")
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
