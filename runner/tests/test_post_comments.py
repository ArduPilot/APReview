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

    def test_ours_is_still_the_last_word_so_edit_in_place(self):
        thread = [c(HUMAN, "2026-09-01T00:00:00Z", "hi"),
                  c(BOT, "2026-09-02T00:00:00Z", AI, cid=7)]
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


class Deprecation(unittest.TestCase):
    def test_wraps_the_original_and_dates_it(self):
        out = pc.deprecate_body("original text", "2026-09-10T12:00:00Z")
        self.assertTrue(out.startswith("> **Deprecated — see below"))
        self.assertIn("<summary>Previous review (2026-09-10)</summary>", out)
        self.assertIn("original text", out)
        self.assertTrue(out.rstrip().endswith("</details>"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
