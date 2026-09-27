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
import re
import shutil
import subprocess
import tempfile
import time
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


class Line(unittest.TestCase):
    """The verdict is line 2 of the comment. That is the whole rule."""

    def test_the_line_under_the_marker_is_the_verdict(self):
        self.assertEqual(V.of_comment(NOTE.rstrip("\n") + "\n**Verdict: REQUEST CHANGES**\n\nReviewed"),
                         (V.REQUEST, "line"))

    def test_every_written_form_reads_the_same(self):
        for line in ("**Verdict: ACCEPT**", "Verdict: ACCEPT", "**Verdict:** ACCEPT",
                     "**Verdict: APPROVE.**", "Verdict: REQUEST_CHANGES",
                     "**Verdict: REQUEST CHANGES.**", "  Verdict: COMMENT  "):
            with self.subTest(line=line):
                self.assertTrue(V.from_line("marker\n" + line + "\nmore"), line)

    def test_a_verdict_word_must_be_the_whole_of_the_line(self):
        # "Verdict: acceptable once fixed" and "Verdict: COMMENT - two notes"
        # are prose, and prose is exactly what this replaced
        for line in ("Verdict: acceptable once the crash is fixed.",
                     "**Verdict: COMMENT — no blockers.**",
                     "Verdict: COMMENT, downgraded from ACCEPT",
                     "The verdict: COMMENT"):
            with self.subTest(line=line):
                self.assertIsNone(V.from_line("marker\n" + line + "\nmore"), line)

    def test_the_line_has_to_be_near_the_top(self):
        # a "Verdict:" deep in the body is somebody discussing one
        deep = "marker\n" + "\n" * (V.HEAD_LINES + 2) + "**Verdict: ACCEPT**"
        self.assertIsNone(V.from_line(deep))
        self.assertEqual(V.from_line("marker\n\n<!-- x -->\n**Verdict: ACCEPT**"), V.ACCEPT)

    def test_the_line_beats_the_old_marker(self):
        body = NOTE.rstrip("\n") + "\n**Verdict: COMMENT**\n<!-- apreview: verdict=ACCEPT -->\n"
        self.assertEqual(V.of_comment(body), (V.COMMENT, "line"))

    def test_the_old_marker_is_still_read_when_there_is_no_line(self):
        self.assertEqual(V.of_comment(NOTE + "<!-- apreview: verdict=REQUEST_CHANGES head=a1 -->"),
                         (V.REQUEST, "marker"))
        self.assertEqual(V.from_marker("<!-- apreview: verdict=APPROVE -->"), V.ACCEPT)
        self.assertIsNone(V.from_marker("<!-- apreview: head=abc123 -->"))
        self.assertIsNone(V.from_marker("<!-- apreview: verdict=LGTM -->"))

    def test_a_quoted_or_indented_marker_is_not_ours(self):
        # the marker counts only at the start of a line; somebody quoting it
        # or showing it as an example does not restate our verdict
        self.assertIsNone(V.from_marker("> <!-- apreview: verdict=ACCEPT -->"))
        self.assertIsNone(V.from_marker("    <!-- apreview: verdict=ACCEPT -->"))

    def test_prose_decides_nothing(self):
        # the shapes the retired parser used to read; a comment written in
        # them now needs verdict-backfill.py, and says so by being unknown
        for text in ("Reviewed at head `x`. **Verdict: COMMENT — no blockers.**",
                     "**REQUEST CHANGES** — the protocol work is clean",
                     "## COMMENT — one real gap",
                     "the verdict moves from REQUEST CHANGES to COMMENT."):
            with self.subTest(text=text):
                self.assertEqual(V.of_comment(NOTE + text), (None, None))


BF = load("verdict_backfill", "verdict-backfill.py")


class Backfill(unittest.TestCase):
    """The one-off that put the line on the comments written before it."""

    def test_the_line_goes_directly_under_the_marker_line(self):
        body = "**Automated review note — AI-generated.** Check.\n\nFull report: x\n"
        self.assertEqual(BF.with_verdict_line(body, V.COMMENT).split("\n")[:3],
                         ["**Automated review note — AI-generated.** Check.",
                          "**Verdict: COMMENT**", ""])

    def test_the_result_reads_back_as_that_verdict(self):
        body = NOTE + "**REQUEST CHANGES** — two blockers."
        self.assertEqual(V.of_comment(BF.with_verdict_line(body, V.REQUEST)), (V.REQUEST, "line"))

    def test_a_comment_that_already_has_the_line_is_left_alone(self):
        self.assertEqual(BF.decide(NOTE.rstrip("\n") + "\n**Verdict: ACCEPT**\n"),
                         (V.ACCEPT, "already"))

    def test_a_person_s_decision_beats_the_prose(self):
        self.assertEqual(BF.decide(NOTE + "**ACCEPT** — clean.", override=V.REQUEST),
                         (V.REQUEST, "by hand"))

    def test_the_old_marker_beats_the_prose(self):
        self.assertEqual(BF.decide(NOTE + "<!-- apreview: verdict=COMMENT -->\n**ACCEPT**"),
                         (V.COMMENT, "marker"))

    def test_the_prose_parser_still_reads_the_seven_shapes(self):
        for text, want in (("Reviewed at head `x`. **Verdict: COMMENT — no blockers.**", V.COMMENT),
                           ("Verdict: **COMMENT** — all four previous BUGs are fixed.", V.COMMENT),
                           ("**REQUEST CHANGES** — the protocol work is clean", V.REQUEST),
                           ("**APPROVE — no blockers.** The refactor is", V.ACCEPT),
                           ("Full report: x\n\n## COMMENT — one real gap", V.COMMENT),
                           ("Verdict stays APPROVE.** One new finding", V.ACCEPT),
                           ("**The blocking finding is fixed, so the verdict moves from "
                            "REQUEST CHANGES to COMMENT.**", V.COMMENT)):
            with self.subTest(text=text[:40]):
                self.assertEqual(BF.decide(NOTE + text)[0], want)

    def test_two_declared_verdicts_are_refused_for_a_person_to_read(self):
        self.assertEqual(BF.decide(NOTE + "Verdict: ACCEPT, downgraded to REQUEST CHANGES."),
                         (None, None))

    def test_only_the_newest_live_comment_of_ours_is_touched(self):
        def c(login, body):
            return {"databaseId": 1, "author": {"login": login}, "body": body}
        pr = {"comments": {"nodes": [
            c("AP-Review", NOTE + "old"),
            c("someone", NOTE + "not ours"),
            c("AP-Review", NOTE + "newest live"),
            c("AP-Review", "> **Deprecated — see below.**\n\n" + NOTE + "superseded"),
            c("tridge", "a human remark")]}}
        self.assertEqual(BF.newest_live(pr, ("AP-Review", "tridge"))["body"], NOTE + "newest live")
        self.assertIsNone(BF.newest_live({"comments": {"nodes": []}}, ("AP-Review",)))


class Thread(unittest.TestCase):
    def test_the_newest_comment_that_states_one_wins(self):
        self.assertEqual(V.of_thread(["**Verdict: COMMENT**", "**Verdict: ACCEPT**"])[0],
                         V.ACCEPT)

    def test_a_followup_with_no_verdict_does_not_erase_the_review(self):
        # it exists to say the code moved; reading it as "no review" would drop
        # a reviewed PR off the board entirely
        thread = [NOTE + "**Verdict: REQUEST CHANGES**\n\nTwo blockers.",
                  NOTE + "Re-reviewed at head `abc`; my earlier comment is superseded."]
        self.assertEqual(V.of_thread(thread)[0], V.REQUEST)

    def test_a_superseded_comment_does_not_outrank_a_newer_followup(self):
        # deprecated bodies are skipped, so the verdict comes from the live
        # review, not from the copy folded into the deprecation
        thread = ["> **Deprecated — see below for the updated review.**\n\n"
                  "**Verdict: ACCEPT**",
                  NOTE + "**Verdict: REQUEST CHANGES**\n\nTwo blockers.",
                  NOTE + "Re-reviewed at head `abc`; earlier comment superseded."]
        self.assertEqual(V.of_thread(thread)[0], V.REQUEST)

    def test_a_thread_of_only_deprecated_comments_has_no_verdict(self):
        # the shape post-comments.py writes: the notice, a blank line, then
        # the old body in full
        self.assertEqual(V.of_thread(["> **Deprecated — see below for the updated review.**"
                                      "\n\n**Verdict: ACCEPT**"]), (None, None))

    def test_no_comments_at_all(self):
        self.assertEqual(V.of_thread([]), (None, None))


class Plan(unittest.TestCase):
    """What the board should change, decided without touching a board."""

    def want(self, **kw):
        return {k: {"verdict": v, "id": "n-" + k, "author": "someone"}
                for k, v in kw.items()}

    def have(self, **kw):
        return {k: {"item": "i-" + k, "result": v, "author": "someone"}
                for k, v in kw.items()}

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

    def test_a_row_missing_only_its_author_is_still_rewritten(self):
        # adding the column to an existing board backfills nothing unless the
        # plan notices the author, not just the verdict
        present = {"a": {"item": "i-a", "result": V.ACCEPT, "author": None}}
        add, upd, rm = PS.plan(self.want(a=V.ACCEPT), present)
        self.assertEqual(upd, ["a"])


class PruneGuard(unittest.TestCase):
    """A sweep that comes back short must not empty the board."""

    def board(self, n):
        return {"k%d" % i: {"item": "i", "result": "ACCEPT", "author": "x"}
                for i in range(n)}

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
        self.shown = ["f1", "fa"]   # field ids the view currently displays
        self.pr_state = {}       # key -> state, for a board row the search missed
        self.pr_comments = {}    # key -> that PR's comment bodies, oldest first
        self.view_readable = True
        self.search_pages = 1    # how many pages the search answers in
        self.item_pages = 1
        self.older = {}          # key -> an older comment page, for pagination
        self.thread = {}         # key -> every comment body, oldest first
        self.author_field = True       # whether the PR Author field exists yet
        self.result_field = True       # whether the Result field exists yet
        self.searched = []       # every search query string seen
        self.real_gh_list = PS.gh_list
        self.addCleanup(setattr, PS, "gh_list", self.real_gh_list)
        PS.gh_list = self.fake_gh_list
        self.view_sets = []

    def fake_gh_list(self, query, strings, lists):
        self.view_sets.append(lists.get("fields"))
        return {"updateProjectV2View": {"projectV2View": {"id": "view1",
                                                          "name": "View 1"}}}

    def fake_gh(self, query, **v):
        self.calls.append((query, v))
        if "views(first: 20)" in query:
            return {"node": {"views": {"nodes": [{"id": "view1", "number": 1,
                                                  "name": "View 1"}]}}}
        if "ProjectV2View" in query and "fields(first: 50)" in query:
            if not self.view_readable:
                raise PS.GhError("502 while reading the view")
            return {"node": {"fields": {"nodes": [{"id": f} for f in self.shown]}}}
        if "node(id: $id)" in query and "comments(last: 100)" in query:
            # a board row's PR, by node id; a name would not be found here
            key = v["id"][len("pr-"):] if v["id"].startswith("pr-") else None
            state = self.pr_state.get(key, "MERGED")
            if isinstance(state, tuple):
                state = state[0]
            if state == "GONE" or key is None:
                return {"node": None}
            bodies = self.pr_comments.get(key)
            if bodies is None:
                bodies = ([NOTE + "**Verdict: %s**" % self.found[key]]
                          if key in self.found else [])
            repo, _, num = key.partition("#")
            return {"node": {
                "id": "node-" + key, "number": int(num), "title": "t", "url": "u",
                "state": state, "author": {"login": "someone"},
                "repository": {"nameWithOwner": repo},
                "comments": {"pageInfo": {"hasPreviousPage": False, "startCursor": None},
                             "nodes": [{"author": {"login": "AP-Review"}, "body": b}
                                       for b in bodies]}}}
        if "before:" in query:
            key = v["id"][len("node-"):]
            self.older_asked = (v["id"], v["before"])
            return {"node": {"comments": self.older.get(key)}}
        if "search(" in query:
            self.searched.append(v["q"])
            org = v["q"].split("org:")[1].split()[0]
            mine = [(k, x) for k, x in self.found.items()
                    if k.partition("#")[0].startswith(org)]
            page = int(v.get("after") or 0)
            per = max(1, -(-len(mine) // self.search_pages)) if mine else 1
            chunk = mine[page * per:(page + 1) * per]
            nodes = []
            which, n = re.search(r"comments\((last|first): (\d+)\)", query).groups()
            for key, verdict in chunk:
                repo, _, num = key.partition("#")
                bodies = self.thread.get(key)
                if bodies is None:
                    bodies = [] if key in self.older else [
                        NOTE + "**Verdict: %s**" % verdict]
                # serve the window the query asked for, the way GitHub would
                window = bodies[-int(n):] if which == "last" else bodies[:int(n)]
                nodes.append({
                    "id": "node-" + key, "number": int(num), "title": "t",
                    "url": "u", "state": "OPEN", "isDraft": False,
                    "author": {"login": "someone"},
                    "repository": {"nameWithOwner": repo},
                    "comments": {
                        "pageInfo": {"hasPreviousPage": key in self.older,
                                     "startCursor": "cur-" + key},
                        "nodes": [{"author": {"login": "AP-Review"}, "body": b}
                                  for b in window]}})
            more = (page + 1) * per < len(mine)
            return {"search": {"pageInfo": {"hasNextPage": more,
                                            "endCursor": str(page + 1)},
                               "nodes": nodes}}
        if "projectsV2(first: 100)" in query:
            return {"organization": {"id": "org1", "projectsV2": {"nodes": [
                {"id": "proj1", "number": 33, "title": PS.TITLE, "url": "purl"}]}}}
        if "fields(first: 50)" in query and "ProjectV2View" not in query:
            fields = []
            if self.result_field:
                fields.append({"id": "f1", "name": PS.FIELD,
                               "options": [{"id": "o-" + n, "name": n} for n, _, _ in PS.OPTIONS]})
            if self.author_field:
                fields.append({"id": "fa", "name": PS.AUTHOR})
            return {"node": {"fields": {"nodes": fields}}}
        if "items(first: 100" in query:
            # a project can hold free-text notes as well as PRs; they have no
            # content, and reaching for their repository would abort the sweep
            nodes = [{"id": "item-note", "content": {},
                      "fieldValues": {"nodes": []}}] if self.note else []
            for key, verdict in self.board.items():
                repo, _, num = key.partition("#")
                content = {"number": int(num), "state": "OPEN",
                           "repository": {"nameWithOwner": repo}}
                if "PullRequest { id " in query:      # only what was asked for
                    content["id"] = "pr-" + key
                nodes.append({"id": "item-" + key, "content": content,
                              "fieldValues": {"nodes": [
                                  {"name": verdict, "field": {"name": PS.FIELD}},
                                  {"text": "someone", "field": {"name": PS.AUTHOR}}]}})
            page = int(v.get("after") or 0)
            per = max(1, -(-len(nodes) // self.item_pages)) if nodes else 1
            chunk = nodes[page * per:(page + 1) * per]
            more = (page + 1) * per < len(nodes)
            return {"node": {"items": {"pageInfo": {"hasNextPage": more,
                                                    "endCursor": str(page + 1)},
                                       "nodes": chunk}}}
        if "deleteProjectV2Item" in query:
            return {"deleteProjectV2Item": {"deletedItemId": v["item"]}}
        if "addProjectV2ItemById" in query:
            return {"addProjectV2ItemById": {"item": {"id": "new-item"}}}
        if "updateProjectV2ItemFieldValue" in query:
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": v["item"]}}}
        if "createProjectV2Field" in query:
            if v.get("name") == PS.FIELD:
                return {"createProjectV2Field": {"projectV2Field": {
                    "id": "f1", "name": PS.FIELD,
                    "options": [{"id": "o-" + n, "name": n} for n, _, _ in PS.OPTIONS]}}}
            return {"createProjectV2Field": {"projectV2Field":
                    {"id": "fa", "name": PS.AUTHOR}}}
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

    def test_a_view_that_does_not_show_the_result_is_made_to(self):
        # the field existed and every row had a value, and the board showed
        # none of it: a new field is not added to views that already exist
        self.shown = ["other-field"]
        self.found = {"ArduPilot/ardupilot#1": "ACCEPT"}
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.view_sets), 1, "it never touched the view")
        self.assertIn("f1", self.view_sets[0], "Result was not made visible")

    def test_a_view_already_showing_it_is_left_alone(self):
        # someone may have arranged their own columns; do not fight them
        self.shown = ["f1", "fa", "something-they-added"]
        self.found = {"ArduPilot/ardupilot#1": "ACCEPT"}
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.view_sets, [])

    def test_a_dry_run_does_not_change_the_view(self):
        self.shown = ["other-field"]
        self.found = {"ArduPilot/ardupilot#1": "ACCEPT"}
        self.assertEqual(self.run_main("--dry-run"), 0)
        self.assertEqual(self.view_sets, [])

    # --- a row stays while its PR is open; only closing removes it -----------
    def test_a_row_the_search_missed_but_the_pr_still_wants_is_kept(self):
        """Search is capped at 1000, lags its index, and can answer short.

        Absence is a question; the PR itself is the answer.
        """
        self.board = {"ArduPilot/ardupilot#1": "ACCEPT"}
        self.found = {}                       # the search lost it
        self.pr_state = {"ArduPilot/ardupilot#1": "OPEN"}
        self.pr_comments = {"ArduPilot/ardupilot#1": [NOTE + "**Verdict: ACCEPT**"]}
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.deletes(), [], "it deleted a PR that is still open")

    def test_a_row_whose_pr_will_not_answer_is_kept(self):
        self.board = {"ArduPilot/ardupilot#1": "ACCEPT"}
        self.found = {}
        self.pr_state = {"ArduPilot/ardupilot#1": "GONE"}
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.deletes(), [])

    def test_a_merged_pr_is_removed_whatever_labels_it_kept(self):
        # the label stays on a PR after it merges, so the state is what decides
        self.board = {"ArduPilot/ardupilot#1": "ACCEPT"}
        self.found = {}
        self.pr_state = {"ArduPilot/ardupilot#1": "MERGED"}
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.deletes(), ["item-ArduPilot/ardupilot#1"])

    def test_a_pr_that_lost_its_label_stays_while_it_is_open(self):
        # DevCallEU and DevCallTopic come off after the call; the PR is still
        # open, still reviewed, and followups still land on it
        self.board = {"ArduPilot/ardupilot#1": "ACCEPT"}
        self.found = {}                       # no trigger label, so not searched
        self.pr_state = {"ArduPilot/ardupilot#1": "OPEN"}
        self.pr_comments = {"ArduPilot/ardupilot#1": [NOTE + "**Verdict: ACCEPT**"]}
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.deletes(), [])
        self.assertFalse([q for q, _ in self.calls if "updateProjectV2ItemFieldValue" in q])

    def test_a_delabelled_pr_still_gets_its_new_verdict(self):
        # a followup re-reviewed it after the label came off; the board must
        # show what that review said, not what the last labelled run said
        self.board = {"ArduPilot/ardupilot#1": "ACCEPT"}
        self.found = {}
        self.pr_state = {"ArduPilot/ardupilot#1": "OPEN"}
        self.pr_comments = {"ArduPilot/ardupilot#1": [
            NOTE + "**Verdict: ACCEPT**", NOTE + "**Verdict: REQUEST CHANGES**"]}
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.deletes(), [])
        sets = [v for q, v in self.calls if "updateProjectV2ItemFieldValue" in q]
        self.assertEqual([s.get("option") for s in sets if "option" in s],
                         ["o-REQUEST CHANGES"])

    def test_an_open_row_with_nothing_readable_is_left_alone(self):
        # the comment may be gone or unreadable; the PR is open, so the row
        # stays as it is rather than being removed or rewritten
        self.board = {"ArduPilot/ardupilot#1": "ACCEPT"}
        self.found = {}
        self.pr_state = {"ArduPilot/ardupilot#1": "OPEN"}
        self.pr_comments = {"ArduPilot/ardupilot#1": []}
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.deletes(), [])
        self.assertFalse([q for q, _ in self.calls if "updateProjectV2ItemFieldValue" in q])

    def test_a_row_is_asked_about_by_its_node_id(self):
        # a renamed or transferred repository whose old name is reused can put
        # a different, closed PR at the same owner/repo/number
        self.board = {"ArduPilot/ardupilot#1": "ACCEPT"}
        self.found = {}
        self.pr_state = {"ArduPilot/ardupilot#1": "OPEN"}
        self.run_main()
        asked = [v for q, v in self.calls if "node(id: $id)" in q and "state" in q]
        self.assertEqual([x.get("id") for x in asked], ["pr-ArduPilot/ardupilot#1"])
        self.assertFalse(any("number" in x or "owner" in x for x in asked))

    def test_an_empty_org_does_not_take_that_org_off_the_board(self):
        # one org answering empty looked exactly like every PR in it closing
        self.board = {"ArduPilot/ardupilot#1": "ACCEPT",
                      "RsyncProject/rsync#9": "COMMENT"}
        self.found = {"ArduPilot/ardupilot#1": "ACCEPT"}
        self.pr_state = {"RsyncProject/rsync#9": "OPEN"}
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.deletes(), [])

    # --- pagination -----------------------------------------------------------
    def test_every_page_of_the_search_is_read(self):
        self.found = {"ArduPilot/ardupilot#%d" % i: "COMMENT" for i in range(7)}
        self.search_pages = 3
        self.assertEqual(self.run_main(), 0)
        adds = [v for q, v in self.calls if "addProjectV2ItemById" in q]
        self.assertEqual(len(adds), 7, "stopped before the last page")

    def test_every_page_of_the_board_is_read(self):
        # a board read short looks like rows that are not there, and the rows it
        # did see get re-added
        self.board = {"ArduPilot/ardupilot#%d" % i: "COMMENT" for i in range(7)}
        self.found = {"ArduPilot/ardupilot#%d" % i: "COMMENT" for i in range(7)}
        self.item_pages = 3
        self.assertEqual(self.run_main(), 0)
        self.assertEqual([v for q, v in self.calls if "addProjectV2ItemById" in q], [])

    def test_the_verdict_is_found_on_an_older_comment_page(self):
        # a PR busy enough to push the review out of the newest hundred would
        # otherwise drop off the board entirely
        key = "ArduPilot/ardupilot#1"
        self.found = {key: "REQUEST CHANGES"}
        self.older[key] = {
            "pageInfo": {"hasPreviousPage": False, "startCursor": None},
            "nodes": [{"author": {"login": "AP-Review"},
                       "body": NOTE + "**Verdict: REQUEST CHANGES**"}]}
        self.assertEqual(self.run_main(), 0)
        sets = [v for q, v in self.calls if "updateProjectV2ItemFieldValue" in q]
        self.assertIn("o-REQUEST CHANGES", [x.get("option") for x in sets])
        # asked for by the PR's id and the page's own cursor, not from the top
        self.assertEqual(self.older_asked, ("node-" + key, "cur-" + key))

    def test_the_search_asks_for_the_newest_comments(self):
        # the verdict is the newest thing we said; the oldest hundred of a
        # busy PR would miss it, and there would be no "previous page" to walk
        key = "ArduPilot/ardupilot#1"
        self.found = {key: "COMMENT"}
        self.thread[key] = ["chatter %d" % i for i in range(120)] + [
            NOTE + "**Verdict: COMMENT**"]
        self.assertEqual(self.run_main(), 0)
        adds = [v for q, v in self.calls if "addProjectV2ItemById" in q]
        self.assertEqual(len(adds), 1, "the verdict was outside the window fetched")

    def test_the_comment_walk_is_bounded(self):
        # a PR with no verdict anywhere must not be walked to its first
        # comment from a run's exit path
        pages = []

        def endless(pr, cursor):
            pages.append(cursor)
            # a walk that is not bounded would otherwise hang this test
            if len(pages) > PS.MAX_COMMENT_PAGES + 5:
                self.fail("the walk did not stop")
            return {"pageInfo": {"hasPreviousPage": True,
                                 "startCursor": "cur-%d" % len(pages)},
                    "nodes": []}

        pr = {"id": "node-x", "comments": {
            "pageInfo": {"hasPreviousPage": True, "startCursor": "cur-0"},
            "nodes": []}}
        self.assertEqual(PS.verdict_of(pr, ("AP-Review",), endless), (None, None))
        self.assertEqual(len(pages), PS.MAX_COMMENT_PAGES)

    def test_the_last_page_fetched_is_still_read(self):
        # a page fetched and then thrown away is a wasted request at best and
        # a missed verdict at worst
        pages = []

        def older(pr, cursor):
            pages.append(cursor)
            last = len(pages) == PS.MAX_COMMENT_PAGES
            return {"pageInfo": {"hasPreviousPage": True,
                                 "startCursor": "cur-%d" % len(pages)},
                    "nodes": ([{"author": {"login": "AP-Review"},
                                "body": NOTE + "**Verdict: COMMENT**"}] if last else [])}

        pr = {"id": "node-x", "comments": {
            "pageInfo": {"hasPreviousPage": True, "startCursor": "cur-0"},
            "nodes": []}}
        self.assertEqual(PS.verdict_of(pr, ("AP-Review",), older)[0], V.COMMENT)

    def test_a_cursor_cycle_stops_the_walk(self):
        pages = []

        def cycle(pr, cursor):
            pages.append(cursor)
            if len(pages) > PS.MAX_COMMENT_PAGES + 5:
                self.fail("the walk did not stop")
            return {"pageInfo": {"hasPreviousPage": True,
                                 "startCursor": ["a", "b"][len(pages) % 2]},
                    "nodes": []}

        pr = {"id": "node-x", "comments": {
            "pageInfo": {"hasPreviousPage": True, "startCursor": "a"},
            "nodes": []}}
        self.assertEqual(PS.verdict_of(pr, ("AP-Review",), cycle), (None, None))
        self.assertLessEqual(len(pages), 3)

    def test_a_cursor_that_does_not_advance_stops_the_walk(self):
        pages = []

        def stuck(pr, cursor):
            pages.append(cursor)
            if len(pages) > PS.MAX_COMMENT_PAGES + 5:
                self.fail("the walk did not stop")
            return {"pageInfo": {"hasPreviousPage": True, "startCursor": "same"},
                    "nodes": []}

        pr = {"id": "node-x", "comments": {
            "pageInfo": {"hasPreviousPage": True, "startCursor": "same"},
            "nodes": []}}
        self.assertEqual(PS.verdict_of(pr, ("AP-Review",), stuck), (None, None))
        self.assertEqual(len(pages), 1)

    def test_a_search_that_repeats_its_cursor_is_an_error_not_a_hang(self):
        # each call answers inside its deadline, so only the walk can notice
        real = self.fake_gh
        asked = []

        def stuck(query, **v):
            if "search(" in query:
                asked.append(v.get("after"))
                if len(asked) > 30:
                    self.fail("the search walk did not stop")
                return {"search": {"nodes": [], "pageInfo": {
                    "hasNextPage": True, "endCursor": "same"}}}
            return real(query, **v)

        PS.gh = stuck
        with self.assertRaises(PS.GhError):
            self.run_main()
        self.assertEqual(self.deletes(), [])

    def test_a_board_read_that_never_ends_is_an_error_not_a_hang(self):
        # cursors that keep advancing past anything a real board could hold
        real = self.fake_gh
        asked = []

        def stuck(query, **v):
            if "items(first: 100" in query:
                asked.append(v.get("after"))
                if len(asked) > PS.MAX_PAGES + 10:
                    self.fail("the board walk did not stop")
                return {"node": {"items": {"nodes": [], "pageInfo": {
                    "hasNextPage": True, "endCursor": "c%d" % len(asked)}}}}
            return real(query, **v)

        PS.gh = stuck
        self.found = {"ArduPilot/ardupilot#1": "COMMENT"}
        with self.assertRaises(PS.GhError):
            self.run_main()

    def test_all_three_trigger_labels_are_searched(self):
        self.found = {"ArduPilot/ardupilot#1": "COMMENT"}
        self.run_main()
        for label in PS.LABELS:
            self.assertTrue(any(label in q for q in self.searched), label)

    # --- views ----------------------------------------------------------------
    def test_a_view_that_cannot_be_read_is_left_alone(self):
        # one 502 used to be read as permission to install the defaults, wiping
        # whatever columns somebody had arranged
        self.view_readable = False
        self.found = {"ArduPilot/ardupilot#1": "COMMENT"}
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.view_sets, [], "it rewrote a view it could not read")

    def test_adding_our_columns_keeps_the_ones_already_there(self):
        self.shown = ["f1", "someone-elses-column"]      # PR Author missing
        self.found = {"ArduPilot/ardupilot#1": "COMMENT"}
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.view_sets), 1)
        self.assertIn("someone-elses-column", self.view_sets[0])
        self.assertIn("fa", self.view_sets[0])

    def test_it_will_not_take_the_last_row_off_a_board(self):
        # three merged PRs is under every threshold, but a board going to zero
        # is what a broken sweep looks like and is worth a human glance
        self.board = {"ArduPilot/ardupilot#%d" % i: "ACCEPT" for i in range(3)}
        self.found = {}
        self.pr_state = {k: "MERGED" for k in self.board}
        self.assertEqual(self.run_main(), 3)
        self.assertEqual(self.deletes(), [])

    def test_a_refused_prune_really_changes_nothing(self):
        # the view was rewritten before the safety check, so "Nothing was
        # changed" was not true
        self.shown = ["f1"]                               # would want updating
        self.board = {"ArduPilot/ardupilot#%d" % i: "ACCEPT" for i in range(60)}
        self.found = {}
        self.pr_state = {k: "MERGED" for k in self.board}
        self.assertEqual(self.run_main(), 3)
        self.assertEqual(self.deletes(), [])
        self.assertEqual(self.view_sets, [], "it changed the view then refused")

    def test_a_refused_prune_creates_no_field_either(self):
        # the view was moved behind the safety check; the field creation was
        # not, so a refused sweep could still add a column while saying
        # nothing was changed
        self.author_field = False
        self.board = {"ArduPilot/ardupilot#%d" % i: "ACCEPT" for i in range(60)}
        self.found = {}
        self.pr_state = {k: "MERGED" for k in self.board}
        self.assertEqual(self.run_main(), 3)
        self.assertFalse([q for q, _ in self.calls if "createProjectV2Field" in q],
                         "it created a field then refused")

    def test_a_refused_prune_creates_no_result_field_either(self):
        self.result_field = False
        self.board = {"ArduPilot/ardupilot#%d" % i: "ACCEPT" for i in range(60)}
        self.found = {}
        self.pr_state = {k: "MERGED" for k in self.board}
        self.assertEqual(self.run_main(), 3)
        self.assertFalse([q for q, _ in self.calls if "createProjectV2Field" in q],
                         "it created the Result field then refused")

    def test_one_org_failing_applies_nothing_from_the_others(self):
        # half a picture is the dangerous kind: the orgs already gathered would
        # look complete, and every row of the failed org would be "missing"
        self.board = {"ArduPilot/ardupilot#1": "ACCEPT",
                      "RsyncProject/rsync#9": "COMMENT"}
        self.found = {"ArduPilot/ardupilot#1": "ACCEPT"}
        real = self.fake_gh

        def flaky(query, **v):
            if "search(" in query and "org:RsyncProject" in v.get("q", ""):
                raise PS.GhError("502 Bad Gateway")
            return real(query, **v)

        PS.gh = flaky
        with self.assertRaises(PS.GhError):
            self.run_main()
        self.assertEqual(self.deletes(), [])

    def test_a_free_text_note_never_reaches_the_plan(self):
        """It must be skipped when the board is read, not merely survive.

        The direct-verification layer would keep it anyway - a note has no PR
        to ask about - so asserting only that it is not deleted proves nothing
        about the skip itself.
        """
        self.note = True
        self.board = {"ArduPilot/ardupilot#1": "ACCEPT"}
        present = PS.project_items("proj1")
        self.assertEqual(sorted(present), ["ArduPilot/ardupilot#1"])

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
        self.assertEqual([s.get("option") for s in sets if "option" in s],
                         ["o-REQUEST CHANGES"])

    def test_a_new_row_records_who_opened_the_pr(self):
        self.found = {"ArduPilot/ardupilot#7": "COMMENT"}
        self.assertEqual(self.run_main(), 0)
        texts = [v["text"] for q, v in self.calls
                 if "updateProjectV2ItemFieldValue" in q and "text" in v]
        self.assertEqual(texts, ["someone"])


class ScopeMessage(unittest.TestCase):
    """The message the cron log shows when the token is not allowed to help."""

    def test_a_scope_failure_names_the_command_that_fixes_it(self):
        text = PS._explain(
            "gh: Your token has not been granted the required scopes to execute "
            "this query. The 'id' field requires one of the following scopes: "
            "['read:project']")
        self.assertIn("gh auth refresh -s project,read:project", text)

    def test_the_message_survives_the_path_a_scope_failure_actually_takes(self):
        """gh exits non-zero and prints it, so the errors list is never reached.

        Testing _explain alone proves nothing about what lands in the log.
        """
        import subprocess as sp
        real = PS.subprocess.run
        self.addCleanup(setattr, PS.subprocess, "run", real)
        PS.subprocess.run = lambda *a, **k: sp.CompletedProcess(
            a[0] if a else [], 1, "",
            "gh: Your token has not been granted the required scopes to execute "
            "this query. The 'id' field requires one of the following scopes: "
            "['read:project']")
        with self.assertRaises(PS.GhError) as e:
            PS.gh("query { viewer { login } }")
        self.assertIn("gh auth refresh -s project,read:project", str(e.exception))

    def test_an_ordinary_failure_is_not_dressed_up_as_a_scope_problem(self):
        text = PS._explain("Could not resolve to an Organization with the login of 'x'")
        self.assertNotIn("gh auth refresh", text)


class GhCalls(unittest.TestCase):
    """The transport. Every one of these is a way to mistake failure for data."""

    def reply(self, out, rc=0):
        import subprocess as sp
        real = PS.subprocess.run
        self.addCleanup(setattr, PS.subprocess, "run", real)
        PS.subprocess.run = lambda *a, **k: sp.CompletedProcess(
            a[0] if a else [], rc, out, "")

    def test_a_number_is_sent_as_a_number(self):
        """-f sends everything as a String, and Int! refuses a String.

        The failure is caught and read as "cannot tell", which is safe and
        useless: every deletion check answered unknown, so nothing was ever
        removed and the board only grew.
        """
        import subprocess as sp
        seen = {}
        real = PS.subprocess.run
        self.addCleanup(setattr, PS.subprocess, "run", real)

        def spy(args, **k):
            seen["args"] = args
            return sp.CompletedProcess(args, 0, json.dumps({"data": {}}), "")

        PS.subprocess.run = spy
        PS.gh("query { x }", number=31738, owner="ArduPilot")
        args = seen["args"]
        self.assertIn("-F", args, "an Int was sent with -f and would be refused")
        self.assertEqual(args[args.index("-F") + 1], "number=31738")
        self.assertEqual(args[args.index("owner=ArduPilot") - 1], "-f")

    def test_errors_beside_data_are_still_errors(self):
        # GitHub answers a partial result as data plus errors, exit 0. Reading
        # the data and ignoring the errors is how a short answer becomes truth.
        self.reply(json.dumps({"data": {"search": {"nodes": []}},
                               "errors": [{"message": "timed out"}]}))
        with self.assertRaises(PS.GhError):
            PS.gh("query { x }")

    def test_unparseable_output_is_an_error_not_an_empty_answer(self):
        self.reply("<html>502</html>")
        with self.assertRaises(PS.GhError):
            PS.gh("query { x }")

    def test_every_call_carries_a_deadline(self):
        """The sync runs from a run's EXIT trap, still holding the run lock.

        Asserting that a TimeoutExpired becomes a GhError proves nothing on its
        own - the stub raises it either way. What matters is that a timeout was
        asked for at all.
        """
        import subprocess as sp
        seen = {}
        real = PS.subprocess.run
        self.addCleanup(setattr, PS.subprocess, "run", real)

        def spy(*a, **k):
            seen.update(k)
            raise sp.TimeoutExpired(cmd="gh", timeout=1)

        PS.subprocess.run = spy
        with self.assertRaises(PS.GhError) as e:
            PS.gh("query { x }")
        self.assertIn("timed out", str(e.exception))
        self.assertTrue(seen.get("timeout"), "subprocess.run was given no timeout")


    def test_a_list_valued_call_carries_the_deadline_too(self):
        import subprocess as sp
        seen = {}
        real = PS.subprocess.run
        self.addCleanup(setattr, PS.subprocess, "run", real)

        def spy(*a, **k):
            seen.update(k)
            return sp.CompletedProcess(a[0] if a else [], 0, json.dumps({"data": {}}), "")

        PS.subprocess.run = spy
        PS.gh_list("mutation { x }", {"view": "v"}, {"fields": ["a"]})
        self.assertTrue(seen.get("timeout"), "gh_list ran gh with no timeout")


class SyncWrapper(unittest.TestCase):
    """project-sync.sh: one sync at a time, and never a failed run.

    Run against a fixture home with a stub in place of project-sync.py, so what
    is checked is the shell around it - the lock, the descriptors, the exit
    status - which nothing in Python can see.
    """

    WRAPPER = os.path.join(BIN, "project-sync.sh")
    STUB = """#!/usr/bin/env python3
import os, sys, time
d = os.environ["STUB_DIR"]
open(os.path.join(d, "ran"), "a").write("x")
open(os.path.join(d, "fds"), "w").write(" ".join(sorted(os.listdir("/proc/self/fd"))))
while os.environ.get("STUB_HOLD") and os.path.exists(os.path.join(d, "hold")):
    time.sleep(0.02)
sys.exit(int(os.environ.get("STUB_RC", "0")))
"""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        root = os.path.join(self.home, "review")
        for d in ("bin", "etc", "logs"):
            os.makedirs(os.path.join(root, d))
        with open(os.path.join(root, "bin", "review-env.sh"), "w") as f:
            f.write('export REVIEW_ROOT="$HOME/review"\n'
                    'export REVIEW_LOGS="$REVIEW_ROOT/logs"\n'
                    'export GH_TOKEN=bot-token\n')
        with open(os.path.join(root, "bin", "project-sync.py"), "w") as f:
            f.write(self.STUB)
        self.stub = os.path.join(self.home, "stub")
        os.makedirs(self.stub)
        self.env = {"HOME": self.home, "PATH": "/usr/bin:/bin", "STUB_DIR": self.stub}
        self.log = os.path.join(root, "logs", "project-sync.log")
        self.lock = os.path.join(root, "etc", "project-sync.lock")

    def run_wrapper(self, **kw):
        return subprocess.run(["bash", self.WRAPPER], env=dict(self.env, **kw),
                              capture_output=True, text=True, timeout=30)

    def read(self, path):
        if not os.path.exists(path):
            return ""
        with open(path) as f:
            return f.read()

    def runs(self):
        return len(self.read(os.path.join(self.stub, "ran")))

    def logged(self):
        return self.read(self.log)

    def wait_for(self, path):
        for _ in range(500):
            if os.path.exists(path):
                return
            time.sleep(0.01)
        self.fail("never appeared: " + path)

    def test_two_syncs_at_once_run_one(self):
        # the cron sweep and a run's exit path can fire together, and two in
        # flight delete each other's rows
        hold = os.path.join(self.stub, "hold")
        open(hold, "w").close()
        first = subprocess.Popen(["bash", self.WRAPPER], env=dict(self.env, STUB_HOLD="1"),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(first.wait)
        self.addCleanup(lambda: os.path.exists(hold) and os.unlink(hold))
        self.wait_for(os.path.join(self.stub, "ran"))
        # only the first is told to hold, so a second that wrongly runs
        # finishes at once and is counted rather than hanging the test
        second = self.run_wrapper()
        self.assertEqual(second.returncode, 0)
        self.assertEqual(self.runs(), 1, "both syncs ran")
        self.assertIn("another project sync is running", self.logged())
        os.unlink(hold)
        self.assertEqual(first.wait(timeout=30), 0)

    def test_python_holds_the_lock_while_it_runs(self):
        # python is the process doing the work; if only the shell held the
        # lock, killing the shell would free it while python was still writing
        self.assertEqual(self.run_wrapper().returncode, 0)
        self.assertEqual(self.runs(), 1)
        self.assertIn("9", self.read(os.path.join(self.stub, "fds")).split())

    def test_a_wrapper_killed_under_python_does_not_free_the_lock(self):
        hold = os.path.join(self.stub, "hold")
        open(hold, "w").close()
        first = subprocess.Popen(["bash", self.WRAPPER], env=dict(self.env, STUB_HOLD="1"),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: os.path.exists(hold) and os.unlink(hold))
        self.wait_for(os.path.join(self.stub, "ran"))
        first.kill()
        first.wait()
        second = self.run_wrapper()
        self.assertEqual(second.returncode, 0)
        self.assertEqual(self.runs(), 1, "a second sync ran beside the orphaned first")
        os.unlink(hold)
        for _ in range(500):            # the orphaned stub exits on its own
            if not os.path.exists("/proc/%d" % first.pid):
                break
            time.sleep(0.01)

    def test_a_lock_that_cannot_be_opened_is_a_skip_not_a_free_run(self):
        # from a run's exit trap, fd 9 arrives open on the run lock; if the
        # sync lock fails to open, that inherited descriptor is what flock
        # would be asked about, and it would say yes
        os.mkdir(self.lock)                      # cannot be opened for writing
        runlock = os.path.join(self.home, "run.lock")
        r = subprocess.run(
            ["bash", "-c", 'exec 9>"$1"; flock 9; bash "$2"', "_", runlock, self.WRAPPER],
            env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.runs(), 0, "it ran with no sync lock at all")
        self.assertIn("cannot open the sync lock", self.logged())

    def test_a_failed_sync_does_not_fail_the_run(self):
        r = self.run_wrapper(STUB_RC="1")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.runs(), 1)

    def test_the_bot_token_is_not_used(self):
        # the commenting token is deliberately narrow; the board is written
        # as the box login
        with open(os.path.join(self.home, "review", "bin", "project-sync.py"), "w") as f:
            f.write(self.STUB.replace(
                'open(os.path.join(d, "fds"), "w")',
                'open(os.path.join(d, "env"), "w").write(repr(os.environ.get("GH_TOKEN")));'
                'open(os.path.join(d, "fds"), "w")'))
        self.assertEqual(self.run_wrapper().returncode, 0)
        self.assertEqual(self.read(os.path.join(self.stub, "env")), "None")


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

    def test_it_finds_repos_json_through_a_symlinked_bin(self):
        """The deployed layout, which is not the layout tests usually run in.

        ~/review/bin is a symlink into the checkout, so a path built from
        __file__ without realpath resolves to ~/review and finds no repos.json.
        That is exactly how this broke on the box after passing every test here.
        """
        link = os.path.join(self.d, "bin")
        os.symlink(BIN, link)
        out = subprocess.run(
            ["python3", "-c",
             "import importlib.util,sys;"
             "spec=importlib.util.spec_from_file_location('ps', sys.argv[1]);"
             "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
             "print(' '.join(m.swept_owners()))",
             os.path.join(link, "project-sync.py")],
            capture_output=True, text=True,
            env={"HOME": self.d, "PATH": "/usr/bin:/bin"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("ArduPilot", out.stdout)

    def test_the_real_repos_file_names_the_orgs_we_sweep(self):
        # a search is per-org, so an org missing here is a repo silently never
        # put on the board
        owners = PS.swept_owners()
        self.assertIn("ArduPilot", owners)
        self.assertTrue(all(owners.count(o) == 1 for o in owners), owners)


if __name__ == "__main__":
    unittest.main(verbosity=2)
