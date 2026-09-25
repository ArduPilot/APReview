#!/usr/bin/env python3
"""Reading a verdict out of a review, and deciding what the board should show.

The verdict is prose written by a model, so these are built from the shapes that
actually appear on the 168 open labelled PRs surveyed on 2026-09-26, not from
shapes that seemed likely. Four of those were being read backwards before the
"moves from X to Y" case was handled, which is why each phrasing has its own
test rather than one test over a list.
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(os.path.dirname(HERE), "bin")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BIN, path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


V = load("apreview_verdict", "apreview_verdict.py")
PS = load("project_sync", "project-sync.py")

NOTE = "**Automated review note — AI-generated (Claude).** Please sanity-check.\n\n"


class Marker(unittest.TestCase):
    def test_the_marker_decides_when_it_is_there(self):
        body = NOTE + "<!-- apreview: verdict=ACCEPT head=abc123 -->\n\n**REQUEST CHANGES**"
        self.assertEqual(V.of_comment(body), (V.ACCEPT, "marker"))

    def test_the_marker_beats_prose_that_says_otherwise(self):
        # the whole point of it: prose is what we are trying to stop depending on
        body = NOTE + "## REQUEST CHANGES\n<!-- apreview: verdict=COMMENT -->"
        self.assertEqual(V.from_marker(body), V.COMMENT)

    def test_request_changes_is_written_with_an_underscore_in_the_marker(self):
        self.assertEqual(V.from_marker("<!-- apreview: verdict=REQUEST_CHANGES -->"),
                         V.REQUEST)

    def test_approve_in_a_marker_is_accept(self):
        self.assertEqual(V.from_marker("<!-- apreview: verdict=APPROVE -->"), V.ACCEPT)

    def test_a_marker_with_no_verdict_field_decides_nothing(self):
        self.assertIsNone(V.from_marker("<!-- apreview: head=abc123 -->"))

    def test_a_nonsense_verdict_in_a_marker_is_not_silently_a_comment(self):
        self.assertIsNone(V.from_marker("<!-- apreview: verdict=LGTM -->"))


class Prose(unittest.TestCase):
    """Every shape here was copied from a real comment."""

    def check(self, text, want, why=""):
        got, how = V.from_prose(text)
        self.assertEqual(got, want, "%s (matched %s)" % (why or text[:60], how))

    def test_verdict_colon_inside_bold(self):
        self.check("Reviewed at head `x`. **Verdict: COMMENT — no blockers.**", V.COMMENT)

    def test_verdict_colon_with_the_word_bolded(self):
        self.check("Verdict: **COMMENT** — all four previous BUGs are fixed.", V.COMMENT)

    def test_verdict_colon_plain(self):
        self.check("Verdict: REQUEST CHANGES", V.REQUEST)

    def test_bare_bold_verdict(self):
        self.check("**REQUEST CHANGES** — the protocol work is clean, but two CI jobs",
                   V.REQUEST)

    def test_bold_that_closes_after_the_word(self):
        self.check("Reviewed at head `x`. **APPROVE — no blockers.** The refactor is",
                   V.ACCEPT)

    def test_a_heading(self):
        self.check("Full report: http://x\n\n## COMMENT — one real gap, three small ones",
                   V.COMMENT)

    def test_verdict_stays(self):
        self.check("I checked its scope rather than taking it on trust. Verdict stays "
                   "APPROVE.** One new finding", V.ACCEPT)

    def test_verdict_now(self):
        self.check("Re-reviewed at head `ea11b89353`. **Verdict now APPROVE** — every "
                   "finding from the previous round", V.ACCEPT)

    # --- the ones that were read backwards --------------------------------------
    def test_a_verdict_that_moved_is_the_one_it_moved_to(self):
        self.check("**The blocking finding is fixed, so the verdict moves from "
                   "REQUEST CHANGES to COMMENT.**", V.COMMENT,
                   "must be COMMENT, not the REQUEST CHANGES it moved from")

    def test_a_verdict_that_moved_with_the_target_bolded(self):
        self.check("Verdict moves from REQUEST CHANGES to **COMMENT**.", V.COMMENT)

    def test_a_verdict_that_moved_with_no_from_clause(self):
        self.check("**No blockers. The verdict moves to APPROVE** (it was COMMENT).",
                   V.ACCEPT)

    def test_listing_all_three_verdicts_is_not_a_verdict(self):
        # "the three passes disagreed on the overall verdict - APPROVE, COMMENT
        # and REQUEST CHANGES" preceded the real one, and won
        self.check("The three passes disagreed on the overall verdict — APPROVE, "
                   "COMMENT and REQUEST CHANGES — and the tie was broken by re-running."
                   "\n\n**REQUEST CHANGES** — real blockers remain.", V.REQUEST)

    # --- what must never match ---------------------------------------------------
    def test_the_words_in_ordinary_prose_decide_nothing(self):
        for text in ("I would accept this once the tests land.",
                     "Please comment on the approach before merging.",
                     "A reviewer might request changes here, but I would not."):
            with self.subTest(text=text):
                self.assertEqual(V.from_prose(text), (None, None))

    def test_a_draft_with_no_verdict_yields_nothing(self):
        self.assertEqual(V.from_prose(
            NOTE + "Reviewed at head `x`. This is a **draft**, so treat the below as "
                   "guidance on work in progress rather than a merge gate."), (None, None))


class Thread(unittest.TestCase):
    def test_the_newest_comment_that_states_one_wins(self):
        self.assertEqual(V.of_thread(["**Verdict: COMMENT**", "**Verdict: ACCEPT**"])[0],
                         V.ACCEPT)

    def test_a_followup_with_no_verdict_does_not_erase_the_review(self):
        # it exists to say the code moved; reading it as "no review" would drop
        # a reviewed PR off the board entirely
        thread = ["**Verdict: REQUEST CHANGES** — two blockers.",
                  NOTE + "Re-reviewed at head `abc`; my earlier comment is superseded."]
        self.assertEqual(V.of_thread(thread)[0], V.REQUEST)

    def test_a_superseded_comment_does_not_outrank_a_newer_followup(self):
        # deprecated bodies are skipped, so the verdict comes from the live
        # review, not from the copy folded into the deprecation
        thread = ["> **Deprecated — see below.**\n<details>**Verdict: ACCEPT**</details>",
                  "**Verdict: REQUEST CHANGES** — two blockers.",
                  NOTE + "Re-reviewed at head `abc`; earlier comment superseded."]
        self.assertEqual(V.of_thread(thread)[0], V.REQUEST)

    def test_a_thread_of_only_deprecated_comments_has_no_verdict(self):
        self.assertEqual(V.of_thread(["> **Deprecated**\n**Verdict: ACCEPT**"]),
                         (None, None))

    def test_no_comments_at_all(self):
        self.assertEqual(V.of_thread([]), (None, None))


class Plan(unittest.TestCase):
    """What the board should change, decided without touching a board."""

    def want(self, **kw):
        return {k: {"verdict": v, "id": "n-" + k} for k, v in kw.items()}

    def have(self, **kw):
        return {k: {"item": "i-" + k, "result": v} for k, v in kw.items()}

    def test_a_reviewed_pr_not_on_the_board_is_added(self):
        add, upd, rm = PS.plan(self.want(a=V.ACCEPT), {})
        self.assertEqual((add, upd, rm), (["a"], [], []))

    def test_a_pr_no_longer_eligible_is_removed(self):
        # merged, closed, or the label taken off: all it means here is that the
        # search no longer returns it
        add, upd, rm = PS.plan({}, self.have(a=V.ACCEPT))
        self.assertEqual((add, upd, rm), ([], [], ["a"]))

    def test_a_changed_verdict_is_relabelled_not_re_added(self):
        add, upd, rm = PS.plan(self.want(a=V.REQUEST), self.have(a=V.ACCEPT))
        self.assertEqual((add, upd, rm), ([], ["a"], []))

    def test_an_unchanged_row_is_left_alone(self):
        # otherwise every run rewrites all 166 rows, and the board's own history
        # becomes useless
        self.assertEqual(PS.plan(self.want(a=V.ACCEPT), self.have(a=V.ACCEPT)),
                         ([], [], []))

    def test_an_item_with_no_result_yet_is_relabelled(self):
        add, upd, rm = PS.plan(self.want(a=V.ACCEPT), self.have(a=None))
        self.assertEqual(upd, ["a"])


class PruneGuard(unittest.TestCase):
    """A sweep that comes back short must not empty the board."""

    def board(self, n):
        return {"k%d" % i: {"item": "i", "result": "ACCEPT"} for i in range(n)}

    def test_an_empty_search_against_a_full_board_is_refused(self):
        b = self.board(166)
        self.assertTrue(PS.too_much_to_remove(list(b), b))

    def test_ordinary_churn_is_allowed(self):
        b = self.board(166)
        self.assertFalse(PS.too_much_to_remove(list(b)[:20], b))

    def test_the_limit_scales_with_the_board_not_just_a_flat_count(self):
        # 30 of 166 is ordinary; 30 of 40 is not
        big, small = self.board(166), self.board(40)
        self.assertFalse(PS.too_much_to_remove(list(big)[:30], big))
        self.assertTrue(PS.too_much_to_remove(list(small)[:30], small))

    def test_a_small_board_is_not_held_hostage_by_the_percentage(self):
        # 4 of 8 is half the board, but four PRs merging is an ordinary morning
        b = self.board(8)
        self.assertFalse(PS.too_much_to_remove(list(b)[:4], b))

    def test_filling_an_empty_board_is_not_a_removal(self):
        self.assertFalse(PS.too_much_to_remove([], {}))

    def test_removing_nothing_is_never_too_much(self):
        self.assertFalse(PS.too_much_to_remove([], self.board(166)))


class OurComments(unittest.TestCase):
    """Which comments count as a review of ours."""

    def nodes(self, *pairs):
        return [{"author": {"login": a}, "body": b} for a, b in pairs]

    def test_a_comment_from_us_with_the_marker_counts(self):
        got = PS.our_comments(self.nodes(("AP-Review", NOTE + "x")), ("AP-Review",))
        self.assertEqual(len(got), 1)

    def test_a_human_comment_from_the_same_account_does_not(self):
        # the older reviews are authored by a person who also writes ordinary
        # comments, and this one names a verdict in passing
        got = PS.our_comments(
            self.nodes(("tridge", "I would REQUEST CHANGES on this, personally.")),
            ("AP-Review", "tridge"))
        self.assertEqual(got, [])

    def test_a_marked_comment_from_someone_else_does_not(self):
        # another bot, or a person quoting our review back at us
        got = PS.our_comments(self.nodes(("someone", NOTE + "**Verdict: ACCEPT**")),
                              ("AP-Review",))
        self.assertEqual(got, [])

    def test_a_comment_with_no_author_is_skipped(self):
        # a deleted account comes back as null, and indexing it would crash the
        # whole sweep rather than lose one row
        got = PS.our_comments([{"author": None, "body": NOTE + "x"}], ("AP-Review",))
        self.assertEqual(got, [])

    def test_order_is_preserved_so_the_thread_reads_oldest_first(self):
        got = PS.our_comments(self.nodes(("AP-Review", NOTE + "one"),
                                         ("AP-Review", NOTE + "two")), ("AP-Review",))
        self.assertEqual([g[-3:] for g in got], ["one", "two"])


class Sweep(unittest.TestCase):
    """main() end to end against a stubbed GitHub.

    The helpers above are pure and easy to get right; what they are wired into
    is what actually removes rows, so these drive the whole decision path.
    """

    def setUp(self):
        self.calls = []
        self.board = {}          # key -> verdict currently shown
        self.real_gh = PS.gh
        self.addCleanup(setattr, PS, "gh", self.real_gh)
        PS.gh = self.fake_gh
        self.found = {}          # key -> verdict the comments say
        self.note = False        # whether the board also holds a free-text note

    def fake_gh(self, query, **v):
        self.calls.append((query, v))
        if "search(" in query:
            nodes = []
            for key, verdict in self.found.items():
                repo, _, num = key.partition("#")
                if not repo.startswith(v["q"].split("org:")[1].split()[0]):
                    continue
                nodes.append({
                    "id": "node-" + key, "number": int(num), "title": "t",
                    "url": "u", "state": "OPEN", "isDraft": False,
                    "repository": {"nameWithOwner": repo},
                    "comments": {"nodes": [{"author": {"login": "AP-Review"},
                                            "body": NOTE + "**Verdict: %s**" % verdict}]}})
            return {"search": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                               "nodes": nodes}}
        if "projectsV2(first: 100)" in query:
            return {"organization": {"id": "org1", "projectsV2": {"nodes": [
                {"id": "proj1", "number": 33, "title": PS.TITLE, "url": "purl"}]}}}
        if "fields(first: 50)" in query:
            return {"node": {"fields": {"nodes": [{"id": "f1", "name": PS.FIELD,
                    "options": [{"id": "o-" + n, "name": n} for n, _, _ in PS.OPTIONS]}]}}}
        if "items(first: 100" in query:
            # a project can hold free-text notes as well as PRs; they have no
            # content, and reaching for their repository would abort the sweep
            nodes = [{"id": "item-note", "content": {},
                      "fieldValues": {"nodes": []}}] if self.note else []
            for key, verdict in self.board.items():
                repo, _, num = key.partition("#")
                nodes.append({"id": "item-" + key,
                              "content": {"number": int(num), "state": "OPEN",
                                          "repository": {"nameWithOwner": repo}},
                              "fieldValues": {"nodes": [
                                  {"name": verdict, "field": {"name": PS.FIELD}}]}})
            return {"node": {"items": {"pageInfo": {"hasNextPage": False,
                                                    "endCursor": None}, "nodes": nodes}}}
        if "deleteProjectV2Item" in query:
            return {"deleteProjectV2Item": {"deletedItemId": v["item"]}}
        if "addProjectV2ItemById" in query:
            return {"addProjectV2ItemById": {"item": {"id": "new-item"}}}
        if "updateProjectV2ItemFieldValue" in query:
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": v["item"]}}}
        raise AssertionError("unstubbed query: " + query[:60])

    def deletes(self):
        return [v["item"] for q, v in self.calls if "deleteProjectV2Item" in q]

    def run_main(self, *args):
        return PS.main(list(args))

    def test_a_merged_pr_is_taken_off_the_board(self):
        # the whole reason for the cron job: a merged PR tells us nothing, it
        # just stops coming back from the search
        self.board = {"ArduPilot/ardupilot#1": "ACCEPT"}
        self.found = {}
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.deletes(), ["item-ArduPilot/ardupilot#1"])

    def test_an_empty_search_does_not_empty_the_board(self):
        # a short or failed sweep looks identical to "everything merged"
        self.board = {"ArduPilot/ardupilot#%d" % i: "ACCEPT" for i in range(60)}
        self.found = {}
        self.assertEqual(self.run_main(), 3)
        self.assertEqual(self.deletes(), [], "it removed rows anyway")

    def test_force_prune_gets_past_the_guard(self):
        self.board = {"ArduPilot/ardupilot#%d" % i: "ACCEPT" for i in range(60)}
        self.found = {}
        self.assertEqual(self.run_main("--force-prune"), 0)
        self.assertEqual(len(self.deletes()), 60)

    def test_a_dry_run_changes_nothing(self):
        self.board = {"ArduPilot/ardupilot#1": "ACCEPT"}
        self.found = {"ArduPilot/ardupilot#2": "COMMENT"}
        self.assertEqual(self.run_main("--dry-run"), 0)
        self.assertEqual(self.deletes(), [])
        self.assertFalse([q for q, _ in self.calls if "addProjectV2ItemById" in q])

    def test_prune_only_removes_without_adding(self):
        self.board = {"ArduPilot/ardupilot#1": "ACCEPT"}
        self.found = {"ArduPilot/ardupilot#2": "COMMENT"}
        self.assertEqual(self.run_main("--prune-only"), 0)
        self.assertEqual(self.deletes(), ["item-ArduPilot/ardupilot#1"])
        self.assertFalse([q for q, _ in self.calls if "addProjectV2ItemById" in q])

    def test_a_free_text_note_on_the_board_is_left_alone(self):
        # someone may pin a note to the project; it is not a PR, it cannot be
        # matched against the search, and it must not be removed or crashed on
        self.note = True
        self.board = {"ArduPilot/ardupilot#1": "ACCEPT"}
        self.found = {"ArduPilot/ardupilot#1": "ACCEPT"}
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.deletes(), [])

    def test_a_new_review_is_added_with_its_verdict(self):
        self.found = {"ArduPilot/ardupilot#7": "REQUEST CHANGES"}
        self.assertEqual(self.run_main(), 0)
        sets = [v for q, v in self.calls if "updateProjectV2ItemFieldValue" in q]
        self.assertEqual(len(sets), 1)
        self.assertEqual(sets[0]["option"], "o-REQUEST CHANGES")


class Owners(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)

    def repos(self, obj):
        p = os.path.join(self.d, "repos.json")
        with open(p, "w") as f:
            json.dump(obj, f)
        return p

    def test_each_owner_once_in_the_order_first_seen(self):
        p = self.repos([{"repo": "ArduPilot/ardupilot"}, {"repo": "mavlink/mavlink"},
                        {"repo": "ArduPilot/MAVProxy"}])
        self.assertEqual(PS.swept_owners(p), ["ArduPilot", "mavlink"])

    def test_a_plain_list_of_names_works_too(self):
        self.assertEqual(PS.swept_owners(self.repos(["A/one", "B/two"])), ["A", "B"])

    def test_the_real_repos_file_names_the_orgs_we_sweep(self):
        # a search is per-org, so an org missing here is a repo silently never
        # put on the board
        owners = PS.swept_owners()
        self.assertIn("ArduPilot", owners)
        self.assertTrue(all(owners.count(o) == 1 for o in owners), owners)


if __name__ == "__main__":
    unittest.main(verbosity=2)
