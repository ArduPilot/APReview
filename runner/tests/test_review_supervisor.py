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
