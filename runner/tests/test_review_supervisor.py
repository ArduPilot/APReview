#!/usr/bin/env python3
"""Bounded subprocess runs cover admission, retries, recovery and overlap."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from review_fixtures import BIN, PR, candidate, python, stop, until, workspace
from review_lock import try_lock
from review_store import Store, StubAdapter, atomic, digest, read

SPEC = importlib.util.spec_from_file_location("review_supervisor", BIN / "review-supervisor.py")
SUPERVISOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUPERVISOR)


class ReviewSupervisor(unittest.TestCase):
    def setUp(self):
        self.root = workspace(self)
        self.store = Store(self.root)
        self.env = dict(os.environ, REVIEW_AI_STUB="1", REVIEW_GUARDIAN_PLAIN="1", PYTHONDONTWRITEBYTECODE="1")

    def start(self, name, candidates=None, admission=4, mode="candidates", resume=False, extra=()):
        directory = self.root / "runs" / name
        args = [sys.executable, str(BIN / "review-supervisor.py"), mode, "--data", str(self.root)]
        if resume:
            args += ["--resume", str(directory)]
        else:
            path = self.root / (name + "-candidates.json")
            atomic(path, candidates)
            args += ["--run", str(directory), "--candidates", str(path), "--admission", str(admission),
                     "--wall", "3", "--permit-timeout", "2"]
        log = self.root / (name + ("-resume" if resume else "") + ".log")
        with open(log, "wb") as stream:
            child = subprocess.Popen(args + list(extra), env=self.env, stdout=stream, stderr=stream)
        def cleanup():
            from review_guardian import cleanup_attempt
            atomic(directory / "abort.json", {"requested": True})
            stop(child)
            for path in (directory / "attempts").glob("*/job.json"):
                cleanup_attempt(path.parent, time.monotonic() + 3)
        self.addCleanup(cleanup)
        return child, directory, log

    def finish(self, child, directory, log):
        self.assertEqual(child.wait(timeout=20), 0, log.read_text())
        summary = read(directory / "summary.json")
        self.assertIsNotNone(summary, log.read_text())
        return summary

    def running(self, directory, count=1):
        def active():
            records = [read(p) for p in (directory / "attempts").glob("*/status.json")]
            return records if sum(x["state"] == "running" for x in records) >= count else None
        return until(self, active)

    def test_frozen_quota_blocks_inference_but_drains_accepted_delivery(self):
        from review_fixtures import complete_claim
        with try_lock(self.store.locks, PR) as lock:
            claim = complete_claim(self.store, lock)
            self.store.accept(lock, PR, claim, [dict(kind="publish", target="page:test/index.html")])
        cfg = self.root / "configuration.json"
        atomic(cfg, {"quota": {"paused": True}})
        child, directory, log = self.start("quota", [candidate(2)], extra=["--config", str(cfg)])
        summary = self.finish(child, directory, log)
        self.assertEqual(summary["prs"]["pr:owner/repo#2"].get("reason"), "quota paused")
        self.assertFalse(list((directory / "attempts").glob("*/job.json")))
        self.assertTrue(list((self.root / "receipts").glob("*.json")))
        self.assertFalse(list((self.root / "outbox").glob("*.json")))

    def test_routing_is_rechecked_before_a_new_claim(self):
        from review_routing import DEFAULT
        site = self.root / "site"
        atomic(site / "etc/routing.json", DEFAULT)
        cfg = self.root / "configuration.json"
        atomic(cfg, {"routing_root": str(site)})
        child, directory, log = self.start("transferred", [candidate(destinations=["page:test/index.html"])], extra=["--config", str(cfg)])
        summary = self.finish(child, directory, log)
        self.assertEqual(summary["prs"][PR].get("reason"), "ownership transferred")
        self.assertFalse(list((self.root / "operations").glob("*.json")))
        self.assertFalse(list((directory / "attempts").glob("*/job.json")))

    def test_paused_discovery_cannot_create_delivery_debt(self):
        from review_routing import DEFAULT
        site = self.root / "site"
        atomic(site / "etc/routing.json", dict(DEFAULT, repositories=["owner/repo"]))
        cfg = self.root / "configuration.json"
        atomic(cfg, {"routing_root": str(site)})
        with try_lock(self.store.locks, "pause"):
            child, directory, log = self.start("pause-projection", [candidate(destinations=["page:test/index.html"])],
                                               admission=.3, extra=["--config", str(cfg)])
            summary = self.finish(child, directory, log)
        self.assertEqual(summary["prs"][PR].get("reason"), "paused")
        self.assertFalse(list((self.root / "operations").glob("*.json")))

    def test_imported_generation_is_retained_and_next_review_updates_its_page(self):
        from review_handoff import import_manifest
        from review_store import digest
        pr = "pr:rsyncproject/rsync#1"
        target = "page:review/RsyncReviews/index.html"
        import_manifest(self.store, "rsyncproject/rsync",
                        {pr: dict(head="a" * 10, section='<section id="pr1">previous section</section>')}, target, {})
        row = dict(candidate(), repository="rsyncproject/rsync", head="d" * 40)
        child, directory, log = self.start("after-import", [row])
        summary = self.finish(child, directory, log)
        self.assertEqual(summary["prs"][pr]["review"], "accepted")
        chain = list(self.store.chain(pr))
        self.assertEqual([b["generation"] for b in chain], [1, 0])
        self.assertTrue(chain[-1]["legacy"])
        membership = read(self.root / "membership" / (digest(target) + ".json"))
        self.assertEqual(membership[pr]["generation"], 1)
        self.assertTrue(any(i["kind"] == "publish" and i["target"] == target for i in chain[0]["intents"]))

    def test_four_pass_order_and_independent_delivery_tracks(self):
        child, directory, log = self.start("one", [candidate(post=True, stub={"primary": {"sleep": 0.3}, "cold": {"sleep": 0.7}})])
        self.running(directory, 2)
        summary = self.finish(child, directory, log)
        self.assertEqual(summary["prs"][PR]["review"], "accepted")
        self.assertEqual(self.store.current(PR)["generation"], 1)
        jobs = {read(p)["kind"]: read(p) for p in (directory / "attempts").glob("*/job.json")}
        self.assertEqual(set(jobs), set(SUPERVISOR.KINDS))
        self.assertLess(jobs["cold"]["registered"], jobs["validation"]["registered"])
        self.assertLess(jobs["validation"]["registered"], jobs["reconciliation"]["registered"])
        self.assertNotIn("primary_result", jobs["cold"])
        self.assertIn("primary_result", jobs["validation"])
        self.assertEqual(set(jobs["reconciliation"]["results"]), {"primary", "cold", "validation"})
        self.assertIn(summary["prs"][PR]["comment"], ("owed", "posted"))
        self.assertIn("delivery_deferred", summary)

    def test_busy_pr_does_not_hold_up_another_candidate(self):
        lock = try_lock(self.store.locks, PR)
        self.addCleanup(lock.close)
        child, directory, log = self.start("busy", [candidate(), candidate(2)], admission=1.5)
        summary = self.finish(child, directory, log)
        self.assertEqual(summary["prs"][PR]["review"], "deferred")
        self.assertEqual(summary["prs"][PR]["reason"], "PR busy")
        self.assertEqual(summary["prs"]["pr:owner/repo#2"]["review"], "accepted")

    def test_retry_keeps_generation_and_preserves_other_passes(self):
        child, directory, log = self.start("retry", [candidate(stub={"primary": [{"exit": 4}, {}]})])
        summary = self.finish(child, directory, log)
        self.assertEqual(summary["prs"][PR]["review"], "accepted")
        jobs = [read(p) for p in (directory / "attempts").glob("*/job.json")]
        self.assertEqual(sum(j["kind"] == "primary" for j in jobs), 2)
        self.assertEqual(sum(j["kind"] == "cold" for j in jobs), 1)
        self.assertEqual({j["generation"] for j in jobs}, {1})

    def test_a_filtered_codex_pass_is_retried_on_the_fallback_model(self):
        child, directory, log = self.start("refuse", [candidate(stub={"cold": [{"refuse": True}, {}]})])
        summary = self.finish(child, directory, log)
        self.assertEqual(summary["prs"][PR]["review"], "accepted", log.read_text())
        colds = sorted((read(p) for p in (directory / "attempts").glob("*/job.json") if read(p)["kind"] == "cold"),
                       key=lambda j: j["registered"])
        self.assertEqual([j.get("refused_before", False) for j in colds], [False, True])

    def test_an_incomplete_pass_is_accepted_on_its_last_try(self):
        # a PR too large to cover in one pass still gets a review, gaps named
        child, directory, log = self.start("partial", [candidate(stub={"cold": {"incomplete": True}})])
        summary = self.finish(child, directory, log)
        self.assertEqual(summary["prs"][PR]["review"], "accepted", log.read_text())
        colds = [p for p in (directory / "attempts").glob("*/job.json") if read(p)["kind"] == "cold"]
        self.assertEqual(len(colds), 2)

    def test_an_incomplete_reconciliation_is_never_accepted(self):
        child, directory, log = self.start("partialfinal", [candidate(stub={"reconciliation": {"incomplete": True}})])
        summary = self.finish(child, directory, log)
        self.assertEqual(summary["prs"][PR]["review"], "deferred")
        self.assertIsNone(self.store.current(PR))

    def test_a_pass_gets_more_time_on_a_larger_diff(self):
        self.assertEqual(SUPERVISOR.scaled_wall(1800, "x\n" * 500), 1800)
        self.assertEqual(SUPERVISOR.scaled_wall(1800, "x\n" * 20000), 3600)
        self.assertEqual(SUPERVISOR.scaled_wall(1800, "x\n" * 90000), 5400)
        self.assertEqual(SUPERVISOR.scaled_wall(1800, None), 1800)

    def test_a_rerun_of_a_deferred_pr_pays_only_for_what_was_left(self):
        child, directory, log = self.start("first", [candidate(stub={"reconciliation": {"invalid": True}})])
        summary = self.finish(child, directory, log)
        self.assertEqual(summary["prs"][PR]["review"], "deferred")
        child, again, log = self.start("second", [candidate()])
        summary = self.finish(child, again, log)
        self.assertEqual(summary["prs"][PR]["review"], "accepted", log.read_text())
        kinds = sorted(read(p)["kind"] for p in (again / "attempts").glob("*/job.json"))
        self.assertEqual(kinds, ["reconciliation"])
        self.assertEqual(summary["prs"][PR].get("carried"), ["cold", "primary", "validation"])

    def test_two_failures_defer_without_acceptance(self):
        child, directory, log = self.start("fail", [candidate(stub={"primary": {"invalid": True}})])
        summary = self.finish(child, directory, log)
        self.assertEqual(summary["prs"][PR]["review"], "deferred")
        self.assertIsNone(self.store.current(PR))
        jobs = [read(p) for p in (directory / "attempts").glob("*/job.json")]
        self.assertEqual(sum(j["kind"] == "primary" for j in jobs), 2)
        self.assertFalse(any(j["kind"] == "reconciliation" for j in jobs))

    def test_a_guardian_that_cannot_start_defers_the_pr_not_the_run(self):
        # cron without the user bus: systemd-run fails for every attempt
        shim = self.root / "shim"
        shim.mkdir()
        (shim / "systemd-run").write_text("#!/bin/sh\necho 'Failed to connect to user scope bus' >&2\nexit 1\n")
        (shim / "systemd-run").chmod(0o755)
        self.env = dict(self.env, PATH=str(shim) + os.pathsep + self.env["PATH"])
        del self.env["REVIEW_GUARDIAN_PLAIN"]
        child, directory, log = self.start("nobus", [candidate(), candidate(2)])
        summary = self.finish(child, directory, log)
        self.assertEqual({v["review"] for v in summary["prs"].values()}, {"deferred"})
        self.assertNotIn("Traceback", log.read_text())

    def test_prs_share_an_account_up_to_its_slot_cap(self):
        # four slots per account: two PRs' primaries run at the same time
        slow = {"primary": {"sleep": 1.5}}
        child, directory, log = self.start("shared", [candidate(stub=slow), candidate(2, stub=slow)], admission=60)
        records = self.running(directory, 3)
        self.assertGreaterEqual(sum(r["kind"] == "primary" and r["state"] == "running" for r in records), 2)
        summary = self.finish(child, directory, log)
        self.assertEqual({v["review"] for v in summary["prs"].values()}, {"accepted"})
        slots = {read(p).get("account_slot") for p in (directory / "attempts").glob("*/status.json")
                 if read(p)["kind"] == "primary"}
        self.assertEqual(len(slots), 2, slots)

    def test_passes_never_outnumber_the_permits_left_for_them(self):
        # pool 3 keeps slot 0 for finishing passes: at most two primaries or
        # colds at once, so none is launched only to wait out a permit
        # each pass outlasts the two second permit wait, inside the wall
        slow = {kind: {"sleep": 2.5} for kind in ("primary", "cold")}
        rows = [candidate(n, stub=slow) for n in (1, 2, 3)]
        child, directory, log = self.start("pool", rows, admission=60, extra=["--pool-size", "3"])
        summary = self.finish(child, directory, log)
        statuses = [read(p) for p in (directory / "attempts").glob("*/status.json")]
        self.assertEqual([s.get("error") for s in statuses if s.get("error")], [])
        self.assertEqual({v["review"] for v in summary["prs"].values()}, {"accepted"},
                         {k: (v["review"], v.get("reason")) for k, v in summary["prs"].items()})

    def test_another_runs_passes_hold_the_permits_it_waits_for(self):
        # a second run's controller cannot see the first's passes in its own
        # state; it probes the pool instead of launching into a full one
        slow = {kind: {"sleep": 2.5} for kind in ("primary", "cold")}
        first, first_dir, first_log = self.start("first", [candidate(n, stub=slow) for n in (1, 2)],
                                                 admission=60, extra=["--pool-size", "3"])
        self.running(first_dir, 4)
        second, second_dir, second_log = self.start("second", [candidate(n, stub=slow) for n in (3, 4)],
                                                    admission=60, extra=["--pool-size", "3"])
        self.finish(first, first_dir, first_log)
        self.finish(second, second_dir, second_log)
        statuses = [read(p) for p in (second_dir / "attempts").glob("*/status.json")]
        self.assertEqual([s.get("error") for s in statuses if s.get("error")], [])

    def test_a_held_exclusive_account_waits_instead_of_spending_tries(self):
        # two PRs, one exclusive account per provider: the second PR must wait
        # for the lease, not burn its two tries on account deadlines
        self.env["REVIEW_STUB_EXCLUSIVE"] = "1"
        slow = {kind: {"sleep": 1.5} for kind in ("primary", "cold")}
        child, directory, log = self.start("exclusive", [candidate(stub=slow), candidate(2, stub=slow)],
                                           admission=60, extra=["--permit-timeout", "1"])
        summary = self.finish(child, directory, log)
        self.assertEqual({v["review"] for v in summary["prs"].values()}, {"accepted"}, log.read_text())
        statuses = [read(p) for p in (directory / "attempts").glob("*/status.json")]
        # the second PR waits unlaunched; it does not churn attempts that
        # wait out the lease and prepare a worktree each time
        self.assertEqual([s.get("error") for s in statuses if s.get("error")], [])

    def test_resume_monitors_surviving_guardians_and_reuses_completed_passes(self):
        child, directory, log = self.start("resume", [candidate(stub={"primary": {"sleep": 0.6}, "cold": {"sleep": 0.6}})], admission=8)
        self.running(directory, 2)
        child.kill()
        child.wait(timeout=5)
        config = read(directory / "run.json")
        config["admission_deadline"] = time.time() - 1
        atomic(directory / "run.json", config)
        self.assertIsNone(try_lock(self.store.locks, PR))
        resumed, directory, log = self.start("resume", resume=True)
        summary = self.finish(resumed, directory, log)
        self.assertEqual(summary["prs"][PR]["review"], "accepted")
        self.assertEqual(len(list((directory / "attempts").glob("*/job.json"))), 4)
        self.assertEqual(self.store.claim(PR)["counter"], 1)

    def test_two_runs_overlap_one_pr_without_duplicate_review(self):
        first, first_dir, first_log = self.start("first", [candidate(stub={"primary": {"sleep": 0.4}})])
        self.running(first_dir)
        second, second_dir, second_log = self.start("second", [candidate(), candidate(2)], admission=8)
        a = self.finish(first, first_dir, first_log)
        b = self.finish(second, second_dir, second_log)
        self.assertEqual(a["prs"][PR]["review"], "accepted")
        self.assertEqual(b["prs"][PR]["review"], "reused")
        self.assertEqual(b["prs"]["pr:owner/repo#2"]["review"], "accepted")
        self.assertEqual(self.store.claim(PR)["counter"], 1)

    def test_run_lock_and_pause_probe(self):
        pause = try_lock(self.store.locks, "pause")
        self.addCleanup(pause.close)
        child, directory, log = self.start("pause", [candidate()], admission=0.4)
        summary = self.finish(child, directory, log)
        self.assertEqual(summary["prs"][PR]["reason"], "paused")
        self.assertFalse(list((directory / "attempts").glob("*/job.json")))
        pause.close()
        lock = try_lock(self.store.locks, "run:" + str(directory))
        self.addCleanup(lock.close)
        child, _, log = self.start("pause", resume=True)
        self.assertEqual(child.wait(timeout=5), 75, log.read_text())

    def test_reread_uses_fresh_head_and_pr_requests_force_once(self):
        child, directory, log = self.start("fresh", [candidate(live={"head": "d" * 40})], mode="pr")
        self.finish(child, directory, log)
        self.assertEqual(self.store.bundle(PR)["inputs"]["head"], "d" * 40)
        resumed, directory, log = self.start("fresh", resume=True)
        self.finish(resumed, directory, log)
        self.assertEqual(self.store.claim(PR)["counter"], 1)
        child, directory, log = self.start("new-request", [candidate(live={"head": "d" * 40})], mode="pr")
        self.finish(child, directory, log)
        self.assertEqual(self.store.claim(PR)["counter"], 2)

    def test_oldest_first_with_canonical_tie_breaker(self):
        candidates = [candidate(3), candidate(2), candidate(1)]
        candidates[0]["created_at"] = "2019-01-01"
        child, directory, log = self.start("ordered", candidates)
        self.finish(child, directory, log)
        ordered = read(directory / "run.json")["candidates"]
        self.assertEqual([c["number"] for c in ordered], [3, 1, 2])
        jobs = sorted([read(p) for p in (directory / "attempts").glob("*/job.json") if read(p)["kind"] == "primary"], key=lambda x: x["registered"])
        self.assertEqual([j["number"] for j in jobs], [3, 1, 2])

    def test_admission_refreshes_are_prefetched_together_and_expire(self):
        supervisor = SUPERVISOR.Supervisor.__new__(SUPERVISOR.Supervisor)
        supervisor.config = {"configuration": {"discovery_workers": 4}}
        supervisor.store = self.store
        supervisor.discovery = Mock()
        supervisor.discovery.refresh.side_effect = lambda c: dict(c, fresh=True)
        rows = [dict(candidate(n), pr="pr:owner/repo#%d" % n) for n in (1, 2, 3)]
        supervisor.prefetch(rows)
        self.assertEqual(supervisor.discovery.refresh.call_count, 3)
        self.assertTrue(supervisor.prefetched_refresh(rows[0]["pr"])["fresh"])
        # taken once; a second claim refreshes for itself
        self.assertIsNone(supervisor.prefetched_refresh(rows[0]["pr"]))
        # a stale prefetch is not used
        supervisor.prefetched[rows[1]["pr"]] = (time.monotonic() - 120, {"stale": True})
        self.assertIsNone(supervisor.prefetched_refresh(rows[1]["pr"]))
        # a PR another pass holds is not fetched while it waits: doing so
        # every minute spent the hourly GitHub budget
        holder = python("import sys,time; from review_lock import try_lock; l = try_lock(sys.argv[1], sys.argv[2]); "
                        "print('held', flush=True); time.sleep(30)", self.store.locks, "pr:owner/repo#5",
                        stdout=subprocess.PIPE, text=True)
        self.addCleanup(stop, holder)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        supervisor.discovery.refresh.reset_mock()
        supervisor.prefetch([dict(candidate(5), pr="pr:owner/repo#5")])
        supervisor.discovery.refresh.assert_not_called()
        # a failed prefetch leaves the claim to refresh and report
        supervisor.discovery.refresh.side_effect = OSError("gone")
        supervisor.prefetch([dict(candidate(4), pr="pr:owner/repo#4")])
        self.assertIsNone(supervisor.prefetched_refresh("pr:owner/repo#4"))

    def test_a_rate_limited_claim_waits_for_the_reset_instead_of_deferring(self):
        from review_github import RateLimited
        supervisor = SUPERVISOR.Supervisor.__new__(SUPERVISOR.Supervisor)
        supervisor.config = {"configuration": {}, "stub": False}
        supervisor.store = self.store
        supervisor.states = {PR: {"review": "pending", "candidate": dict(candidate(), pr=PR)}}
        supervisor.owned, supervisor.next_claim, supervisor.backoff = {}, {}, {}
        supervisor.discovery = Mock()
        reset = time.time() + 600
        supervisor.discovery.refresh.side_effect = RateLimited("API rate limit exceeded", reset)
        supervisor.finish = Mock()
        supervisor.claim_candidate(dict(candidate(), pr=PR))
        supervisor.finish.assert_not_called()
        self.assertGreaterEqual(supervisor.next_claim[PR], reset)
        self.assertNotIn(PR, supervisor.owned)
        # the PR's lock was given back for whoever needs it next
        lock = try_lock(self.store.locks, PR)
        self.assertIsNotNone(lock)
        lock.close()

    def test_a_failing_discovery_is_retried_then_left_empty_not_fatal(self):
        supervisor = SUPERVISOR.Supervisor.__new__(SUPERVISOR.Supervisor)
        supervisor.config = {"phases": {}, "mode": "followup"}
        supervisor.discovery = Mock()
        supervisor.discovery.discover.side_effect = [OSError("network is unreachable"), [], ]
        with patch.object(SUPERVISOR, "DISCOVERY_RETRY", 0):
            try:
                supervisor.discover_phase("initial")
            except (AttributeError, KeyError, TypeError):
                pass   # the bare supervisor lacks the rest of a phase's state
        self.assertEqual(supervisor.discovery.discover.call_count, 2)
        supervisor.config["phases"] = {}
        supervisor.discovery.discover.reset_mock()
        supervisor.discovery.discover.side_effect = OSError("network is unreachable")
        with patch.object(SUPERVISOR, "DISCOVERY_RETRY", 0):
            try:
                supervisor.discover_phase("initial")
            except (AttributeError, KeyError, TypeError):
                pass
        self.assertEqual(supervisor.discovery.discover.call_count, 3)

    def test_acceptance_waits_on_discoverys_projection_only_if_discovery_journalled_it(self):
        supervisor = SUPERVISOR.Supervisor.__new__(SUPERVISOR.Supervisor)
        supervisor.store = Store(self.root / "data")
        supervisor.run_id = str(self.root / "run")
        supervisor.config = {"stub": True, "mode": "followup", "phases": {}, "configuration": {}}
        page = "page:stub/end/a.html"
        c = dict(candidate(), pr=PR, destinations=[page])
        projection = lambda: [x for x in supervisor.intents(c, 1) if x["kind"] == "projection"][0]
        self.assertEqual(projection()["dependencies"], [])
        supervisor.store.journal(supervisor.run_id, "discovery", PR,
                                 [{"kind": "projection", "target": page, "gate": "page", "patches": {}}])
        self.assertEqual(len(projection()["dependencies"]), 1)

    def test_an_observation_that_changes_nothing_visible_is_merged_not_journalled(self):
        supervisor = SUPERVISOR.Supervisor.__new__(SUPERVISOR.Supervisor)
        supervisor.store = Store(self.root / "data")
        supervisor.run_id = str(self.root / "run")
        supervisor.config = {"stub": True, "mode": "followup", "phases": {}, "configuration": {},
                             "observation": 0}
        page = "page:stub/end/a.html"
        ops = lambda: sorted((supervisor.store.root / "operations").glob("*.json"))
        rows = lambda: supervisor.store.merge_membership(page, {})
        c = dict(candidate(), pr=PR, destinations=[page], ci={"state": "success", "at": "2026-10-04T01:00"})
        supervisor._project(dict(c, observation=1), "discovery", quiet=True)
        self.assertEqual(len(ops()), 1)                 # new on the page: journalled
        supervisor.store.drain(StubAdapter(supervisor.store.root))
        # observed again: same CI state the same day, so the page is unchanged
        supervisor.run_id = str(self.root / "run2")
        supervisor._project(dict(c, observation=2, ci={"state": "success", "at": "2026-10-04T05:00"}),
                            "discovery", quiet=True)
        self.assertEqual(len(ops()), 1)
        self.assertEqual(rows()[PR]["ticket"], 2)       # but its ticket still fences
        # an older queued removal arriving late cannot undo it
        supervisor.store.merge_membership(page, {PR: {"ticket": 1, "removed": True}})
        self.assertFalse(rows()[PR]["removed"])
        # a visible change is journalled
        supervisor.run_id = str(self.root / "run3")
        supervisor._project(dict(c, observation=3, ci={"state": "failure", "at": "2026-10-04T06:00"}),
                            "discovery", quiet=True)
        self.assertEqual(len(ops()), 2)
        # and a visible change is never written ahead of its journal, so a
        # crash before journalling leaves the change still to be made
        self.assertEqual(rows()[PR]["ci"]["state"], "success")

    def test_a_page_change_outside_membership_is_published_at_the_next_observation(self):
        supervisor = SUPERVISOR.Supervisor.__new__(SUPERVISOR.Supervisor)
        supervisor.store = Store(self.root / "data")
        supervisor.run_id = str(self.root / "run")
        supervisor.config = {"stub": True, "mode": "followup", "phases": {}, "configuration": {},
                             "observation": 0}
        page = "page:stub/end/a.html"
        ops = lambda: len(list((supervisor.store.root / "operations").glob("*.json")))
        ci = {"state": "success", "at": "2026-10-04T01:00"}
        c = dict(candidate(), pr=PR, destinations=[page], ci=ci)
        supervisor._project(dict(c, observation=1), "discovery", quiet=True)
        supervisor.store.drain(StubAdapter(supervisor.store.root))
        supervisor.run_id = str(self.root / "run2")
        supervisor._project(dict(c, observation=2), "discovery", quiet=True)
        self.assertEqual(ops(), 1)                       # unchanged: suppressed
        # a claim appears after the page was published: what it shows differs
        # from what it was last asked to show, so the next observation publishes
        atomic(supervisor.store.pr_dir(PR) / "claim.json",
               dict(generation=1, status="deferred", attempts=[], selected={}))
        supervisor.run_id = str(self.root / "run3")
        supervisor._project(dict(c, observation=3), "discovery", quiet=True)
        self.assertEqual(ops(), 2)

    def quiet_supervisor(self):
        supervisor = SUPERVISOR.Supervisor.__new__(SUPERVISOR.Supervisor)
        supervisor.store = Store(self.root / "data")
        supervisor.run_id = str(self.root / "run0")
        supervisor.config = {"stub": True, "mode": "followup", "phases": {}, "observation": 0,
                             "configuration": {"endpoints": {"stub": {"publish": "A", "url": "http://a"}}}}
        return supervisor

    def observe(self, supervisor, c, n, **changes):
        supervisor.run_id = str(self.root / ("run%d" % n))
        before = len(list((supervisor.store.root / "operations").glob("*.json")))
        supervisor._project(dict(c, observation=n, **changes), "discovery", quiet=True)
        adapter = StubAdapter(supervisor.store.root)
        adapter.destination_of = lambda e: supervisor.destination(e["target"])
        supervisor.store.drain(adapter)
        return len(list((supervisor.store.root / "operations").glob("*.json"))) - before

    def test_a_row_without_a_verified_view_always_publishes(self):
        supervisor = self.quiet_supervisor()
        page = "page:stub/end/a.html"
        # a row from before records existed, then a per-page removal
        supervisor.store.merge_membership(page, {PR: {"ticket": 1, "removed": False}})
        c = dict(candidate(), pr=PR, destinations=[page], membership_removed={page: True})
        self.assertEqual(self.observe(supervisor, c, 2), 1)

    def test_a_busy_page_journals_and_a_return_to_the_old_state_publishes(self):
        supervisor = self.quiet_supervisor()
        page = "page:stub/end/a.html"
        ok, bad = {"state": "success", "at": "2026-10-04T01:00"}, {"state": "failure", "at": "2026-10-04T02:00"}
        c = dict(candidate(), pr=PR, destinations=[page])
        self.observe(supervisor, c, 1, ci=ok)
        with patch.object(SUPERVISOR, "try_lock", return_value=None):
            self.assertEqual(self.observe(supervisor, c, 2, ci=bad), 1)
        # back to passing: the page shows failing, so this must publish
        self.assertEqual(self.observe(supervisor, c, 3, ci=ok), 1)
        self.assertEqual(self.observe(supervisor, c, 4, ci=ok), 0)

    def test_a_new_destination_publishes_even_when_nothing_else_changed(self):
        supervisor = self.quiet_supervisor()
        page = "page:stub/end/a.html"
        c = dict(candidate(), pr=PR, destinations=[page])
        self.observe(supervisor, c, 1)
        self.assertEqual(self.observe(supervisor, c, 2), 0)
        supervisor.config["configuration"]["endpoints"]["stub"]["url"] = "http://b"
        self.assertEqual(self.observe(supervisor, c, 3), 1)

    def test_a_delayed_projection_cannot_make_a_stale_view_match(self):
        # Codex's sequence: publish passing; journal failing and leave it
        # queued; observe passing (matches, merged); drain the failing
        # projection; observe failing again. The page must end up failing.
        supervisor = self.quiet_supervisor()
        page = "page:stub/end/a.html"
        ok, bad = {"state": "success", "at": "2026-10-04T01:00"}, {"state": "failure", "at": "2026-10-04T02:00"}
        c = dict(candidate(), pr=PR, destinations=[page])
        self.observe(supervisor, c, 1, ci=ok)
        supervisor.run_id = str(self.root / "late")
        supervisor._project(dict(c, observation=2, ci=bad), "discovery", quiet=True)   # queued
        supervisor.run_id = str(self.root / "run3")
        supervisor._project(dict(c, observation=3, ci=ok), "discovery", quiet=True)    # matches
        supervisor.store.drain(StubAdapter(supervisor.store.root))                      # delayed one lands
        self.observe(supervisor, c, 4, ci=bad)
        rows = supervisor.store.merge_membership(page, {})
        self.assertEqual(rows[PR]["ci"]["state"], "failure")
        self.assertEqual(rows[PR]["published"]["view"][1], "failure")

    def test_a_view_from_an_older_upload_or_no_destination_never_matches_live(self):
        supervisor = self.quiet_supervisor()
        supervisor.config["stub"] = False
        page = "page:stub/end/a.html"
        ci = {"state": "success", "at": "2026-10-04T01:00"}
        supervisor.store.merge_membership(page, {PR: {"ticket": 1, "removed": False, "ci": ci,
                                                      "progress": "discovery"}})
        epoch = supervisor.store.root / "pages" / digest(page) / "epoch.json"
        def record(**published):
            rows = supervisor.store.merge_membership(page, {})
            rows[PR]["published"] = dict(view=supervisor.view(rows[PR], PR), **published)
            with try_lock(supervisor.store.locks, page) as lock:
                supervisor.store.write_membership(lock, page, rows)
        c = dict(candidate(), pr=PR, destinations=[page], ci=ci)
        examine = lambda n: supervisor.examine(page, PR, {"ticket": n, "removed": False, "ci": ci,
                                                          "progress": "discovery"}, True)
        atomic(epoch, 3)
        record(destination=supervisor.destination(page), epoch=3)
        self.assertTrue(examine(2))
        # another upload since (an annotation, a comment repair, or one that
        # died before recording): stale
        atomic(epoch, 4)
        self.assertFalse(examine(3))
        # a stub-recorded view has no destination: never enough for a live run
        record(destination=None, epoch=4)
        self.assertFalse(examine(4))

    def test_a_partly_suppressed_discovery_resumes_and_feeds_acceptance(self):
        supervisor = SUPERVISOR.Supervisor.__new__(SUPERVISOR.Supervisor)
        supervisor.store = Store(self.root / "data")
        supervisor.run_id = str(self.root / "run")
        supervisor.config = {"stub": True, "mode": "followup", "phases": {}, "configuration": {},
                             "observation": 0}
        a, b = "page:stub/end/a.html", "page:stub/end/b.html"
        ci = {"state": "success", "at": "2026-10-04T01:00"}
        supervisor.store.merge_membership(a, {PR: {"ticket": 1, "removed": False, "ci": ci,
                                                   "progress": "discovery"}})
        rows = supervisor.store.merge_membership(a, {})
        rows[PR]["published"] = {"view": supervisor.view(rows[PR], PR), "destination": supervisor.destination(a)}
        with try_lock(supervisor.store.locks, a) as lock:
            supervisor.store.write_membership(lock, a, rows)
        c = dict(candidate(), pr=PR, destinations=[a, b], ci=ci, observation=2)
        supervisor._project(c, "discovery", quiet=True)     # a unchanged, b new
        [op] = (supervisor.store.root / "operations").glob("*.json")
        self.assertEqual([i["target"] for i in read(op)["intents"] if i["kind"] == "projection"], [b])
        # a resumed controller replays discovery: no identity error, no change
        supervisor._project(c, "discovery", quiet=True)
        self.assertEqual(len(list((supervisor.store.root / "operations").glob("*.json"))), 1)
        # acceptance waits on discovery's projection of b only
        deps = {x["target"]: x["dependencies"] for x in supervisor.intents(c, 1) if x["kind"] == "projection"}
        self.assertEqual(deps[a], [])
        self.assertEqual(len(deps[b]), 1)

    def test_a_controller_that_does_not_deliver_still_recovers(self):
        supervisor = SUPERVISOR.Supervisor.__new__(SUPERVISOR.Supervisor)
        supervisor.directory = self.root
        supervisor.store = Mock()
        supervisor.store.recover.return_value = None
        supervisor.store.last_recovered = 0
        supervisor.adapter = None
        supervisor.states = {}
        supervisor.config = {"configuration": {"controller_delivers": False}}
        supervisor.bounded_drain()
        supervisor.store.recover.assert_called_once()
        supervisor.store.drain.assert_not_called()
        supervisor.config = {"configuration": {}}       # older frozen runs still deliver
        supervisor.bounded_drain()
        supervisor.store.drain.assert_called_once()

    def test_a_pr_discovery_just_settled_is_not_read_again_at_admission(self):
        supervisor = SUPERVISOR.Supervisor.__new__(SUPERVISOR.Supervisor)
        fresh = dict(classification="REUSE", discovered_at=time.time())
        self.assertTrue(supervisor.settled_at_discovery(fresh))
        self.assertTrue(supervisor.settled_at_discovery(dict(fresh, classification="DROPPED")))
        # a PR going to review is always read again before its first pass
        self.assertFalse(supervisor.settled_at_discovery(dict(fresh, classification="REVIEW")))
        # and an old discovery read is not trusted
        self.assertFalse(supervisor.settled_at_discovery(dict(fresh, discovered_at=time.time() - 7200)))

    def test_finishing_marks_and_clears_pending_work(self):
        supervisor = SUPERVISOR.Supervisor.__new__(SUPERVISOR.Supervisor)
        supervisor.store = Mock()
        supervisor.states = {PR: {"review": "pending"}}
        supervisor.config = {"candidates": [], "configuration": {}}
        supervisor.owned = {}
        supervisor.finish(PR, "deferred", "quota paused")
        supervisor.store.mark_pending.assert_called_once_with(PR, "quota paused")
        supervisor.finish(PR, "dropped", "label removed")
        supervisor.store.clear_pending.assert_not_called()
        supervisor.finish(PR, "reused")
        supervisor.store.clear_pending.assert_called_once_with(PR)

    def test_save_reads_only_the_receipts_a_generation_names(self):
        supervisor = SUPERVISOR.Supervisor.__new__(SUPERVISOR.Supervisor)
        supervisor.store = Store(self.root / "data")
        generation = supervisor.store.pr_dir(PR) / "generations" / "1"
        atomic(generation / "bundle.json", {"intents": [{"id": "c1", "kind": "comment", "target": PR},
                                                        {"id": "a1", "kind": "annotation", "target": "x"}]})
        self.assertEqual(supervisor.generation_receipts(PR, 2), [])      # claimed, no bundle yet
        atomic(supervisor.store.pr_dir(PR) / "generations" / "2" / "bundle.json",
               {"intents": [{"id": "c2", "kind": "comment", "target": PR}]})
        atomic(supervisor.store.root / "receipts" / "c2.json", {"kind": "comment", "state": "held"})
        self.assertEqual(supervisor.generation_receipts(PR, 2), [{"kind": "comment", "state": "held"}])
        self.assertEqual(supervisor.generation_receipts(PR, 1), [])
        atomic(supervisor.store.root / "receipts" / "c1.json", {"kind": "comment", "state": "posted"})
        atomic(supervisor.store.root / "receipts" / "other.json", {"kind": "comment", "state": "posted"})
        self.assertEqual(supervisor.generation_receipts(PR, 1), [{"kind": "comment", "state": "posted"}])
        self.assertEqual(supervisor.generation_receipts(PR, None), [])

    def test_a_pr_reviewed_recently_waits_unless_named_or_due_at_a_call(self):
        import datetime
        supervisor = SUPERVISOR.Supervisor.__new__(SUPERVISOR.Supervisor)
        supervisor.store = Store(self.root / "data")
        supervisor.config = {"mode": "followup", "configuration": {"rereview_hours": 12}}
        c = dict(candidate(), pr=PR, destinations=[])
        self.assertFalse(supervisor.too_soon(PR, c))                   # never reviewed
        generation = supervisor.store.pr_dir(PR) / "generations" / "1"
        atomic(generation / "bundle.json", {"intents": []})
        atomic(supervisor.store.pr_dir(PR) / "current", {"generation": 1, "digest": "x"})
        self.assertTrue(supervisor.too_soon(PR, c))                    # reviewed just now
        self.assertFalse(supervisor.too_soon(PR, dict(c, mode="pr")))  # asked for by name
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y_%m_%d")
        call = dict(c, destinations=["page:review/DevCallReviews/%s/DevCallEU/devcall_pr_reviews.html" % tomorrow])
        self.assertFalse(supervisor.too_soon(PR, call))                # due at a call
        later = (datetime.date.today() + datetime.timedelta(days=5)).strftime("%Y_%m_%d")
        self.assertTrue(supervisor.too_soon(PR, dict(c, destinations=[
            "page:review/DevCallReviews/%s/DevCallEU/devcall_pr_reviews.html" % later])))
        old = time.time() - 13 * 3600
        os.utime(generation, (old, old))
        self.assertFalse(supervisor.too_soon(PR, c))                   # interval passed
        atomic(generation / "bundle.json", {"intents": [], "legacy": True})
        os.utime(generation, None)
        self.assertFalse(supervisor.too_soon(PR, c))                   # an imported review
        supervisor.config["configuration"]["rereview_hours"] = 0
        self.assertFalse(supervisor.too_soon(PR, c))

    def test_the_loops_own_drain_is_short_while_prs_wait(self):
        supervisor = SUPERVISOR.Supervisor.__new__(SUPERVISOR.Supervisor)
        supervisor.directory = self.root
        supervisor.store = Mock()
        supervisor.store.recover.return_value = None
        supervisor.store.last_recovered = 0
        supervisor.adapter = None
        supervisor.states = {PR: {"review": "pending"}}
        supervisor.bounded_drain()
        self.assertLessEqual(supervisor.store.drain.call_args.kwargs["seconds"], supervisor.DRAIN_BUSY)
        supervisor.states = {PR: {"review": "accepted"}}
        supervisor.bounded_drain()
        self.assertGreater(supervisor.store.drain.call_args.kwargs["seconds"], supervisor.DRAIN_BUSY)

    def test_terminal_transition_between_poll_and_schedule_does_not_retry(self):
        supervisor = SUPERVISOR.Supervisor.__new__(SUPERVISOR.Supervisor)
        supervisor.directory = self.root
        supervisor.states = {PR: {"candidate": candidate(), "attempts": {"cold": ["unused"]}}}
        supervisor.store = Mock()
        supervisor.store.claim.return_value = {"selected": {"primary": "done"}}
        supervisor.attempt_state = Mock(side_effect=["live", "done"])
        supervisor.start_attempt = Mock()
        supervisor.advance(PR)
        self.assertEqual([call.args[1] for call in supervisor.start_attempt.call_args_list], ["validation"])


if __name__ == "__main__":
    unittest.main()
