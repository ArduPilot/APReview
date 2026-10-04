#!/usr/bin/env python3
"""Slice-two contracts: fixture reads, git history, served bytes and durable sends."""

import base64
import http.client
import copy
import importlib.util
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from review_fixtures import BIN, PR, candidate, complete_claim, stop, until, workspace, python
from review_store import Store, atomic, delivery_id, digest, read
from review_lock import try_lock
from review_github import GitHub
from review_discovery import Discovery, POST, normal_patch, rebase_only
from review_render import Renderer, sha, verify
from review_delivery import Board, Delivery, Posting, Publication, comment_body, delivery_marker
from review_inference import prepare


class FakeGitHub:
    def __init__(self, head="a" * 40, comments=()):
        self.head, self.comments, self.calls = head, list(comments), []
        self.before_write = lambda: None
        self.login = "bot"

    def request(self, endpoint, **kw):
        self.calls.append((endpoint, kw))
        if endpoint == "user":
            return {"login": self.login}
        if kw.get("method") in ("POST", "PATCH"):
            self.before_write()
            return dict(id=42, html_url="https://example.test/comment/42")
        return dict(head=dict(sha=self.head), node_id="node-1", user=dict(login="author"))

    def thread(self, *args, **kw):
        return copy.deepcopy(self.comments)


def accepted(store, **changes):
    inputs = candidate(
        pr=PR,
        manifest_key="1",
        title="A test",
        author="author",
        post=True,
        configuration={},
        **changes,
    )
    with try_lock(store.locks, PR) as lock:
        claim = complete_claim(store, lock, inputs=inputs)
        generation = claim["generation"]
        target = f"page:test/PRReviews/owner/repo/1/{generation}.html"
        publish = dict(kind="publish", target=target, retained=True)
        comment = dict(
            kind="comment", target=PR, dependencies=[delivery_id(PR, generation, "publish", target)]
        )
        store.accept(lock, PR, claim, [publish, comment])
    return store.bundle(PR)


def entry_of(store, bundle, kind):
    intent = next(i for i in bundle["intents"] if i["kind"] == kind)
    return read(store.root / "outbox" / (intent["id"] + ".json"))


class GithubTransport(unittest.TestCase):
    def setUp(self):
        self.root = workspace(self)

    def test_live_recordings_replay_without_network(self):
        directory = Path(__file__).parent / "fixtures" / "github"
        with patch("subprocess.run", side_effect=AssertionError("network used")):
            gh = GitHub(directory, "replay")
            records = list(directory.glob("*.json"))
            self.assertGreaterEqual(len(records), 5)
            for path in records:
                record = read(path)
                request = dict(record["request"])
                endpoint = request.pop("endpoint")
                self.assertEqual(gh.request(endpoint, **request), record["response"])

    def test_replay_missing_is_not_a_live_fallback(self):
        with patch("subprocess.run", side_effect=AssertionError("network used")):
            with self.assertRaises(OSError):
                GitHub(self.root, "replay").request("not-recorded")

    def test_writes_are_disabled_even_in_replay(self):
        from review_store import digest

        request = dict(
            endpoint="graphql",
            account="read",
            method="POST",
            payload={"query": "mutation { x }"},
            text=False,
        )
        atomic(
            self.root / (digest(request) + ".json"), dict(request=request, response={"data": {}})
        )
        with self.assertRaises(PermissionError):
            GitHub(self.root, "replay").request(
                "graphql", method="POST", payload={"query": "mutation { x }"}
            )

    def test_pagination_collects_all_and_rejects_truncation(self):
        gh = GitHub()
        gh.request = Mock(side_effect=[list(range(100)), [100]])
        self.assertEqual(gh.pages("comments"), list(range(101)))
        self.assertIn("page=2", gh.request.call_args.args[0])
        gh.request = Mock(return_value=dict(items=[], total_count=1001))
        with self.assertRaises(OSError):
            gh.pages("search", field="items")

    def test_repeated_page_is_not_an_infinite_loop(self):
        gh = GitHub()
        gh.request = Mock(return_value=list(range(100)))
        with self.assertRaises(OSError):
            gh.pages("comments")
        self.assertLess(gh.request.call_count, 4)

    def test_a_cut_reply_to_a_read_is_tried_again_but_a_write_is_not(self):
        cut = Mock(returncode=1, stdout="", stderr="unexpected end of JSON input")
        good = Mock(returncode=0, stdout='{"ok": 1}', stderr="")
        with patch("subprocess.run", side_effect=[cut, good]) as run, patch("time.sleep"):
            self.assertEqual(GitHub().request("repos/o/r/pulls/1"), {"ok": 1})
            self.assertEqual(run.call_count, 2)
        # a request GitHub refused is not retried
        missing = Mock(returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)")
        with patch("subprocess.run", side_effect=[missing, good]) as run, patch("time.sleep"):
            with self.assertRaises(OSError):
                GitHub().request("repos/o/r/pulls/1")
            self.assertEqual(run.call_count, 1)
        # a write may have landed; it is never repeated
        with patch("subprocess.run", side_effect=[cut, good]) as run, patch("time.sleep"):
            with self.assertRaises(OSError):
                GitHub(writes=True).request("repos/o/r/issues/1/comments", method="POST", payload={"body": "x"})
            self.assertEqual(run.call_count, 1)
        # three cut replies in a row are an error, not a hang
        with patch("subprocess.run", side_effect=[cut, cut, cut, good]) as run, patch("time.sleep"):
            with self.assertRaises(OSError):
                GitHub().request("repos/o/r/pulls/1")
            self.assertEqual(run.call_count, 3)

    def test_a_spent_allowance_is_its_own_error_with_the_reset_time(self):
        from review_github import RateLimited
        limited = Mock(returncode=1, stdout="", stderr="gh: API rate limit exceeded for user ID 1. (HTTP 403)")
        with patch("subprocess.run", return_value=limited) as run, \
                patch.object(GitHub, "rate_reset", staticmethod(lambda env, deadline=None: 1790000000.0)):
            with self.assertRaises(RateLimited) as caught:
                GitHub().request("repos/o/r/pulls/1")
            self.assertEqual(caught.exception.reset, 1790000000.0)
            self.assertEqual(run.call_count, 1)

    def test_project_identity_does_not_inherit_bot_token(self):
        with patch.dict(os.environ, GH_TOKEN="bot"), patch("subprocess.run") as run:
            run.return_value = Mock(returncode=0, stdout="{}")
            GitHub().request("query", account="project")
            self.assertNotIn("GH_TOKEN", run.call_args.kwargs["env"])


class DiscoveryContract(unittest.TestCase):
    def setUp(self):
        self.root = workspace(self)
        self.store = Store(self.root)
        self.config = dict(
            routing={"schema": 1, "repositories": [], "labels": [], "modes": ["all"]},
            comment_accounts=["new-bot", "old-bot"],
            stamp="2026-09-28_12-00",
            date="2026-09-28",
            repos={
                "repos": [
                    dict(
                        repo="owner/repo",
                        key="",
                        house_rules="none",
                        post_comments=True,
                        discovery="main",
                    )
                ]
            },
        )
        self.meta = dict(
            node_id="node-1",
            created_at="2020",
            title="Test",
            user={"login": "author"},
            draft=False,
            state="open",
            labels=[{"name": "AIReview"}],
            head={"sha": "a" * 40},
            base={"sha": "b" * 40, "repo": {"full_name": "owner/repo"}},
        )
        self.gh = Mock()
        self.gh.request.side_effect = (
            lambda endpoint, **kw: self.meta if "/pulls/" in endpoint else {"statuses": []}
        )
        self.gh.pages.return_value = []
        self.gh.thread.return_value = []
        self.discover = Discovery(self.gh, self.config, self.store)
        self.discover.snapshot_diff = Mock()

    def test_candidate_contract_and_house_rules(self):
        c = self.discover.candidate(PR, "AIReview")
        self.assertEqual(c["classification"], "REVIEW")
        self.assertEqual(c["manifest_key"], "1")
        self.assertEqual(c["ci"]["head"], c["head"])
        self.assertEqual(c["ci"]["state"], "none")
        self.assertIn("ArduPilot house rules do not apply", c["rules"])
        self.assertTrue(c["post"])
        self.assertEqual(len(c["destinations"]), 2)
        for key in (
            "previous_comment",
            "previous_section",
            "previous_manifest_head",
            "created_at",
            "merge_base",
            "reason",
        ):
            self.assertIn(key, c)

    def test_a_label_pr_at_its_published_head_is_reused_without_reading_its_thread(self):
        manifests = {"AIReview": {PR: {"head": "a" * 40}}}
        self.gh.thread.reset_mock()
        self.assertEqual(self.discover.candidate(PR, "AIReview", manifests)["classification"], "REUSE")
        self.gh.thread.assert_not_called()
        # a moved head is reviewed, and its thread is read for the previous round
        manifests = {"AIReview": {PR: {"head": "c" * 40}}}
        self.assertEqual(self.discover.candidate(PR, "AIReview", manifests)["classification"], "REVIEW")
        self.gh.thread.assert_called_once()
        # followup always needs the thread: the told head lives in our comment
        self.gh.thread.reset_mock()
        self.discover.candidate(PR, "followup", manifests)
        self.gh.thread.assert_called_once()

    def test_one_pr_github_will_not_describe_is_skipped_not_the_run(self):
        self.discover.search = lambda mode, swept: {"pr:owner/repo#1", "pr:owner/repo#2"}
        self.discover.swept = lambda: {"owner/repo": {}}
        self.discover.manifests = lambda mode=None: {}
        self.config["routing"] = dict(schema=1, repositories=[], labels=[], modes=["all"])
        def request(endpoint, **kw):
            if endpoint.endswith("/pulls/2"):
                raise OSError("unexpected end of JSON input")
            return dict(self.meta, number=1) if "/pulls/" in endpoint else {"statuses": []}
        self.gh.request.side_effect = request
        rows = self.discover.discover("AIReview")
        self.assertEqual([r["pr"] for r in rows], [PR])
        self.assertEqual(self.discover.skipped, ["pr:owner/repo#2"])

    def test_candidates_are_discovered_in_parallel_and_returned_in_order(self):
        import threading, time
        active, peak = [0], [0]
        gate = threading.Lock()
        real = self.discover.candidate
        def slow(pr, mode, manifests=None):
            with gate:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with gate:
                active[0] -= 1
            return real(pr, mode, manifests)
        self.discover.candidate = slow
        self.discover.search = lambda mode, swept: {f"pr:owner/repo#{n}" for n in range(1, 9)}
        self.discover.swept = lambda: {"owner/repo": {}}
        self.discover.manifests = lambda mode=None: {}
        self.config["routing"] = dict(schema=1, repositories=[], labels=[], modes=["all"])
        self.gh.request.side_effect = lambda endpoint, **kw: (
            dict(self.meta, number=int(endpoint.rsplit("/", 1)[1])) if "/pulls/" in endpoint else {"statuses": []})
        rows = self.discover.discover("AIReview")
        self.assertEqual(len(rows), 8)
        self.assertGreater(peak[0], 1)
        self.assertEqual([r["created_at"] for r in rows], sorted(r["created_at"] for r in rows))

    def test_filters_and_forced_pr_mode(self):
        manifests = {"AIReview": {PR: {"head": "a" * 40}}}
        self.assertEqual(
            self.discover.candidate(PR, "AIReview", manifests)["classification"], "REUSE"
        )
        self.assertEqual(self.discover.candidate(PR, "pr", manifests)["classification"], "REVIEW")
        # a draft is dropped, unless it carries AIReview: someone asked for it
        self.meta["draft"] = True
        self.meta["labels"] = []
        self.assertEqual(self.discover.candidate(PR, "pr")["classification"], "DROPPED")
        self.meta["labels"] = [{"name": "AIReview"}]
        self.assertEqual(self.discover.candidate(PR, "pr")["classification"], "REVIEW")
        self.assertEqual(self.discover.candidate(PR, "AIReview", manifests)["classification"], "REUSE")
        self.meta["draft"] = False
        self.meta["state"] = "closed"
        self.assertEqual(self.discover.candidate(PR, "pr")["classification"], "DROPPED")

    def test_followup_uses_newest_across_every_account(self):
        self.gh.thread.return_value = [
            dict(
                kind="comment",
                login=account,
                id=i,
                at=str(i),
                body="AI-generated\nReviewed head `" + head * 40 + "`.",
            )
            for i, account, head in ((1, "new-bot", "a"), (2, "old-bot", "b"), (3, "human", "c"))
        ]
        c = self.discover.candidate(PR, "followup")
        self.assertEqual(c["previous_comment"]["id"], 2)
        self.assertEqual(c["previous_comment"]["told_head"], "b" * 40)
        self.discover.snapshot_diff.assert_called_once()
        self.assertEqual(self.discover.snapshot_diff.call_args.args[1], "b" * 40)
        self.gh.thread.return_value = []
        self.assertEqual(self.discover.candidate(PR, "followup")["classification"], "DROPPED")

    def test_told_head_unchanged_needs_no_diff(self):
        self.gh.thread.return_value = [
            dict(
                kind="comment",
                login="old-bot",
                id=1,
                at="1",
                body="AI-generated\nReviewed head `" + "a" * 40 + "`.",
            )
        ]
        self.assertEqual(self.discover.candidate(PR, "followup")["classification"], "REUSE")
        self.discover.snapshot_diff.assert_not_called()
        # an unchanged PR costs its issue comments only, not reviews and
        # inline comments as well
        self.assertEqual([c.kwargs.get("kinds") for c in self.gh.thread.call_args_list], [("comment",)])

    def test_a_moved_followup_pr_reads_its_whole_thread(self):
        self.gh.thread.return_value = [dict(kind="comment", login="old-bot", id=1, at="1",
                                            body="AI-generated\nReviewed head `" + "b" * 40 + "`.")]
        self.assertEqual(self.discover.candidate(PR, "followup")["classification"], "REVIEW")
        self.assertEqual([c.kwargs.get("kinds") for c in self.gh.thread.call_args_list],
                         [("comment",), ("review_comment", "review")])

    def test_label_discovery_excludes_board_and_followup_unions_it(self):
        self.discover.swept = Mock(return_value={"owner/repo": {}})
        self.discover.search = Mock(return_value={PR})
        self.discover.board_rows = Mock(return_value={"pr:owner/repo#2": {}})
        self.discover.manifests = Mock(return_value={"AIReview": {PR: {}}})
        self.discover.discover("AIReview")
        self.discover.board_rows.assert_not_called()
        with patch.object(
            self.discover,
            "candidate",
            side_effect=lambda pr, *a: dict(pr=pr, created_at="2020", classification="REVIEW"),
        ), patch.object(self.discover.store, "in_followup_window", return_value=True):
            rows = self.discover.discover("followup")
        self.assertEqual({c["pr"] for c in rows}, {PR, "pr:owner/repo#2"})

    def test_followup_skips_prs_not_worked_on_within_the_window(self):
        self.discover.swept = Mock(return_value={"owner/repo": {}})
        self.discover.board_rows = Mock(return_value={"pr:owner/repo#2": {}})
        self.discover.manifests = Mock(return_value={"AIReview": {PR: {}}})
        worked = {PR: time.time() - 3 * 86400, "pr:owner/repo#2": time.time() - 20 * 86400}
        window = lambda pr, cutoff: worked[pr] >= cutoff
        asked = []
        with patch.object(self.discover, "candidate",
                          side_effect=lambda pr, *a: asked.append(pr) or dict(pr=pr, created_at="2020",
                                                                              classification="REVIEW")), \
                patch.object(self.discover.store, "in_followup_window", side_effect=window):
            rows = self.discover.discover("followup")
        self.assertEqual({c["pr"] for c in rows}, {PR})
        self.assertEqual(asked, [PR])           # no GitHub reads for the old one

    def test_the_batched_head_read_parses_and_predicts(self):
        def node(n, head, state="OPEN", more=False):
            return {"number": n, "state": state, "isDraft": False, "headRefOid": head, "baseRefName": "main",
                    "labels": {"nodes": [{"name": "AIReview"}], "pageInfo": {"hasNextPage": more}}}
        self.gh.graphql.return_value = {"rateLimit": {"cost": 1}, "repository": {
            "p1": node(1, "a" * 40), "p2": node(2, "b" * 40, "MERGED"), "p3": node(3, "c" * 40, more=True)}}
        heads = self.discover.batch_heads([PR, "pr:owner/repo#2", "pr:owner/repo#3"])
        self.assertEqual(sorted(heads), [PR, "pr:owner/repo#2"])    # incomplete labels left out
        self.assertEqual(heads[PR]["labels"], ["AIReview"])
        self.assertEqual(self.gh.graphql.call_count, 1)             # one query for the repository
        with patch.object(self.discover, "told_locally", return_value="a" * 40):
            self.assertEqual(self.discover.predict(PR, heads), "REUSE")
            self.assertEqual(self.discover.predict("pr:owner/repo#2", heads), "DROPPED")
            self.assertIsNone(self.discover.predict("pr:owner/repo#3", heads))
        with patch.object(self.discover, "told_locally", return_value="d" * 40):
            self.assertEqual(self.discover.predict(PR, heads), "FETCH")
        draft = dict(heads[PR], draft=True, labels=[])
        self.assertEqual(self.discover.predict(PR, {PR: draft}), "DROPPED")
        with patch.object(self.discover, "told_locally", return_value=None):
            self.assertIsNone(self.discover.predict(PR, heads))

    def test_the_shadow_can_never_fail_discovery(self):
        self.gh.graphql.side_effect = OSError("GraphQL errors")
        self.discover.shadow([PR], [dict(pr=PR, classification="REVIEW")])
        self.gh.graphql.side_effect = None
        self.gh.graphql.return_value = {"rateLimit": None, "repository": {"p1": {"number": 1}}}
        self.discover.shadow([PR], [dict(pr=PR, classification="REVIEW")])
        self.gh.graphql.return_value = None
        self.discover.shadow([PR], [dict(pr=PR, classification="REVIEW")])
        with patch.object(self.discover, "predict", side_effect=ValueError("bad history")):
            self.discover.shadow([PR], [dict(pr=PR, classification="REVIEW")])

    def test_the_shadow_stops_at_its_deadline_and_maps_stages(self):
        self.assertEqual(Discovery.stage(dict(classification="DROPPED", reason="no AI comment with told-head")), "FETCH")
        self.assertEqual(Discovery.stage(dict(classification="DROPPED", reason="closed")), "DROPPED")
        self.assertEqual(Discovery.stage(dict(classification="REUSE", reason="head already told")), "REUSE")
        self.assertEqual(Discovery.stage(dict(classification="REUSE", reason="rebase only")), "FETCH")
        seen, predicted = [], []
        self.gh.graphql.side_effect = lambda q, account=None, deadline=None: seen.append(deadline) or {}
        self.discover.shadow([PR], [dict(pr=PR, classification="REVIEW")])
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], time.monotonic())           # a real deadline reached GraphQL
        seen.clear()
        with patch.object(Discovery, "SHADOW_SECONDS", 0), \
                patch.object(self.discover, "predict", side_effect=lambda *a: predicted.append(a)):
            self.discover.shadow([PR], [dict(pr=PR, classification="REVIEW")])
        self.assertEqual((seen, predicted), ([], []))           # out of time: neither ran

    def test_a_failed_followup_discovery_never_moves_the_window(self):
        self.discover.swept = Mock(return_value={"owner/repo": {}})
        self.discover.board_rows = Mock(return_value={PR: {}})
        self.discover.manifests = Mock(return_value={})
        coverage = self.discover.store.root / "followup-coverage.json"
        atomic(coverage, {"at": time.time() - 30 * 86400})
        seen = []
        with patch.object(self.discover.store, "in_followup_window",
                          side_effect=lambda pr, cutoff: seen.append(cutoff) or True), \
                patch.object(self.discover, "candidate", side_effect=OSError("GitHub 502")):
            self.discover.discover("followup")
        # the cutoff came from the last completed coverage, a month back
        self.assertLess(seen[0], time.time() - 40 * 86400)
        # and the PR it could not read is kept in the window
        self.assertTrue((self.discover.store.pr_dir(PR) / "pending.json").exists())
        # discovery itself never records coverage: the controller does, once
        # its candidates are durable
        self.assertLess(read(coverage)["at"], time.time() - 29 * 86400)
        self.assertTrue(self.discover.followup_started)

    def test_search_restricts_organisations_and_repositories(self):
        self.gh.pages.return_value = [
            dict(repository_url="https://api.github.com/repos/owner/repo", number=1),
            dict(repository_url="https://api.github.com/repos/owner/outsider", number=2),
        ]
        self.assertEqual(self.discover.search("AIReview", {"owner/repo": {}}), {PR})
        self.assertIn("org%3Aowner", self.gh.pages.call_args.args[0])

    def test_dynamic_submodule_and_mavlink_keys(self):
        self.gh.request.side_effect = None
        self.gh.request.return_value = {
            "content": base64.b64encode(
                b'[submodule "m"]\nurl = https://github.com/ArduPilot/mavlink.git\n'
            ).decode()
        }
        self.discover.swept()
        self.assertEqual(self.discover.repos["ardupilot/mavlink"]["key"], "mavlink")

    def test_manifest_keys_do_not_alias_wiki_or_two_mavlinks(self):
        import repos

        discovery = Discovery(self.gh, dict(self.config, repos=repos.load()))
        discovery.repos["ardupilot/mavlink"] = dict(repo="ArduPilot/mavlink", key="mavlink")
        rows = discovery.parse_manifest(
            '<!-- reviewprs-manifest v1 heads="wiki#1:abcdef0 upstream-mavlink#1:abcdef1 mavlink#1:abcdef2" -->'
        )
        self.assertEqual(
            set(rows),
            {"pr:ardupilot/ardupilot_wiki#1", "pr:mavlink/mavlink#1", "pr:ardupilot/mavlink#1"},
        )

    def test_the_hyphen_key_form_the_command_publishes_is_read(self):
        # the AIReview page of 2026-09-29 keys every non-main repository as
        # key-number; a key nobody claims is skipped, not fatal
        import repos

        discovery = Discovery(self.gh, dict(self.config, repos=repos.load()))
        try:
            rows = discovery.parse_manifest(
                '<!-- reviewprs-manifest v1 heads="wiki-8080:abcdef0 34234:abcdef1 upstream-mavlink-523:abcdef2 '
                'wiki#8074:abcdef3 Nobody-7:abcdef4" -->')
        except OSError as error:
            self.fail("an unknown key ended the parse: %s" % error)
        self.assertEqual(set(rows), {"pr:ardupilot/ardupilot_wiki#8080", "pr:ardupilot/ardupilot#34234",
                                     "pr:mavlink/mavlink#523", "pr:ardupilot/ardupilot_wiki#8074"})
        self.assertEqual(discovery.unparsed, ["Nobody-7"])

    def test_a_legacy_page_section_is_found_for_import(self):
        # the command's pages, which the handoff imports, use div sections
        # closed by a bare </div>; the rsync page of 2026-08-26 is this shape
        import repos

        discovery = Discovery(self.gh, dict(self.config, repos=repos.load()))
        discovery.repos = {"rsyncproject/rsync": dict(repo="RsyncProject/rsync", key="")}
        html = ('<!-- reviewprs-manifest v1 label="AIReview" heads="1065:c512980a46 1060:0580585747 1059:1111111111" -->\n'
                '<h2>Reviews</h2>\n<div class="pr new" id="pr1065">\n<h3>1065</h3>\n<div class="x">nested</div>\n</div>\n'
                '<div class="pr" id="pr1060">\n<h3>1060</h3>\n</div>\n'
                '<div class="pr changed" id="pr1059">\n<h3>1059</h3>\n</div>\n<h2>Summary</h2>\n')
        rows = discovery.parse_manifest(html)
        # the label pages mark sections "pr new" and "pr changed" as well
        self.assertEqual(rows["pr:rsyncproject/rsync#1065"]["section"],
                         '<div class="pr new" id="pr1065">\n<h3>1065</h3>\n<div class="x">nested</div>\n</div>')
        self.assertEqual(rows["pr:rsyncproject/rsync#1060"]["section"],
                         '<div class="pr" id="pr1060">\n<h3>1060</h3>\n</div>')
        self.assertEqual(rows["pr:rsyncproject/rsync#1059"]["section"],
                         '<div class="pr changed" id="pr1059">\n<h3>1059</h3>\n</div>')
        self.assertEqual(rows["pr:rsyncproject/rsync#1060"]["key"], "1060")
        discovery.repos["rsyncproject/rsync"]["key"] = "rsync"
        rows = discovery.parse_manifest('<!-- reviewprs-manifest v1 heads="rsync#7:2222222222" -->\n'
                                        '<div class="pr" id="prrsync7">\n<h3>author page form</h3>\n</div>\n<h2>Summary</h2>\n')
        self.assertIn("author page form", rows["pr:rsyncproject/rsync#7"]["section"])

    def test_reserved_modes_and_pr_reference_precedence(self):
        self.assertEqual(self.discover.resolve("--FollowUp"), "followup")
        self.assertEqual(self.discover.resolve("/RSYNC"), "rsync")
        self.assertEqual(self.discover.resolve("#12"), "owner/repo#12")
        self.assertEqual(
            self.discover.resolve("https://github.com/owner/repo/pull/12/files?q=x"),
            "owner/repo#12",
        )
        self.gh.request.side_effect = None
        self.gh.request.return_value = {"login": "FollowUp"}
        self.assertEqual(self.discover.resolve("@FollowUp"), "@FollowUp")

    def test_removed_label_does_not_remove_other_label_membership(self):
        self.meta["labels"] = [{"name": "AIReview"}]
        manifests = {"DevCallTopic": {PR: {}}, "AIReview": {PR: {}}}
        c = self.discover.candidate(PR, "DevCallTopic", manifests)
        self.assertTrue(
            all(
                removed
                for target, removed in c["membership_removed"].items()
                if "/DevCallTopic/" in target
            )
        )
        self.assertTrue(
            all(
                not removed
                for target, removed in c["membership_removed"].items()
                if "/AIReview/" in target
            )
        )
        self.assertEqual(c["classification"], "DROPPED")

    def test_a_manifest_page_is_fetched_once_per_discovery(self):
        from unittest.mock import patch, MagicMock
        page = MagicMock()
        page.__enter__.return_value.read.return_value = (
            b'<!-- reviewprs-manifest v1 heads="1:aaaaaaaaaa" -->\n<section id="pr1">x</section>')
        config = dict(self.config, endpoints={"review": {"url": "https://example.test"}})
        discovery = Discovery(self.gh, config, self.store)
        with patch("review_discovery.urlopen", return_value=page) as opened:
            first = discovery.manifests("AIReview")
            second = discovery.manifests("AIReview")
        self.assertEqual(opened.call_count, 3)   # one per label page
        self.assertEqual(first, second)
        self.assertIn(PR, first["AIReview"])

    def test_a_failed_submodule_sweep_falls_back_to_the_listed_repositories(self):
        discovery = Discovery(self.gh, self.config, self.store)
        self.gh.request.side_effect = OSError("dial tcp: connect: network is unreachable")
        swept = discovery.swept()
        self.assertIn("owner/repo", swept)

    def test_manifests_are_built_once_for_a_burst_of_refreshes(self):
        discovery = Discovery(self.gh, self.config, self.store)
        calls = []
        real = discovery._manifests
        discovery._manifests = lambda mode=None: calls.append(mode) or real(mode)
        for _ in range(5):
            discovery.manifests("followup")
        self.assertEqual(calls, ["followup"])
        discovery._manifest_cache["followup"] = (0, {})   # long expired
        discovery.manifests("followup")
        self.assertEqual(calls, ["followup", "followup"])

    def test_manifests_sweep_submodules_before_parsing_keys(self):
        import base64
        modules = "[submodule \"modules/mavlink\"]\n\turl = https://github.com/ArduPilot/mavlink\n"
        self.gh.request.side_effect = (
            lambda endpoint, **kw: {"content": base64.b64encode(modules.encode()).decode()}
            if endpoint.endswith(".gitmodules") else self.meta if "/pulls/" in endpoint else {"statuses": []}
        )
        self.discover.manifests("AIReview")
        self.assertIn("ardupilot/mavlink", self.discover.repos)
        self.assertIn("page:review/PRReviews/owner/repo/1/index.html",
                      self.discover.candidate(PR, "pr")["destinations"])
        self.config["retained_prefix"] = "RsyncReviews/PRReviews"
        self.assertIn("page:review/RsyncReviews/PRReviews/owner/repo/1/index.html",
                      self.discover.candidate(PR, "pr")["destinations"])

    def test_failed_diff_defers_instead_of_claiming_coverage(self):
        self.discover.snapshot_diff.side_effect = OSError("no object")
        c = self.discover.candidate(PR, "pr")
        self.assertEqual(c["classification"], "DEFERRED")
        self.assertIn("no object", c["reason"])


class PatchHistory(unittest.TestCase):
    def setUp(self):
        self.root = workspace(self)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.test")
        self.git("config", "user.name", "Test")

    def git(self, *args):
        return (
            subprocess.check_output(["git", "-C", str(self.root), *args], stderr=subprocess.DEVNULL)
            .decode()
            .strip()
        )

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def test_hunk_offsets_and_index_lines_are_ignored(self):
        self.assertEqual(
            normal_patch("index abc..def 100644\n@@ -1,2 +1,2 @@ fn\n+a\n"),
            normal_patch("index 123..456 100644\n@@ -3,2 +3,2 @@ fn\n+a\n"),
        )

    def test_a_submodule_url_left_pointing_at_a_dead_worktree_is_repaired(self):
        import shutil
        import subprocess
        from review_inference import init_submodules
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_ALLOW_PROTOCOL="file:https",
                   GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        run = lambda *a, cwd=self.root: subprocess.run(a, cwd=cwd, check=True, capture_output=True, env=env)
        sub = self.root.parent / (self.root.name + "-sub")
        worktree = self.root.parent / (self.root.name + "-wt")
        second = self.root.parent / (self.root.name + "-wt2")
        for path in (sub, worktree, second):
            self.addCleanup(shutil.rmtree, path, True)
        sub.mkdir()
        run("git", "init", "-q", cwd=sub)
        (sub / "f").write_text("x")
        run("git", "add", ".", cwd=sub)
        run("git", "commit", "-qm", "s", cwd=sub)
        # the upstream URL, as .gitmodules names it; reachable only through
        # the rewrite while the reference is built
        upstream = "https://example.invalid/sub.git"
        run("git", "-c", "url.%s.insteadOf=%s" % (sub, upstream), "-c", "protocol.file.allow=always",
            "submodule", "add", "-q", upstream, "modules/sub")
        run("git", "commit", "-qm", "with submodule")
        run("git", "worktree", "add", "-q", "--detach", str(worktree))
        # what the reviewer did: the shared config now names a path that is gone
        run("git", "config", "submodule.modules/sub.url", str(self.root.parent / "gone"))
        shutil.rmtree(sub)   # the upstream is unreachable from here on
        os.environ["GIT_ALLOW_PROTOCOL"] = "file:https"
        self.addCleanup(os.environ.pop, "GIT_ALLOW_PROTOCOL", None)
        # the URL is put back from .gitmodules and the clone is made from the
        # reference's own checkout, never the network
        init_submodules(worktree, False, self.root)
        self.assertTrue((worktree / "modules/sub/f").exists())
        url = run("git", "config", "submodule.modules/sub.url").stdout.decode().strip()
        self.assertEqual(url, upstream)
        run("git", "worktree", "add", "-q", "--detach", str(second))
        init_submodules(second, False, self.root)
        self.assertTrue((second / "modules/sub/f").exists())

    def test_a_worktree_git_cannot_remove_in_time_is_deleted_and_pruned(self):
        import shutil
        import subprocess
        from unittest.mock import patch
        from review_inference import cleanup
        from review_store import Store
        (self.root / "f").write_text("x")
        self.commit("base")
        worktree = self.root.parent / (self.root.name + "-slow")
        self.addCleanup(shutil.rmtree, worktree, True)
        self.git("worktree", "add", "-q", "--detach", str(worktree))
        store = Store(self.root.parent / (self.root.name + "-store"))
        self.addCleanup(shutil.rmtree, store.root, True)
        real = subprocess.run
        def slow(args, **kw):
            if "remove" in args:
                raise subprocess.TimeoutExpired(args, kw.get("timeout"))
            return real(args, **kw)
        with patch("review_inference.subprocess.run", side_effect=slow):
            cleanup(store, {"worktree": str(worktree), "reference_clone": str(self.root)})
        self.assertFalse(worktree.exists())
        self.assertNotIn(str(worktree), self.git("worktree", "list"))

    def test_an_abbreviated_told_head_is_resolved_not_fetched_by_prefix(self):
        from unittest.mock import Mock
        (self.root / "text").write_text("one\n")
        told = self.commit("told")
        discovery = Discovery(Mock(), {"repos": {"repos": []}})
        # present locally: expanded without asking anyone
        self.assertEqual(discovery.told_commit(self.root, "owner/repo", told[:10]), told)
        discovery.gh.request.assert_not_called()
        # gone: the rebase shortcut is lost, the review is not deferred
        discovery.gh.request.side_effect = OSError("404 No commit found")
        self.assertIsNone(discovery.told_commit(self.root, "owner/repo", "0123456789"))

    def test_rebase_is_skipped_but_binary_regeneration_is_not(self):
        (self.root / "text").write_text("one\n" + "context\n" * 10 + "old\n")
        (self.root / "binary").write_bytes(b"\x00base")
        base = self.commit("base")
        (self.root / "text").write_text("one\n" + "context\n" * 10 + "new\n")
        (self.root / "binary").write_bytes(b"\x00authored")
        old = self.commit("old")
        self.git("checkout", "--detach", base)
        (self.root / "text").write_text("insert\none\n" + "context\n" * 10 + "old\n")
        newer_base = self.commit("base shifts hunk")
        (self.root / "text").write_text("insert\none\n" + "context\n" * 10 + "new\n")
        (self.root / "binary").write_bytes(b"\x00authored")
        new = self.commit("rebased patch")
        self.assertTrue(rebase_only(self.root, old, new, newer_base, ["text", "binary"]))
        (self.root / "binary").write_bytes(b"\x00regenerated")
        changed = self.commit("binary changed")
        self.assertFalse(rebase_only(self.root, old, changed, newer_base, ["text", "binary"]))


class RenderContract(unittest.TestCase):
    def setUp(self):
        self.root = workspace(self)
        self.store = Store(self.root)
        self.bundle = accepted(self.store)
        self.renderer = Renderer(self.store)

    def test_digest_and_section_identity_are_checked(self):
        raw = self.renderer.render("page:test/report.html", self.bundle)
        verified = verify(raw)
        self.assertEqual(verified["sections"][0]["pr"], PR)
        self.assertIn(b"<body>\n<!-- reviewprs-manifest", raw)
        self.assertIn(b'<p class="summary">', raw)
        self.assertIn(b"role','button", raw)
        with self.assertRaises(OSError):
            verify(raw.replace(b"Stub review.", b"Other review."))
        with self.assertRaises(OSError):
            verify(raw.replace(b"<h1>ArduPilot PR review: #1</h1>", b"<h1>Wrong report</h1>"))
        wrong = dict(verified["sections"][0], generation=2)
        with self.assertRaises(OSError):
            verify(raw, [wrong])

    def test_an_imported_legacy_section_with_nested_markup_verifies(self):
        # the command's sections nest divs, self-close tags and carry comments
        legacy = dict(pr="pr:owner/repo#7", generation=0, legacy=True,
                      inputs=dict(repository="owner/repo", number=7, head="a" * 10, manifest_key="7"),
                      results={"reconciliation": {"section_md":
                          '<div class="pr new" id="pr7">\n<h3>seven</h3>\n<ul><li class="f-bug"><span class="tag">BUG</span> '
                          '<div class="body">nested<br/>line<!-- note --> &amp; more</div></li></ul>\n</div>'}})
        raw = self.renderer.render("page:test/legacy.html", legacy)
        verified = verify(raw)
        self.assertEqual(verified["sections"][0]["pr"], "pr:owner/repo#7")
        self.assertIn(b"<h3>seven</h3>", raw)
        # the command's own card, styled by its own stylesheet, one anchor
        self.assertIn(b'<div class="pr new">', raw)
        self.assertEqual(raw.count(b'id="pr7"'), 1)

    def test_an_unbalanced_legacy_section_still_ends_where_the_renderer_says(self):
        for markup in ('<div class="pr" id="pr8">\n<h3>eight</h3>\n<div class="x">no closer\n</div>',
                       '<div class="pr" id="pr8">\n<h3>eight</h3>\n</div></div>\n</div>'):
            legacy = dict(pr="pr:owner/repo#8", generation=0, legacy=True,
                          inputs=dict(repository="owner/repo", number=8, head="b" * 10, manifest_key="8"),
                          results={"reconciliation": {"section_md": markup}})
            raw = self.renderer.render("page:test/legacy.html", legacy)
            self.assertEqual(verify(raw)["sections"][0]["pr"], "pr:owner/repo#8")
            self.assertIn(b"<h3>eight</h3>", raw)

    def test_section_digest_is_checked_even_with_valid_page_digest(self):
        from review_render import META

        raw = self.renderer.render("page:test/report.html", self.bundle).replace(
            b"Stub review.", b"Other review."
        )
        raw = META.sub(b"", raw)
        raw = raw.replace(
            b"</head>",
            b'<meta name="apreview-digest" content="' + sha(raw).encode() + b'">\n</head>',
        )
        with self.assertRaises(OSError):
            verify(raw)

    def test_pending_keeps_previous_head_and_progress_below_review(self):
        page = "page:test/report.html"
        self.store.merge_membership(
            page, {PR: dict(ticket=1, removed=False, progress="reviewing new head")}
        )
        with try_lock(self.store.locks, PR) as lock:
            claim = self.store.allocate(lock, PR, "next-run", "next-request", candidate())
            claim["status"] = "reviewing new head"
            self.store.save_claim(lock, PR, claim)
        raw = self.renderer.render(page)
        self.assertIn(('heads="1:' + "a" * 40 + '"').encode(), raw)
        self.assertGreater(raw.index(b"reviewing new head"), raw.index(b"Stub review."))
        self.store.merge_membership(page, {PR: dict(ticket=2, removed=True)})
        self.assertEqual(verify(self.renderer.render(page))["sections"], [])

    def test_ci_observation_time_is_not_a_ci_state_change(self):
        ci = dict(state="passing", head="a" * 40, at="2026-09-28T10:00:00Z")
        accepted(self.store, ci=ci)
        page = "page:test/ci.html"
        self.store.merge_membership(
            page, {PR: dict(ticket=1, removed=False, ci=dict(ci, at="2026-09-28T11:00:00Z"))}
        )
        self.assertNotIn(b"CI updated", self.renderer.render(page))
        self.store.merge_membership(
            page,
            {
                PR: dict(
                    ticket=2, removed=False, ci=dict(ci, state="failing", at="2026-09-28T12:00:00Z")
                )
            },
        )
        self.assertIn(b"CI updated", self.renderer.render(page))

    def test_a_label_page_reads_like_the_commands_report(self):
        # title, contents with title/author/verdict for imported reviews too
        page = "page:test/DevCallReviews/DevCallEU/devcall_pr_reviews.html"
        legacy = ('<div class="pr" id="pr9">\n<h3><a href="u">#9</a> &mdash; Fix the thing</h3>\n'
                  '<p class="meta"><span>Author: <strong>someone</strong></span>'
                  '<span>Verdict: <span class="v-request">REQUEST CHANGES</span></span></p>\n</div>')
        from review_handoff import legacy_bundle
        lock = __import__("review_lock").try_lock(self.store.locks, "pr:owner/repo#9")
        with lock:
            legacy_bundle(self.store, "pr:owner/repo#9", dict(head="c" * 10, section=legacy), page, 1, {})
        self.store.merge_membership(page, {"pr:owner/repo#9": dict(ticket=2, removed=False, generation=0)})
        raw = self.renderer.render(page).decode()
        self.assertIn("<title>ArduPilot DevCallEU PR reviews</title>", raw)
        self.assertIn("<h2>Contents</h2>", raw)
        self.assertIn(">Fix the thing</a>", raw)
        self.assertIn("<td data-sort=\"someone\">someone</td>", raw)
        self.assertIn('<span class="v-request">REQUEST CHANGES</span></td>', raw)
        self.assertNotIn("LEGACY", raw)
        self.assertNotIn("page:test", raw.split("-->", 1)[1])
        self.assertIn('content:"\\2195"', raw)

    def test_the_dated_page_is_the_calls_full_report(self):
        # people open DevCallReviews/<date>/devcall_pr_reviews.html for a call;
        # it must hold the reviews, not a list of links
        target = "page:test/DevCallReviews/2026_09_30/DevCallEU/devcall_pr_reviews.html"
        self.store.merge_membership(target, {PR: dict(ticket=1, removed=False,
                                                      generation=self.bundle["generation"])})
        anchor = verify(self.renderer.render(target))["sections"]
        self.assertEqual([x["pr"] for x in anchor], [PR])
        pages = {"DevCallEU": dict(path="DevCallEU/devcall_pr_reviews.html", anchors=["pr1"], target=target),
                 "AIReview": dict(path="AIReview/devcall_pr_reviews.html", anchors=["pr1", "pr9"])}
        raw = self.renderer.landing("2026_09_30", pages)
        self.assertEqual([x["pr"] for x in verify(raw)["sections"]], [PR])
        self.assertIn(b'label="DevCallEU"', raw)
        self.assertIn(b'href="AIReview/devcall_pr_reviews.html">AIReview', raw)
        # an anchor only another label has is still routed there
        self.assertIn(b'"pr9": "AIReview/devcall_pr_reviews.html"', raw)
        self.assertNotIn(b'"pr1":', raw)

    def test_dev_call_labels_archive_under_the_commands_dated_name(self):
        from review_discovery import Discovery
        discovery = Discovery(None, {"repos": {"repos": []}, "date": "2026-09-29"})
        self.assertEqual(discovery.destination("DevCallEU")[1],
                         "page:review/DevCallReviews/2026_09_30/DevCallEU/devcall_pr_reviews.html")

    def test_legacy_mapping_priority_retention_and_unavailable(self):
        pages = {
            label: dict(path=label + "/devcall_pr_reviews.html", anchors=["pr1"])
            for label in ("AIReview", "DevCallEU", "DevCallTopic")
        }
        raw = self.renderer.landing("2026-09-28", pages)
        self.assertIn(b"DevCallTopic/devcall_pr_reviews.html#pr1", raw)
        pages.pop("DevCallTopic")
        self.renderer.landing("2026-09-28", pages)
        pages["DevCallTopic"] = dict(path="DevCallTopic/devcall_pr_reviews.html", anchors=["pr1"])
        raw = self.renderer.landing("2026-09-28", pages)
        self.assertIn(b'id="pr1"><a href="DevCallEU', raw)
        raw = self.renderer.landing("2026-09-28", {})
        self.assertIn(b"Review unavailable: pr1", raw)


class LocalPublication(unittest.TestCase):
    def setUp(self):
        self.root = workspace(self)
        self.store = Store(self.root / "store")
        self.served = self.root / "served"
        self.served.mkdir()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "http.server",
                str(port),
                "--bind",
                "127.0.0.1",
                "--directory",
                str(self.served),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(stop, self.server)

        def ready():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return True
            except OSError:
                return False

        until(self, ready)
        self.config = dict(
            endpoints={"test": dict(publish=str(self.served), url=f"http://127.0.0.1:{port}")},
            comment_accounts=["bot"],
        )
        self.bundle = accepted(self.store)
        self.publisher = Publication(self.store, self.config)

    def test_rsync_http_and_replaced_section(self):
        entry = entry_of(self.store, self.bundle, "publish")
        with try_lock(self.store.locks, entry["target"]):
            receipt = self.publisher.deliver(entry, time.monotonic() + 10)
        self.assertEqual(receipt["state"], "published")
        self.assertEqual(receipt["sections"][0]["generation"], 1)
        comment = entry_of(self.store, self.bundle, "comment")
        self.publisher.verify_comment(comment, time.monotonic() + 5)
        path = self.served / "PRReviews/owner/repo/1/1.html"
        path.write_bytes(Renderer(self.store).render("page:test/empty.html"))
        self.publisher.verify_comment(comment, time.monotonic() + 5)
        self.assertEqual(verify(path.read_bytes())["sections"][0]["generation"], 1)

    def test_a_local_render_proves_an_upload_current_or_not(self):
        target = "page:test/report.html"
        self.store.merge_membership(target, {PR: dict(ticket=1, removed=False, generation=1)})
        claim = self.store.pr_dir(PR) / "claim.json"
        atomic(claim, dict(generation=2, status="active", attempts=[], selected={}))
        entry = dict(id="pub", pr=PR, generation="op", kind="publish", target=target, gate="page")
        with try_lock(self.store.locks, target):
            receipt = self.publisher.deliver(entry, time.monotonic() + 10)
            # the upload reports what it rendered for each row, by the same
            # model a controller compares observations with
            from review_render import bundle_at, comment_receipts, row_view
            row = read(self.store.root / "membership" / (digest(target) + ".json"))[PR]
            bundle = bundle_at(self.store, PR, 1)
            self.assertEqual(receipt["views"][PR], row_view(row, bundle, self.store.claim(PR),
                                                            comment_receipts(self.store, bundle), {}))
            self.assertTrue(Store.current_matches(self.publisher, entry, receipt))
            # another publication of the page since: the upload is no longer
            # what is served, whatever the render says
            directory = self.store.root / "pages" / digest(target)
            atomic(directory / "revision.json", receipt["revision"] + 1)
            self.assertFalse(Store.current_matches(self.publisher, entry, receipt))
            atomic(directory / "revision.json", receipt["revision"])
            # an upload that replaced the page and then failed its check leaves
            # the revision alone, but its epoch still invalidates the proof
            atomic(directory / "epoch.json", receipt["epoch"] + 1)
            self.assertFalse(Store.current_matches(self.publisher, entry, receipt))
            atomic(directory / "epoch.json", receipt["epoch"])
            self.assertTrue(Store.current_matches(self.publisher, entry, receipt))
            # a claim change alters the page with membership untouched, so the
            # earlier upload no longer proves anything
            atomic(claim, dict(generation=2, status="deferred", attempts=[], selected={}))
            self.assertFalse(Store.current_matches(self.publisher, entry, receipt))

    def test_a_pages_confirmed_upload_settles_a_later_publish_until_it_changes(self):
        target = "page:test/report.html"
        self.store.merge_membership(target, {PR: dict(ticket=1, removed=False, generation=1)})
        first = dict(id="pub1", pr=PR, generation="op1", kind="publish", target=target, gate="page")
        later = dict(first, id="pub2", generation="op2")
        with try_lock(self.store.locks, target):
            self.publisher.deliver(first, time.monotonic() + 10)
            confirmed = self.publisher.confirmed(later)
            self.assertTrue(self.store.contained(later, confirmed, self.publisher))
        self.store.merge_membership(target, {"pr:owner/repo#2": dict(ticket=2, removed=False)})
        with try_lock(self.store.locks, target):
            self.assertFalse(self.store.contained(later, confirmed, self.publisher))
        # a retained generation page is never settled this way
        self.assertIsNone(self.publisher.confirmed(dict(later, retained=True)))
        # nor by an upload to another server
        other = Publication(self.store, dict(self.config, endpoints={"test": dict(
            self.config["endpoints"]["test"], url="http://elsewhere.invalid")}))
        self.assertIsNone(other.confirmed(later))

    def test_a_landing_page_keeps_its_served_sections_in_its_route_history(self):
        target = "page:test/DevCallReviews/2026_10_04/A/devcall_pr_reviews.html"
        self.store.merge_membership(target, {PR: dict(ticket=1, removed=False, generation=1)})
        renderer = Renderer(self.store)
        page = dict(path="A/devcall_pr_reviews.html", anchors=[], target=target)
        body = renderer.landing("2026_10_04", {"A": page}).decode()
        served = re.findall(r'<section id="([^"]+)"', body)
        self.assertTrue(served)
        routes = read(self.store.root / "landing" / "2026_10_04.json")
        self.assertTrue(set(served) <= set(routes))

    def test_a_probe_render_of_a_landing_page_saves_no_routes(self):
        routes = self.store.root / "landing" / "2026_10_04.json"
        renderer = Renderer(self.store)
        renderer.landing("2026_10_04", {"A": dict(path="A/devcall_pr_reviews.html", anchors=["pr-1"], target=None)},
                         commit=False)
        self.assertFalse(routes.exists())
        renderer.landing("2026_10_04", {"A": dict(path="A/devcall_pr_reviews.html", anchors=["pr-1"], target=None)})
        self.assertEqual(read(routes), {"pr-1": "A/devcall_pr_reviews.html"})

    def test_the_fetch_back_is_sampled_and_the_local_copy_is_the_served_one(self):
        from review_render import served_copy
        target = "page:test/report.html"
        self.store.merge_membership(target, {PR: dict(ticket=1, removed=False, generation=1)})
        entry = dict(id="pub", pr=PR, generation="op", kind="publish", target=target, gate="page")
        fetched = []
        real = self.publisher.fetch
        self.publisher.fetch = lambda *a, **k: fetched.append(a[0]) or real(*a, **k)
        with try_lock(self.store.locks, target):
            self.publisher.deliver(entry, time.monotonic() + 10)      # first: checked
            self.publisher.deliver(entry, time.monotonic() + 10)      # soon after: not
        self.assertEqual(fetched, [target])
        served = served_copy(self.store, target)
        self.assertEqual(served, (self.served / "report.html").read_bytes())
        self.assertEqual(served_copy(self.store, target, self.publisher.destination(target)), served)
        self.assertIsNone(served_copy(self.store, target, ["elsewhere", "http://elsewhere.invalid"]))
        # an upload attempted since, even one that failed, voids the copy
        epoch = self.store.root / "pages" / digest(target) / "epoch.json"
        atomic(epoch, read(epoch) + 1)
        self.assertIsNone(served_copy(self.store, target))
        atomic(epoch, read(epoch) - 1)
        # a local source that no longer matches the confirmed upload is not used
        source = self.store.root / "pages" / digest(target) / "report.html"
        source.write_bytes(source.read_bytes() + b"<!-- later attempt -->")
        self.assertIsNone(served_copy(self.store, target))

    def test_publication_uses_frozen_rsync_auth_options(self):
        from review_delivery import run_external
        option = "--password-file=/outside/credentials with spaces"
        self.config["endpoints"]["test"]["rsync_args"] = [option]
        seen = []
        def transport(argv, **kwargs):
            seen.extend(argv)
            # The local rsync fixture needs no password file. Verify the exact
            # argument at the boundary, then exercise the real local transfer.
            return run_external([x for x in argv if x != option], **kwargs)
        entry = entry_of(self.store, self.bundle, "publish")
        with patch("review_delivery.run_external", side_effect=transport):
            with try_lock(self.store.locks, entry["target"]):
                self.publisher.deliver(entry, time.monotonic() + 10)
        self.assertIn(option, seen)

    def test_held_comment_annotation_has_its_own_locked_publication(self):
        target = "page:test/report.html"
        self.store.merge_membership(target, {PR: dict(ticket=1, removed=False, generation=1)})
        comment = entry_of(self.store, self.bundle, "comment")
        self.store.receipt(comment, "held", manual_command="gh pr comment --body-file held.md")
        entry = dict(id="annotation", pr=PR, generation=1, kind="annotation", target=target)
        adapter = Delivery(self.store, FakeGitHub(), self.config)
        with try_lock(self.store.locks, PR) as gate:
            stack, locks = self.store.side_locks(entry, [], gate)
            with stack:
                child = python(
                    "from review_lock import try_lock; import sys; sys.exit(1 if try_lock(sys.argv[1], sys.argv[2]) else 0)",
                    self.store.locks,
                    target,
                )
                self.assertEqual(child.wait(timeout=3), 0)
                try:
                    receipt = adapter.deliver(entry, time.monotonic() + 5)
                except OSError as error:
                    self.fail(str(error))
                self.store.receipt(entry, **receipt)
        self.assertEqual(receipt["state"], "published")
        body = (self.served / "report.html").read_text()
        self.assertIn("Comment held for a human", body)
        self.assertIn("gh pr comment --body-file held.md", body)

    def test_http_success_with_wrong_page_digest_is_not_publication(self):
        entry = entry_of(self.store, self.bundle, "publish")
        with patch.object(
            self.publisher, "fetch", return_value=dict(page_digest="0" * 64, sections=[])
        ):
            with self.assertRaises(OSError):
                self.publisher.deliver(entry, time.monotonic() + 5)

    def test_comment_requires_retained_generation_page(self):
        entry = entry_of(self.store, self.bundle, "comment")
        with patch("review_delivery.bundle_at", return_value=dict(self.bundle, intents=[])):
            with self.assertRaises(OSError):
                self.publisher.verify_comment(entry, time.monotonic() + 5)

    def test_drain_freezes_payload_before_request_and_receipts_separately(self):
        gh = FakeGitHub()
        adapter = Delivery(self.store, gh, self.config)
        self.store.drain(
            adapter, seconds=10, snapshot=[entry_of(self.store, self.bundle, "publish")]
        )
        entry = entry_of(self.store, self.bundle, "comment")

        def assert_frozen():
            frozen = read(self.store.root / "outbox" / (entry["id"] + ".json"))
            self.assertEqual(frozen["state"], "sending")
            self.assertEqual(
                frozen["payload"]["body_digest"], sha(frozen["payload"]["body"].encode())
            )
            code = "from review_lock import try_lock; import sys; x=try_lock(sys.argv[1],sys.argv[2]); sys.exit(1 if x else 0)"
            child = python(code, self.store.locks, self.bundle["intents"][0]["target"])
            self.assertEqual(child.wait(timeout=3), 0)

        gh.before_write = assert_frozen
        self.store.drain(adapter, seconds=10)
        receipt = read(self.store.root / "receipts" / (entry["id"] + ".json"))
        self.assertEqual(receipt["comment_id"], 42)
        self.assertEqual(receipt["state"], "posted")


class PostingContract(unittest.TestCase):
    def setUp(self):
        self.root = workspace(self)
        self.store = Store(self.root)
        self.bundle = accepted(self.store)
        self.entry = entry_of(self.store, self.bundle, "comment")
        self.gh = FakeGitHub()
        self.pages = Mock()
        self.pages.url.return_value = "https://reports.test/retained.html"
        self.now = 1000
        self.poster = Posting(
            self.store,
            self.gh,
            self.pages,
            dict(comment_accounts=["bot", "oldbot"]),
            now=lambda: self.now,
        )
        self.deadline = time.monotonic() + 30
        self.poster.prepare(self.entry, self.deadline)

    def test_changed_credential_identity_cannot_send_frozen_body(self):
        self.gh.login = "other-bot"
        with self.assertRaises(OSError):
            self.poster.deliver(self.entry, self.deadline)
        self.assertFalse([c for c in self.gh.calls if c[1].get("method")])

    def test_held_manual_body_uses_durable_raw_utf8(self):
        bundle = dict(self.bundle, inputs=dict(self.bundle["inputs"], held=True))
        with patch("review_delivery.bundle_at", return_value=bundle):
            receipt = self.poster.prepare(self.entry, self.deadline)
        path = self.store.root / "held" / (self.entry["id"] + ".md")
        self.assertEqual(receipt["state"], "held")
        self.assertTrue(path.read_text().startswith("**Automated review note"))
        self.assertEqual(path.read_text().splitlines()[2], delivery_marker(self.entry["id"]))
        self.assertIn(str(path), receipt["manual_command"])

    def test_exact_header_and_moved_head(self):
        body = comment_body(self.entry, self.bundle, "https://report", "b" * 40)
        lines = body.splitlines()
        self.assertIn("AI-generated", lines[0])
        self.assertEqual(lines[1], "**Verdict: ACCEPT**")
        self.assertEqual(lines[2], delivery_marker(self.entry["id"]))
        self.assertIn("b" * 40, body)
        self.assertIn("only `" + "a" * 40, body)
        note = dict(self.entry, kind="note")
        self.assertEqual(
            comment_body(note, self.bundle, "https://report", "a" * 40).splitlines()[1], ""
        )

    def test_note_never_edits_or_deprecates_a_verdict(self):
        body = self.entry["payload"]["body"]
        thread = [dict(kind="comment", id=1, login="bot", at="1", body=body)]
        action, ident = POST.decide(thread, body, ["bot"], note=True)
        self.assertEqual((action, ident), ("post", None))

    def match(self, **changes):
        return dict(
            dict(
                kind="comment",
                id=42,
                login="bot",
                at="2026-09-28",
                body=self.entry["payload"]["body"],
            ),
            **changes,
        )

    def test_unique_marker_body_account_and_target(self):
        self.gh.comments = [self.match()]
        self.assertEqual(self.poster.reconcile(self.entry, self.deadline)["comment_id"], 42)
        self.gh.comments = [self.match(login="human")]
        self.assertIsNone(self.poster.reconcile(self.entry, self.deadline))
        self.gh.comments = [self.match(body=self.entry["payload"]["body"] + "changed")]
        self.assertIsNone(self.poster.reconcile(self.entry, self.deadline))
        self.assertEqual(self.entry["state"], "uncertain")
        self.gh.comments = [self.match(), self.match(id=43)]
        self.assertIsNone(self.poster.reconcile(self.entry, self.deadline))
        self.assertEqual(self.entry["failures"], 5)

    def test_two_reads_after_grace_with_durable_schedule(self):
        self.now = 1119
        self.assertIsNone(self.poster.reconcile(self.entry, self.deadline))
        self.assertFalse([c for c in self.gh.calls if c[1].get("method")])
        self.now = 1120
        self.assertIsNone(self.poster.reconcile(self.entry, self.deadline))
        recovered = read(self.store.root / "outbox" / (self.entry["id"] + ".json"))
        self.assertEqual(recovered["absent_at"], 1120)
        self.now = 1179
        self.assertIsNone(self.poster.reconcile(recovered, self.deadline))
        self.now = 1180
        self.assertEqual(self.poster.reconcile(recovered, self.deadline)["state"], "posted")
        self.assertEqual(self.gh.calls[-1][1]["payload"]["body"], self.entry["payload"]["body"])

    def test_superseded_ambiguous_write_is_not_retried(self):
        self.entry.update(superseded=True, absent_at=1120)
        self.now = 1180
        self.assertEqual(self.poster.reconcile(self.entry, self.deadline)["state"], "superseded")
        self.assertFalse([c for c in self.gh.calls if c[1].get("method")])

    def test_frozen_head_change_holds_instead_of_rewriting(self):
        self.entry["absent_at"] = 1120
        self.now = 1180
        self.gh.head = "c" * 40
        self.assertEqual(self.poster.reconcile(self.entry, self.deadline)["state"], "held")
        self.assertFalse([c for c in self.gh.calls if c[1].get("method")])

    def test_deprecation_is_frozen_and_separate(self):
        predecessor = self.match(id=1, body="old AI-generated")
        receipt = dict(state="posted", comment_id=42, predecessors=[predecessor])
        atomic(self.store.root / "receipts" / "posted.json", receipt)
        entry = dict(pr=PR, dependencies=["posted"])
        self.poster.deprecate(entry, self.deadline)
        self.assertIn("/comments/1", self.gh.calls[-1][0])
        self.assertNotIn("/comments/42", self.gh.calls[-1][0])

    def test_deprecation_leaves_another_logins_comments_to_their_own_job(self):
        # the old command posted as a different login; the bot cannot edit
        # those, and GitHub answers "Must have admin rights to Repository"
        mine = self.match(id=1, body="old AI-generated")
        theirs = self.match(id=2, login="legacy", body="older AI-generated")
        receipt = dict(state="posted", comment_id=42, account="bot", predecessors=[mine, theirs])
        atomic(self.store.root / "receipts" / "posted2.json", receipt)
        before = len(self.gh.calls)
        self.assertEqual(self.poster.deprecate(dict(pr=PR, dependencies=["posted2"]), self.deadline)["state"],
                         "deprecated")
        edited = [c[0] for c in self.gh.calls[before:] if "/issues/comments/" in c[0]]
        self.assertEqual([e.rsplit("/", 1)[1] for e in edited], ["1"])


class BoardContract(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub(
            comments=[
                dict(kind="comment", id=3, login="bot", at="3", body="AI-generated\n\nA note"),
                dict(
                    kind="comment",
                    id=2,
                    login="bot",
                    at="2",
                    body="AI-generated\n**Verdict: ACCEPT**",
                ),
            ]
        )
        self.board = Board(self.gh, dict(project_id="project", comment_accounts=["bot"]))
        from review_discovery import module

        self.functions = module("project-sync")
        self.functions.ensure_field = Mock(return_value=("field", {"ACCEPT": "option"}, "existing"))
        self.functions.ensure_author_field = Mock(return_value=("author-field", "existing"))
        self.row = dict(item="item", content="node-1", result="ACCEPT", author="author")
        self.functions.project_items = Mock(return_value={PR: self.row})
        self.gh.graphql = Mock(return_value={})

    def test_targeted_ack_preserves_verdict_under_newer_note(self):
        with patch("review_delivery.module", return_value=self.functions):
            ack = self.board.deliver(
                dict(id="delivery", pr=PR, node_id="node-1"), time.monotonic() + 10
            )
        self.assertEqual(
            ack,
            dict(
                state="synced",
                delivery_id="delivery",
                node_id="node-1",
                item="item",
                comment_id=2,
                fields=dict(result="ACCEPT", author="author"),
            ),
        )

    def test_unlabelled_pr_is_inserted_by_node_id(self):
        self.functions.project_items.side_effect = [{}, {PR: self.row}]
        self.gh.graphql.return_value = {"addProjectV2ItemById": {"item": {"id": "item"}}}
        with patch("review_delivery.module", return_value=self.functions):
            ack = self.board.deliver(
                dict(id="delivery", pr=PR, node_id="node-1"), time.monotonic() + 10
            )
        self.assertEqual(ack["node_id"], "node-1")
        self.assertTrue(
            any(call.kwargs.get("content") == "node-1" for call in self.gh.graphql.call_args_list)
        )

    def test_mismatched_readback_is_not_success(self):
        self.functions.project_items.side_effect = [
            {PR: self.row},
            {PR: dict(self.row, result="COMMENT")},
        ]
        with patch("review_delivery.module", return_value=self.functions):
            with self.assertRaises(OSError):
                self.board.deliver(dict(id="delivery", pr=PR), time.monotonic() + 10)

    def test_changed_node_cannot_update_wrong_pr(self):
        with patch("review_delivery.module", return_value=self.functions):
            with self.assertRaises(OSError):
                self.board.deliver(
                    dict(id="delivery", pr=PR, node_id="foreign"), time.monotonic() + 10
                )


class RealInference(unittest.TestCase):
    def setUp(self):
        self.root = workspace(self)
        self.store = Store(self.root / "data")
        self.reference = self.root / "reference"
        self.reference.mkdir()
        for args in (
            ["init", "-q"],
            ["config", "user.email", "test@example.test"],
            ["config", "user.name", "Test"],
        ):
            subprocess.run(["git", "-C", str(self.reference), *args], check=True)
        (self.reference / "x").write_text("pinned")
        subprocess.run(["git", "-C", str(self.reference), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.reference), "commit", "-qm", "base"], check=True)
        self.head = (
            subprocess.check_output(["git", "-C", str(self.reference), "rev-parse", "HEAD"])
            .decode()
            .strip()
        )
        self.fake = self.root / "bin"
        self.fake.mkdir()
        for cli in ("claude", "codex"):
            path = self.fake / cli
            path.write_text(
                "#!/usr/bin/env python3\nimport os,json,pathlib,sys\nsys.path.insert(0,"
                + repr(str(BIN))
                + ')\nfrom review_schema import canned,FILES\np=pathlib.Path(os.environ["REVIEW_JOB_DIR"])\nj=json.loads((p/"job.json").read_text())\n(p/FILES[j["kind"]]).write_text(json.dumps(canned(j)))\n(p/"observed.json").write_text(json.dumps(dict(cwd=os.getcwd(),argv=sys.argv,env=dict(os.environ))))\n'
            )
            path.chmod(0o755)
        self.config = dict(
            prompts={
                kind: "Frozen " + kind + " prompt"
                for kind in ("primary", "cold", "validation", "reconciliation")
            },
            reference_clones={"owner/repo": str(self.reference)},
            path=str(self.fake) + ":" + str(self.reference / "Tools/autotest") + ":/usr/bin:/bin",
            providers={},
        )
        for cli in ("claude", "codex"):
            self.config["providers"][cli] = dict(
                account=cli + "-account",
                home=str(self.root / cli),
                model=cli + "-test-model",
                effort="high",
                permission_mode="auto" if cli == "claude" else "workspace-write",
                granted_directories=[str(self.root / "granted")],
            )

    def test_fake_clis_use_pinned_worktree_frozen_options_and_environment(self):
        from review_schema import read_result, FILES

        (self.reference / "x").write_text("base moved after pin")
        subprocess.run(
            ["git", "-C", str(self.reference), "commit", "-qam", "base moved"], check=True
        )

        for kind, provider in (("primary", "claude"), ("cold", "codex")):
            path = self.root / kind
            path.mkdir()
            job = dict(
                candidate(),
                run="run",
                job="job",
                attempt=kind,
                generation=1,
                kind=kind,
                provider=provider,
                head=self.head,
                env={},
                rules="injected",
            )
            prepare(self.store, path, job, self.config)
            atomic(path / "job.json", job)
            result = subprocess.run(
                job["command"],
                env=dict(os.environ, **job["env"], REVIEW_JOB_DIR=str(path)),
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(read_result(path / FILES[kind], job)["status"], "complete")
            observed = read(path / "observed.json")
            self.assertEqual(observed["cwd"], str(path / "wt"))
            self.assertEqual(observed["env"]["BUILDLOGS"], str(path / "buildlogs"))
            # scratch work stays inside the attempt, where GC can find it
            self.assertEqual(observed["env"]["REVIEW_SCRATCH"], str(path / "scratch"))
            self.assertEqual(observed["env"]["TMPDIR"], str(path / "scratch"))
            self.assertTrue((path / "scratch").is_dir())
            for cache in ("UV_CACHE_DIR", "PIP_CACHE_DIR", "npm_config_cache", "REVIEW_VENVS"):
                self.assertFalse(observed["env"][cache].startswith(str(self.store.root) + "/"), cache)
            self.assertNotIn(str(self.reference / "Tools/autotest"), observed["env"]["PATH"])
            self.assertIn(provider + "-test-model", observed["argv"])
            self.assertIn("Frozen " + kind + " prompt", " ".join(observed["argv"]))
            self.assertIn(str(self.root / "granted"), observed["argv"])
            self.assertEqual(
                subprocess.check_output(["git", "-C", str(path / "wt"), "rev-parse", "HEAD"])
                .decode()
                .strip(),
                self.head,
            )
            from review_inference import cleanup

            cleanup(self.store, job)

    def test_supervisor_without_stub_runs_all_four_fake_cli_jobs(self):
        from review_store import StubAdapter

        spec = importlib.util.spec_from_file_location(
            "real_adapter_supervisor", BIN / "review-supervisor.py"
        )
        supervisor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(supervisor)
        c = candidate(pr=PR, mode="pr", post=False)
        c["head"] = self.head
        c["configuration"] = self.config
        discovery = Mock()
        discovery.refresh.return_value = c
        discovery.gh.request.return_value = dict(title="Test", head={"sha": self.head})
        discovery.gh.thread.return_value = []
        directory = self.store.root / "runs" / "real"
        with patch.dict(os.environ, REVIEW_GUARDIAN_PLAIN="1"):
            os.environ.pop("REVIEW_AI_STUB", None)
            with patch.object(supervisor, "Discovery", return_value=discovery), patch.object(
                supervisor, "Delivery", return_value=StubAdapter(self.store.root)
            ):
                controller = supervisor.Supervisor(
                    self.store.root,
                    directory,
                    [c],
                    configuration=self.config,
                    mode="pr",
                    admission=8,
                    wall=5,
                )
                self.assertEqual(controller.run(coordinate=False), 0)
        summary = read(directory / "summary.json")
        self.assertEqual(summary["prs"][PR]["review"], "accepted")
        jobs = list((directory / "attempts").glob("*/job.json"))
        self.assertEqual(
            {read(path)["kind"] for path in jobs},
            {"primary", "cold", "validation", "reconciliation"},
        )
        for path in jobs:
            self.assertTrue((path.parent / "observed.json").exists())
            self.assertFalse((path.parent / "wt").exists())

    def test_worktree_git_keeps_refresh_lease_when_controller_dies(self):
        gitbin = self.root / "gitbin"
        gitbin.mkdir()
        ready = self.root / "git-ready"
        executable = gitbin / "git"
        executable.write_text(
            "#!/usr/bin/env python3\nimport pathlib,sys,time\nif 'worktree' in sys.argv:\n pathlib.Path("
            + repr(str(ready))
            + ").touch()\n time.sleep(2)\n"
        )
        executable.chmod(0o755)
        path = self.root / "dying-attempt"
        path.mkdir()
        atomic(path / "config.json", self.config)
        code = "from pathlib import Path; import sys; from review_inference import prepare; from review_store import Store,read; p=Path(sys.argv[1]); prepare(Store(sys.argv[2]),p,dict(repository='owner/repo',provider='claude',kind='primary',head='a'*40,env={},rules=''),read(p/'config.json'))"
        env = dict(os.environ, PYTHONPATH=str(BIN), PATH=str(gitbin) + ":/usr/bin:/bin")
        controller = subprocess.Popen(
            [sys.executable, "-c", code, str(path), str(self.store.root)], env=env
        )
        self.addCleanup(stop, controller)
        until(self, ready.exists)
        controller.kill()
        controller.wait(timeout=3)
        lease = try_lock(self.store.locks, "refresh")
        if lease:
            lease.close()
        self.assertIsNone(lease, "git outlived the controller without its refresh lease")
        until(self, lambda: try_lock(self.store.locks, "refresh"), seconds=4).close()

    def test_partial_reference_is_refused(self):
        subprocess.run(
            ["git", "-C", str(self.reference), "config", "remote.origin.promisor", "true"],
            check=True,
        )
        job = dict(candidate(), provider="claude", env={}, head=self.head, kind="primary")
        path = self.root / "attempt"
        path.mkdir()
        with self.assertRaises(OSError):
            prepare(self.store, path, job, self.config)

    def test_heavy_nested_wrapper_reuses_single_slot(self):
        env = dict(
            os.environ,
            REVIEW_DATA=str(self.store.root),
            REVIEW_HEAVY_SIZE="1",
            REVIEW_HEAVY_WAIT=".5",
        )
        wrapper = str(BIN / "review-heavy.sh")
        result = subprocess.run(
            [
                wrapper,
                wrapper,
                sys.executable,
                "-c",
                'import os; os.fstat(int(os.environ["REVIEW_HEAVY_FD"])); print("inherited")',
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "inherited")

    def test_heavy_background_descendant_holds_slot_after_wrapper_exits(self):
        env = dict(
            os.environ,
            REVIEW_DATA=str(self.store.root),
            REVIEW_HEAVY_SIZE="1",
            REVIEW_HEAVY_WAIT=".15",
        )
        code = 'import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(1.5)"],close_fds=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)'
        wrapper = str(BIN / "review-heavy.sh")
        first = subprocess.run(
            [wrapper, sys.executable, "-c", code], env=env, capture_output=True, timeout=5
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        second = subprocess.run([wrapper, "true"], env=env, capture_output=True, timeout=5)
        self.assertEqual(second.returncode, 75)
        until(self, lambda: try_lock(self.store.locks, "permit:heavy:0"), seconds=3).close()


class PhaseController(unittest.TestCase):
    def setUp(self):
        self.root = workspace(self)
        spec = importlib.util.spec_from_file_location(
            "adapter_supervisor", BIN / "review-supervisor.py"
        )
        self.supervisor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.supervisor)

    def test_all_label_discoveries_join_before_followup_and_resume_never_rediscovers(self):
        barrier = threading.Barrier(3, timeout=5)
        calls = []
        directory = self.root / "runs" / "all"

        def discover(mode):
            calls.append(mode)
            if mode != "followup":
                barrier.wait()
                return [
                    candidate(
                        pr=PR,
                        mode=mode,
                        classification="REVIEW",
                        post=False,
                        destinations=["page:stub/" + mode + ".html"],
                    )
                ]
            summary = read(directory / "summary.json")
            self.assertEqual(summary["state"], "running")
            self.assertNotIn(
                summary["prs"][PR]["review"], ("pending", "claimed", "reviewing", "reconciling")
            )
            return []

        discovery = Mock()
        discovery.discover.side_effect = discover
        discovery.refresh.side_effect = lambda c: c
        # Stub reconciliation reads need the same pinned head and an empty thread.
        discovery.gh.request.return_value = dict(title="Test", head={"sha": "a" * 40})
        discovery.gh.thread.return_value = []
        with patch.dict(os.environ, REVIEW_AI_STUB="1", REVIEW_GUARDIAN_PLAIN="1"), patch.object(
            self.supervisor, "Discovery", return_value=discovery
        ):
            controller = self.supervisor.Supervisor(
                self.root, directory, mode="all", configuration={"test": True}, admission=8, wall=2
            )
            self.assertEqual(controller.run(coordinate=False), 0)
            self.assertEqual(calls.count("followup"), 1)
            self.assertEqual(set(calls), {"DevCallTopic", "DevCallEU", "AIReview", "followup"})
            self.assertEqual(len(read(directory / "run.json")["phases"]["labels"]["snapshots"]), 3)
            controller.discover_phase("labels")
            self.assertEqual(len(calls), 4)
            resumed = self.supervisor.Supervisor(self.root, directory)
            self.assertEqual(resumed.run(coordinate=False), 0)
            self.assertEqual(len(calls), 4)
            self.assertEqual(Store(self.root).current(PR)["generation"], 1)

    def test_explicit_pr_reference_forces_new_generation_at_same_head(self):
        store = Store(self.root)
        accepted(store)
        c = candidate(pr=PR, mode="pr")
        directory = self.root / "runs" / "explicit"
        with patch.dict(os.environ, REVIEW_AI_STUB="1"):
            controller = self.supervisor.Supervisor(self.root, directory, [c], mode="owner/repo#1")
            controller.initialize()
            controller.states[PR] = dict(review="pending", publish={}, comment="owed", board="owed")
            controller.claim_candidate(c)
            self.assertEqual(controller.states[PR]["review"], "claimed")
            self.assertEqual(controller.states[PR]["generation"], 2)
            controller.finish(PR, "deferred", "test completed")

    def test_resume_after_phase_snapshot_before_state_admission(self):
        store = Store(self.root)
        accepted(store)
        c = candidate(pr=PR, mode="followup")
        c["head"] = "d" * 40
        directory = self.root / "runs" / "phase-crash"
        with patch.dict(os.environ, REVIEW_AI_STUB="1", REVIEW_GUARDIAN_PLAIN="1"):
            controller = self.supervisor.Supervisor(self.root, directory, [c], mode="all", wall=2)
            controller.initialize()
            controller.config.update(
                active_phase="followup",
                phases={
                    "labels": {"state": "complete", "snapshots": {}},
                    "followup": {"state": "admitted", "snapshots": {"followup": [c]}},
                },
            )
            atomic(directory / "run.json", controller.config)
            atomic(
                directory / "state.json",
                {
                    PR: dict(
                        review="accepted",
                        generation=1,
                        phase="labels",
                        publish={},
                        comment="posted",
                        board="synced",
                    )
                },
            )
            resumed = self.supervisor.Supervisor(self.root, directory)
            self.assertEqual(resumed.run(coordinate=False), 0)
            self.assertEqual(store.current(PR)["generation"], 2)
            self.assertEqual(read(directory / "summary.json")["state"], "complete")

    def test_reused_archive_also_schedules_legacy_landing_with_all_labels(self):
        c = candidate(
            pr=PR,
            mode="AIReview",
            classification="REUSE",
            destinations=[
                "page:test/DevCallReviews/2026-09-28/AIReview/devcall_pr_reviews.html",
                "page:test/DevCallReviews/2026-09-28/DevCallTopic/devcall_pr_reviews.html",
            ],
        )
        with patch.dict(os.environ):
            os.environ.pop("REVIEW_AI_STUB", None)
            controller = self.supervisor.Supervisor(self.root, self.root / "runs" / "reuse", [c])
            controller.initialize()
            controller.project(c, "reuse")
        intents = [
            intent
            for path in (self.root / "operations").glob("*.json")
            for intent in read(path)["intents"]
        ]
        landing = [intent for intent in intents if intent.get("landing")]
        self.assertEqual(len(landing), 1)
        self.assertEqual(len(landing[0]["dependencies"]), 2)
        self.assertEqual(landing[0]["gate"], "page")

    def test_save_does_not_rewrite_unchanged_state_every_tick(self):
        directory = self.root / "runs" / "save"
        with patch.dict(os.environ, REVIEW_AI_STUB="1"):
            controller = self.supervisor.Supervisor(self.root, directory, [])
            controller.initialize()
            controller.save(force=True)
            before = (directory / "state.json").stat().st_mtime_ns
            with patch.object(self.supervisor, "atomic", wraps=atomic) as writes, patch.object(
                controller.store, "receipt_index", wraps=controller.store.receipt_index
            ) as scans:
                for _ in range(20):
                    controller.save()
                self.assertEqual(writes.call_count, 0)
                self.assertEqual(scans.call_count, 0)
            self.assertEqual((directory / "state.json").stat().st_mtime_ns, before)

    def test_refresh_observation_is_used_for_membership(self):
        directory = self.root / "runs" / "refresh"
        c = candidate(pr=PR, mode="AIReview", destinations=["page:stub/report.html"])
        with patch.dict(os.environ, REVIEW_AI_STUB="1"):
            controller = self.supervisor.Supervisor(self.root, directory, [c])
            controller.initialize()
            controller.states[PR] = dict(review="pending", publish={}, comment="owed", board="owed")
            controller.discovery = Mock()
            controller.discovery.refresh.return_value = dict(
                c, observation=99, classification="DROPPED"
            )
            controller.claim_candidate(c)
            patches = [
                patch
                for path in (self.root / "operations").glob("*.json")
                for intent in read(path)["intents"]
                for patch in intent.get("patches", {}).values()
            ]
            # journalled, or (changing nothing a page shows) merged directly
            row = controller.store.merge_membership("page:stub/report.html", {}).get(PR, {})
            self.assertTrue(any(p["ticket"] == 99 and p["removed"] for p in patches)
                            or (row.get("ticket") == 99 and row.get("removed")))


class GitHubHttp(unittest.TestCase):
    def setUp(self):
        from review_github import GitHub
        self.root = workspace(self)
        self.gh = GitHub(http_cache=self.root / "http-cache")
        self.gh._tokens["read"] = "t"
        self.script, self.sent = [], []
        test = self

        class Response:
            length = None

            def __init__(self, status, body=b"", headers=None, delay=0):
                self.status, self.body, self.headers, self.delay = status, body, headers or {}, delay

            def read1(self, size=None):
                time.sleep(self.delay)
                chunk, self.body = self.body, b""
                return chunk

            read = read1

            def getheader(self, name):
                return self.headers.get(name.lower())

        class Connection:
            sock = None

            def __init__(self, *a, **kw):
                self.timeout = kw.get("timeout")

            def request(self, method, url, headers):
                test.sent.append((url, dict(headers)))

            def getresponse(self):
                return Response(*test.script.pop(0))

            def close(self):
                pass

        patcher = patch.object(GitHub, "open_connection", lambda self, end: Connection(timeout=end - time.monotonic()))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_unchanged_answer_is_revalidated_not_refetched(self):
        self.script = [(200, b'{"n": 1}', {"etag": 'W/"a"'}), (304, b"", {})]
        self.assertEqual(self.gh.request("repos/o/r/pulls/1"), {"n": 1})
        self.assertEqual(self.gh.request("repos/o/r/pulls/1"), {"n": 1})
        self.assertNotIn("If-None-Match", self.sent[0][1])
        self.assertEqual(self.sent[1][1]["If-None-Match"], 'W/"a"')

    def test_a_spent_allowance_carries_its_reset_and_errors_map_as_gh_did(self):
        from review_github import RateLimited
        self.script = [(403, b"", {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1790000000"})]
        with self.assertRaises(RateLimited) as caught:
            self.gh.request("repos/o/r/pulls/1")
        self.assertEqual(caught.exception.reset, 1790000000.0)
        self.script = [(404, b"Not Found", {})]
        with self.assertRaises(OSError) as caught:
            self.gh.request("repos/o/r/pulls/2")
        self.assertIn("HTTP 404", str(caught.exception))
        self.assertEqual(len(self.sent), 2)                 # a 4xx is not retried
        self.script = [(502, b"", {}), (200, b'{"n": 3}', {})]
        with patch("review_github.time.sleep"):
            self.assertEqual(self.gh.request("repos/o/r/pulls/3"), {"n": 3})

    def test_redirects_are_followed_on_api_github_com_only(self):
        self.script = [(301, b"", {"location": "https://api.github.com/repositories/9/pulls/1"}),
                       (200, b'{"n": 1}', {})]
        self.assertEqual(self.gh.request("repos/o/old/pulls/1"), {"n": 1})
        self.assertEqual(self.sent[1][0], "/repositories/9/pulls/1")
        self.script = [(302, b"", {"location": "https://elsewhere.example/x"})]
        with self.assertRaises(OSError):
            self.gh.request("repos/o/r/pulls/2")

    def test_a_slow_answer_cannot_run_past_the_deadline(self):
        self.script = [(200, b'{"n": 1}', {}, 0.5)] * 3
        start = time.monotonic()
        with self.assertRaises((TimeoutError, OSError)):
            self.gh.request("repos/o/r/pulls/1", deadline=start + 0.3)
        self.assertLess(time.monotonic() - start, 1.0)

    def test_only_the_read_account_uses_it(self):
        with patch("review_github.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{"login": "bot"}', "")
            self.gh.request("user", account="comment")
        self.assertEqual(self.sent, [])
        self.assertEqual(run.call_count, 1)

    def test_writes_and_graphql_never_use_it(self):
        from review_github import GitHub
        gh = GitHub(http_cache=self.root / "c", writes=True)
        with patch("review_github.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{"data": {}}', "")
            gh.request("graphql", method="POST", payload={"query": "query { x }"})
            gh.request("repos/o/r/issues/1/comments", method="POST", payload={"body": "x"})
        self.assertEqual(self.sent, [])
        self.assertEqual(run.call_count, 2)


class GitHubTrickle(unittest.TestCase):
    def test_a_trickling_answer_from_a_real_socket_stops_at_the_deadline(self):
        import socket as sk
        import ssl
        import threading as th
        from review_github import GitHub
        server = sk.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def trickle():
            conn, _ = server.accept()
            conn.recv(65536)
            # a chunk-size line trickled a digit at a time and never ended:
            # one read keeps receiving bytes, so no socket timeout fires
            conn.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nTransfer-Encoding: chunked\r\n\r\n")
            try:
                for _ in range(40):
                    conn.sendall(b"0")
                    time.sleep(0.05)
            except OSError:
                pass
            conn.close()
        th.Thread(target=trickle, daemon=True).start()
        gh = GitHub(http_cache=workspace(self) / "c")
        gh._tokens["read"] = "t"
        real = http.client.HTTPConnection
        with patch.object(GitHub, "open_connection",
                          lambda self, end: real("127.0.0.1", port, timeout=end - time.monotonic())):
            start = time.monotonic()
            with self.assertRaises((TimeoutError, OSError)):
                gh.request("repos/o/r/pulls/1", deadline=start + 0.3)
        self.assertLess(time.monotonic() - start, 0.9)
        server.close()


class GitHubResolve(unittest.TestCase):
    def test_a_stuck_name_lookup_stops_at_the_deadline(self):
        from review_github import resolve
        with patch("review_github.socket.getaddrinfo", side_effect=lambda *a, **k: time.sleep(5)):
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                resolve("stuck.example", time.monotonic() + 0.2)
        self.assertLess(time.monotonic() - start, 0.6)


class GitHubConnect(unittest.TestCase):
    def test_every_address_is_tried_within_the_deadline(self):
        from review_github import _Pinned
        tried = []
        def connect(addr, timeout):
            tried.append(addr[0])
            raise OSError("unreachable")
        conn = _Pinned(["2001:db8::1", "192.0.2.1"], time.monotonic() + 1, None)
        with patch("review_github.socket.create_connection", side_effect=connect):
            with self.assertRaises(OSError):
                conn.connect()
        self.assertEqual(tried, ["2001:db8::1", "192.0.2.1"])

    def test_stuck_lookups_cannot_pile_up(self):
        import review_github
        with patch("review_github.socket.getaddrinfo", side_effect=lambda *a, **k: time.sleep(2)):
            for n in range(6):
                with self.assertRaises(TimeoutError):
                    review_github.resolve("stuck%d.example" % n, time.monotonic() + 0.05)
        alive = [t for t in threading.enumerate() if t.daemon and t.is_alive()]
        self.assertLessEqual(len(alive), 4 + 2)


class GitHubDeadline(unittest.TestCase):
    def test_retries_and_backoff_never_run_past_the_deadline(self):
        from review_github import GitHub
        timeouts = []
        def slow(*args, timeout=None, **kw):
            timeouts.append(timeout)
            time.sleep(min(timeout, 0.5))
            raise subprocess.TimeoutExpired(args[0], timeout)
        start = time.monotonic()
        with patch("review_github.subprocess.run", side_effect=slow):
            with self.assertRaises(TimeoutError):
                GitHub().request("repos/o/r/pulls/1", deadline=start + 3)
        self.assertLess(time.monotonic() - start, 3.5)
        self.assertTrue(all(t <= 3 for t in timeouts))


if __name__ == "__main__":
    unittest.main()
