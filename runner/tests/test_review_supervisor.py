#!/usr/bin/env python3
"""Bounded subprocess runs cover admission, retries, recovery and overlap."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock

from review_fixtures import BIN, PR, candidate, stop, until, workspace
from review_lock import try_lock
from review_store import Store, atomic, read

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
        # a failed prefetch leaves the claim to refresh and report
        supervisor.discovery.refresh.side_effect = OSError("gone")
        supervisor.prefetch([dict(candidate(4), pr="pr:owner/repo#4")])
        self.assertIsNone(supervisor.prefetched_refresh("pr:owner/repo#4"))

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
