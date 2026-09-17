#!/usr/bin/env python3
"""Tests for the posting tool's decisions. No network: decide() is pure.

Each case is one the review workflow actually hits, and several are bugs that
reached production when this logic was rewritten per run.
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "post_comments", os.path.join(HERE, "..", "bin", "post-comments.py"))
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

BOT, OLD, HUMAN = "AP-Review", "tridge", "peterbarker"
ACCOUNTS = [BOT, OLD]
AI = "**Automated review note — AI-generated (Claude)** reviewed at head `abcdef1234`."
NEW_BODY = "**Automated review note — AI-generated (Claude)** reviewed at head `999999aaaa`."


def c(login, at, body, kind="comment", cid=1):
    return {"kind": kind, "id": cid, "login": login, "at": at, "body": body}


class Decide(unittest.TestCase):
    def test_never_commented_here(self):
        self.assertEqual(pc.decide([c(HUMAN, "2026-09-01T00:00:00Z", "hi")],
                                   NEW_BODY, ACCOUNTS), ("post", None))

    def test_same_head_and_nobody_since_so_edit_in_place(self):
        # an edit notifies nobody, so it is only right when nothing has changed
        # for the author: same head, and no one has spoken since
        thread = [c(HUMAN, "2026-09-01T00:00:00Z", "hi"),
                  c(BOT, "2026-09-02T00:00:00Z", AI, cid=7)]
        self.assertEqual(pc.decide(thread, NEW_BODY, ACCOUNTS, head="abcdef1234"),
                         ("edit", 7))

    def test_the_head_moved_so_repost_even_though_nobody_spoke(self):
        # the case this tool shipped without: the author force-pushes to address
        # the review, says nothing, and an edit sends them no notification
        thread = [c(BOT, "2026-09-02T00:00:00Z", AI, cid=7)]
        self.assertEqual(pc.decide(thread, NEW_BODY, ACCOUNTS, head="999999aaaa"),
                         ("repost", 7))

    def test_a_short_head_matching_the_told_prefix_is_the_same_head(self):
        thread = [c(BOT, "2026-09-02T00:00:00Z", AI, cid=7)]
        self.assertEqual(pc.decide(thread, NEW_BODY, ACCOUNTS,
                                   head="abcdef1234567890"), ("edit", 7))

    def test_decide_without_a_head_falls_back_to_who_spoke_last(self):
        # decide() stays permissive so it can be reasoned about in isolation;
        # main() is what refuses a plan entry that omits the head
        thread = [c(BOT, "2026-09-02T00:00:00Z", AI, cid=7)]
        self.assertEqual(pc.decide(thread, NEW_BODY, ACCOUNTS), ("edit", 7))

    def test_somebody_replied_since_so_repost(self):
        thread = [c(BOT, "2026-09-02T00:00:00Z", AI, cid=7),
                  c(HUMAN, "2026-09-03T00:00:00Z", "I disagree")]
        self.assertEqual(pc.decide(thread, NEW_BODY, ACCOUNTS), ("repost", 7))

    def test_a_review_counts_as_somebody_speaking(self):
        thread = [c(BOT, "2026-09-02T00:00:00Z", AI, cid=7),
                  c(HUMAN, "2026-09-03T00:00:00Z", "", kind="review", cid=9)]
        self.assertEqual(pc.decide(thread, NEW_BODY, ACCOUNTS), ("repost", 7))

    def test_an_inline_review_comment_counts_too(self):
        thread = [c(BOT, "2026-09-02T00:00:00Z", AI, cid=7),
                  c(HUMAN, "2026-09-03T00:00:00Z", "nit", kind="review_comment", cid=9)]
        self.assertEqual(pc.decide(thread, NEW_BODY, ACCOUNTS), ("repost", 7))

    def test_our_own_later_activity_is_not_somebody_else(self):
        thread = [c(BOT, "2026-09-02T00:00:00Z", AI, cid=7),
                  c(OLD, "2026-09-03T00:00:00Z", "", kind="review", cid=9)]
        self.assertEqual(pc.decide(thread, NEW_BODY, ACCOUNTS), ("edit", 7))

    def test_a_comment_from_the_account_we_replaced_is_still_ours(self):
        # the whole point of the identity set: after the switch, the previous
        # account's comment must be found, or the run posts a duplicate
        thread = [c(OLD, "2026-09-02T00:00:00Z", AI, cid=7)]
        self.assertEqual(pc.decide(thread, NEW_BODY, ACCOUNTS), ("edit", 7))

    def test_matching_only_the_current_account_would_duplicate(self):
        thread = [c(OLD, "2026-09-02T00:00:00Z", AI, cid=7)]
        self.assertEqual(pc.decide(thread, NEW_BODY, [BOT]), ("post", None))

    def test_identical_body_is_left_alone(self):
        thread = [c(BOT, "2026-09-02T00:00:00Z", AI, cid=7)]
        self.assertEqual(pc.decide(thread, AI, ACCOUNTS), ("unchanged", 7))

    def test_identical_but_for_trailing_whitespace_is_still_unchanged(self):
        thread = [c(BOT, "2026-09-02T00:00:00Z", AI + "\n\n", cid=7)]
        self.assertEqual(pc.decide(thread, AI, ACCOUNTS), ("unchanged", 7))

    def test_a_human_comment_of_ours_without_the_marker_is_not_a_review(self):
        # tridge commenting as himself must not be mistaken for an AI review
        thread = [c(OLD, "2026-09-02T00:00:00Z", "looks fine to me", cid=7)]
        self.assertEqual(pc.decide(thread, NEW_BODY, ACCOUNTS), ("post", None))

    def test_the_newest_of_several_of_ours_is_the_one_updated(self):
        thread = [c(OLD, "2026-09-01T00:00:00Z", AI, cid=5),
                  c(BOT, "2026-09-04T00:00:00Z", AI, cid=8)]
        self.assertEqual(pc.decide(thread, NEW_BODY, ACCOUNTS), ("edit", 8))


class Modes(unittest.TestCase):
    """followup must notify; pr must always leave a comment."""

    def test_followup_never_edits(self):
        thread = [c(BOT, "2026-09-02T00:00:00Z", AI, cid=7)]
        self.assertEqual(pc.decide(thread, NEW_BODY, ACCOUNTS,
                                   head="abcdef1234", mode="followup"),
                         ("repost", 7))

    def test_pr_mode_comments_even_at_an_unchanged_head(self):
        thread = [c(BOT, "2026-09-02T00:00:00Z", AI, cid=7)]
        self.assertEqual(pc.decide(thread, AI, ACCOUNTS,
                                   head="abcdef1234", mode="pr"), ("repost", 7))

    def test_label_mode_still_leaves_an_identical_body_alone(self):
        thread = [c(BOT, "2026-09-02T00:00:00Z", AI, cid=7)]
        self.assertEqual(pc.decide(thread, AI, ACCOUNTS, head="abcdef1234"),
                         ("unchanged", 7))


class ApiFailure(unittest.TestCase):
    """A failed call must never look like a PR we have not commented on."""

    def test_gh_json_raises_rather_than_returning_empty(self):
        import subprocess as sp
        real = pc.subprocess.run
        pc.subprocess.run = lambda *a, **k: sp.CompletedProcess(
            a[0], 1, "", "gh: API rate limit exceeded")
        try:
            with self.assertRaises(pc.GhError):
                pc.gh_json("repos/x/y/issues/1/comments")
        finally:
            pc.subprocess.run = real


class TheWiring(unittest.TestCase):
    """main()'s ordering and fallbacks - where reading the code is not enough."""

    def setUp(self):
        self.calls = []
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = (pc.thread_of, pc.patch, pc.post)
        self.addCleanup(self.restore)

    def restore(self):
        pc.thread_of, pc.patch, pc.post = self.saved

    def run_plan(self, thread, patch_ok=True, post_ok=True, mode="label",
                 head="abcdef1234"):
        import json, os, sys
        d = self.tmp.name
        open(os.path.join(d, "b.md"), "w").write(NEW_BODY)
        plan = {"mode": mode, "accounts": ACCOUNTS,
                "comments": [{"key": "1", "repo": "o/r", "number": 1,
                              "body_file": "b.md", **({"head": head} if head else {})}]}
        p = os.path.join(d, "plan.json")
        json.dump(plan, open(p, "w"))
        pc.thread_of = lambda *a: thread
        pc.patch = lambda repo, cid, body: (self.calls.append(("patch", cid)), patch_ok)[1]
        pc.post = lambda repo, num, body: (self.calls.append(("post", num)), post_ok)[1]
        argv = sys.argv
        sys.argv = ["post-comments.py", p]
        try:
            return pc.main()
        finally:
            sys.argv = argv

    def test_the_new_comment_is_posted_before_the_old_is_deprecated(self):
        # the other order leaves "Deprecated - see below" with nothing below it
        thread = [c(BOT, "2026-09-02T00:00:00Z", AI, cid=7),
                  c(HUMAN, "2026-09-03T00:00:00Z", "ping")]
        self.run_plan(thread)
        self.assertEqual([k for k, _ in self.calls], ["post", "patch"])

    def test_a_failed_post_leaves_the_old_comment_alone(self):
        thread = [c(BOT, "2026-09-02T00:00:00Z", AI, cid=7),
                  c(HUMAN, "2026-09-03T00:00:00Z", "ping")]
        rc = self.run_plan(thread, post_ok=False)
        self.assertEqual([k for k, _ in self.calls], ["post"])
        self.assertEqual(rc, 1)

    def test_an_edit_we_may_not_make_falls_back_to_posting(self):
        # the newest AI comment belongs to the account we replaced: the PATCH is
        # rejected, and without a fallback the author gets nothing at all
        thread = [c(OLD, "2026-09-02T00:00:00Z", AI, cid=7)]
        rc = self.run_plan(thread, patch_ok=False, head="abcdef1234")
        kinds = [k for k, _ in self.calls]
        self.assertEqual(kinds[0], "patch")      # tried to edit
        self.assertIn("post", kinds)             # then posted instead
        self.assertEqual(rc, 0)

    def test_a_thread_we_cannot_read_posts_nothing(self):
        # a failed fetch used to read as "never commented here", which posts a
        # duplicate and leaves the previous review standing
        import json, os, sys
        d = self.tmp.name
        open(os.path.join(d, "b.md"), "w").write(NEW_BODY)
        p = os.path.join(d, "plan2.json")
        json.dump({"accounts": ACCOUNTS,
                   "comments": [{"key": "1", "repo": "o/r", "number": 1,
                                 "head": "abcdef1234", "body_file": "b.md"}]}, open(p, "w"))

        def boom(*a):
            raise pc.GhError("comments: 502 Bad Gateway")

        pc.thread_of = boom
        pc.patch = lambda *a: self.calls.append(("patch", a)) or True
        pc.post = lambda *a: self.calls.append(("post", a)) or True
        argv = sys.argv
        sys.argv = ["post-comments.py", p]
        try:
            rc = pc.main()
        finally:
            sys.argv = argv
        self.assertEqual(self.calls, [], "wrote to GitHub despite a failed read")
        self.assertEqual(rc, 1)


class PlanWiring(unittest.TestCase):
    """main() must actually pass the plan's head and mode to decide().

    The fixes are pinned inside decide(); this pins the wiring that feeds it.
    Discarding both fields used to leave the whole suite green.
    """

    def setUp(self):
        self.seen = {}
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = (pc.thread_of, pc.decide, pc.patch, pc.post)
        self.addCleanup(lambda: setattr_all(pc, self.saved))

    def run_plan(self, entry, mode=None):
        import json, os, sys
        d = self.tmp.name
        open(os.path.join(d, "b.md"), "w").write(NEW_BODY)
        plan = {"accounts": ACCOUNTS, "comments": [dict(entry, body_file="b.md")]}
        if mode:
            plan["mode"] = mode
        p = os.path.join(d, "plan.json")
        json.dump(plan, open(p, "w"))
        pc.thread_of = lambda *a: [c(BOT, "2026-09-02T00:00:00Z", AI, cid=7)]

        def spy(thread, body, accounts, head=None, mode="label"):
            self.seen = {"head": head, "mode": mode}
            return "unchanged", 7

        pc.decide = spy
        pc.patch = pc.post = lambda *a, **k: True
        argv = sys.argv
        sys.argv = ["post-comments.py", p, "--dry-run"]
        try:
            pc.main()
        finally:
            sys.argv = argv
        return self.seen

    def test_the_head_from_the_plan_reaches_decide(self):
        seen = self.run_plan({"key": "1", "repo": "o/r", "number": 1,
                              "head": "0374a23d84"})
        self.assertEqual(seen["head"], "0374a23d84")

    def test_the_mode_from_the_plan_reaches_decide(self):
        seen = self.run_plan({"key": "1", "repo": "o/r", "number": 1,
                              "head": "abcdef1234"}, mode="followup")
        self.assertEqual(seen["mode"], "followup")

    def test_an_entry_with_no_head_is_refused_rather_than_silently_edited(self):
        # a forgotten "head" key used to fall back to the pre-fix behaviour
        import json, os, sys
        d = self.tmp.name
        open(os.path.join(d, "b.md"), "w").write(NEW_BODY)
        p = os.path.join(d, "nohead.json")
        json.dump({"accounts": ACCOUNTS,
                   "comments": [{"key": "1", "repo": "o/r", "number": 1,
                                 "body_file": "b.md"}]}, open(p, "w"))
        pc.thread_of = lambda *a: [c(BOT, "2026-09-02T00:00:00Z", AI, cid=7)]
        argv = sys.argv
        sys.argv = ["post-comments.py", p, "--dry-run"]
        try:
            rc = pc.main()
        finally:
            sys.argv = argv
        self.assertEqual(rc, 1)


def setattr_all(mod, saved):
    mod.thread_of, mod.decide, mod.patch, mod.post = saved


class Guards(unittest.TestCase):
    """The checks that decide whether we write to GitHub at all.

    Each of these was a mutation that left the suite green: disabling the marker
    refusal, disabling the hold, letting the config read fail open, and letting
    the write helper report success without calling gh.
    """

    def setUp(self):
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = (pc.thread_of, pc.decide, pc.patch, pc.post)
        self.calls = []
        self.addCleanup(lambda: setattr_all(pc, self.saved))
        pc.thread_of = lambda *a: []
        pc.patch = lambda *a: self.calls.append("patch") or True
        pc.post = lambda *a: self.calls.append("post") or True

    def run_plan(self, plan, body="x"):
        import json, os, sys
        d = self.tmp.name
        open(os.path.join(d, "b.md"), "w").write(body)
        p = os.path.join(d, "g.json")
        json.dump(plan, open(p, "w"))
        argv = sys.argv
        sys.argv = ["post-comments.py", p]
        try:
            return pc.main()
        finally:
            sys.argv = argv

    def test_a_body_without_the_marker_is_never_posted(self):
        rc = self.run_plan({"accounts": ACCOUNTS,
                            "comments": [{"key": "1", "repo": "o/r", "number": 1,
                                          "head": "abc1234", "body_file": "b.md"}]},
                           body="a review with no marker")
        self.assertEqual(self.calls, [], "posted an unmarked machine review")
        self.assertEqual(rc, 1)

    def test_a_held_repo_is_never_posted_to(self):
        rc = self.run_plan({"accounts": ACCOUNTS, "hold": ["o/r"],
                            "comments": [{"key": "1", "repo": "o/r", "number": 1,
                                          "head": "abc1234", "body_file": "b.md"}]},
                           body=NEW_BODY)
        self.assertEqual(self.calls, [], "posted to a repo whose comments are held")
        self.assertEqual(rc, 0)

    def test_the_hold_is_case_insensitive(self):
        rc = self.run_plan({"accounts": ACCOUNTS, "hold": ["MAVLink/MAVLink"],
                            "comments": [{"key": "1", "repo": "mavlink/mavlink",
                                          "number": 1, "head": "abc1234",
                                          "body_file": "b.md"}]}, body=NEW_BODY)
        self.assertEqual(self.calls, [])
        self.assertEqual(rc, 0)

    def test_an_unreadable_config_stops_the_run_rather_than_holding_nothing(self):
        import os
        os.environ["REVIEW_REPO_CONFIG"] = os.path.join(self.tmp.name, "nope.json")
        self.addCleanup(os.environ.pop, "REVIEW_REPO_CONFIG", None)
        with self.assertRaises(SystemExit):
            self.run_plan({"accounts": ACCOUNTS,
                           "comments": [{"key": "1", "repo": "o/r", "number": 1,
                                         "head": "abc1234", "body_file": "b.md"}]},
                          body=NEW_BODY)
        self.assertEqual(self.calls, [])

    def test_the_write_helper_actually_calls_gh(self):
        import subprocess as sp
        seen = {}
        real = pc.subprocess.run

        def spy(cmd, **kw):
            seen["cmd"] = cmd
            return sp.CompletedProcess(cmd, 0, "{}", "")

        pc.subprocess.run = spy
        real_post = self.saved[3]          # setUp stubbed pc.post; test the real one
        try:
            self.assertTrue(real_post("o/r", 1, "body"))
        finally:
            pc.subprocess.run = real
        self.assertEqual(seen["cmd"][:2], ["gh", "api"])
        self.assertIn("repos/o/r/issues/1/comments", seen["cmd"])

    def test_reads_are_paginated(self):
        import subprocess as sp
        seen = {}
        real = pc.subprocess.run

        def spy(cmd, **kw):
            seen["cmd"] = cmd
            return sp.CompletedProcess(cmd, 0, "[]", "")

        pc.subprocess.run = spy
        try:
            pc.gh_json("repos/o/r/issues/1/comments")
        finally:
            pc.subprocess.run = real
        self.assertIn("--paginate", seen["cmd"],
                      "a thread past page one would read as shorter than it is")


class StaleComments(unittest.TestCase):
    """An earlier run whose deprecation failed leaves a live comment behind."""

    def test_an_identical_body_still_tidies_a_stale_one(self):
        thread = [c(BOT, "2026-09-01T00:00:00Z", AI, cid=5),
                  c(BOT, "2026-09-02T00:00:00Z", AI, cid=7)]
        self.assertEqual(pc.decide(thread, AI, ACCOUNTS, head="abcdef1234"),
                         ("deprecate-stale", 7))

    def test_nothing_to_tidy_is_simply_unchanged(self):
        dep = "> **Deprecated** ...\n" + AI
        thread = [c(BOT, "2026-09-01T00:00:00Z", dep, cid=5),
                  c(BOT, "2026-09-02T00:00:00Z", AI, cid=7)]
        self.assertEqual(pc.decide(thread, AI, ACCOUNTS, head="abcdef1234"),
                         ("unchanged", 7))

    def test_a_legacy_comment_we_cannot_edit_is_not_a_run_failure(self):
        # it belongs to the account we replaced; the handover job collapses it
        saved = pc.patch
        pc.patch = lambda *a: False
        try:
            ok = pc.collapse_stale("o/r",
                                   [c(OLD, "2026-09-01T00:00:00Z", AI, cid=5)],
                                   ACCOUNTS, keep=None, what="o/r#1")
        finally:
            pc.patch = saved
        self.assertTrue(ok)

    def test_our_own_comment_failing_to_collapse_is_a_failure(self):
        saved = pc.patch
        pc.patch = lambda *a: False
        try:
            ok = pc.collapse_stale("o/r",
                                   [c(BOT, "2026-09-01T00:00:00Z", AI, cid=5)],
                                   ACCOUNTS, keep=None, what="o/r#1")
        finally:
            pc.patch = saved
        self.assertFalse(ok)


class Deprecation(unittest.TestCase):
    def test_wraps_the_original_and_dates_it(self):
        out = pc.deprecate_body("original text", "2026-09-10T12:00:00Z")
        self.assertTrue(out.startswith("> **Deprecated — see below"))
        self.assertIn("<summary>Previous review (2026-09-10)</summary>", out)
        self.assertIn("original text", out)
        self.assertTrue(out.rstrip().endswith("</details>"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
