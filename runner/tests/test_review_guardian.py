#!/usr/bin/env python3
"""Payload, guardian, timeout and abort failures cannot manufacture success."""
import copy
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from review_fixtures import BIN, PR, candidate, stop, until, workspace
from review_guardian import alive, cleanup_attempt, launch
from review_lock import permit, region, try_lock
from review_schema import canned, validate
from review_store import Store, atomic, mkdir, read


class ReviewGuardian(unittest.TestCase):
    def setUp(self):
        self.root = workspace(self)
        self.store = Store(self.root)
        self.pr = try_lock(self.store.locks, PR)
        self.addCleanup(self.pr.close)
        self.attempt = self.root / "attempt"
        mkdir(self.attempt)
        self.env = patch.dict(os.environ, REVIEW_GUARDIAN_PLAIN="1", REVIEW_AI_STUB="1")
        self.env.start()
        self.addCleanup(self.env.stop)

    def start(self, command=None, timeout=3, behavior=None):
        job = dict(candidate(), schema=1, run="run", job="primary", attempt="attempt", generation=1,
                   kind="primary", pr=PR, provider="claude", account="stub", pool_size=2,
                   wall_timeout=timeout, permit_timeout=1, abort_path=str(self.root / "abort.json"),
                   command=command or [sys.executable, str(BIN / "review_stub.py")], stub=behavior or {})
        atomic(self.attempt / "job.json", job)
        child = launch(self.root, self.attempt, self.pr)
        def cleanup():
            atomic(self.root / "abort.json", {"requested": True})
            try:
                child.wait(timeout=6)
            except subprocess.TimeoutExpired:
                stop(child)
            cleanup_attempt(self.attempt, time.monotonic() + 3)
        self.addCleanup(cleanup)
        return child

    def status(self):
        return read(self.attempt / "status.json", {})

    def running(self):
        return until(self, lambda: self.status() if self.status().get("state") == "running" else None)

    def terminal(self, child):
        child.wait(timeout=10)
        status = self.status()
        self.assertEqual(status.get("state"), "terminal", (status, read(self.attempt / "empty.json")))
        self.assertTrue(status["empty"])
        return status

    def test_memory_limits_follow_the_box_not_a_fixed_figure(self):
        from review_guardian import memory_limits
        # blu6: 30G and four build slots -> throttle near 6G, kill near 12G
        high, ceiling = memory_limits(heavy=4, total=30 * (1 << 30))
        self.assertEqual(high, "--property=MemoryHigh=6144M")
        self.assertEqual(ceiling, "--property=MemoryMax=12288M")
        # never above the machine, never absurdly small
        for total in (4, 30, 256):
            h, c = (int(x.split("=")[-1][:-1]) for x in memory_limits(heavy=4, total=total * (1 << 30)))
            self.assertLessEqual(c, total * 1024)
            self.assertGreaterEqual(h, 2048)
            self.assertLessEqual(h, c)

    def test_success_and_payload_receives_no_lock_descriptors(self):
        command = [sys.executable, "-c", "import os,runpy,sys; from pathlib import Path; assert not any(os.path.realpath(p)==os.environ['LOCK_PATH'] for p in Path('/proc/self/fd').glob('*')); sys.path.insert(0,str(Path(os.environ['STUB_PATH']).parent)); runpy.run_path(os.environ['STUB_PATH'],run_name='__main__')"]
        with patch.dict(os.environ, LOCK_PATH=str(self.store.locks), STUB_PATH=str(BIN / "review_stub.py")):
            child = self.start(command)
        status = self.terminal(child)
        self.assertEqual(status["exit"], 0)
        self.assertEqual(status["result_status"], "complete")
        self.assertIsNone(try_lock(self.store.locks, PR))
        self.pr.close()
        lock = try_lock(self.store.locks, PR)
        self.assertIsNotNone(lock)
        lock.close()

    def test_killed_payload_is_failure_and_releases_only_after_empty(self):
        child = self.start(behavior={"sleep": 2})
        status = self.running()
        self.pr.close()
        self.assertIsNone(try_lock(self.store.locks, PR))
        os.kill(status["payload"]["pid"], signal.SIGKILL)
        terminal = self.terminal(child)
        self.assertEqual(terminal["exit"], -signal.SIGKILL)
        self.assertEqual(terminal["result_status"], "missing")

    def test_killed_guardian_cannot_leave_a_payload_or_success(self):
        command = [sys.executable, "-c", "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import os,time; os.setsid(); time.sleep(8)']); time.sleep(8)"]
        child = self.start(command, timeout=4)
        status = self.running()
        self.pr.close()
        os.kill(status["pid"], signal.SIGKILL)
        child.wait(timeout=10)
        self.assertNotEqual(self.status()["state"], "terminal")
        self.assertFalse(alive(status["payload"]))
        self.assertTrue(cleanup_attempt(self.attempt, time.monotonic() + 2))
        lock = try_lock(self.store.locks, PR)
        self.assertIsNotNone(lock)
        lock.close()

    def test_timeout_kills_detached_descendants(self):
        command = [sys.executable, "-c", "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import os,time; os.setsid(); time.sleep(8)']); time.sleep(8)"]
        child = self.start(command, timeout=0.2)
        status = self.terminal(child)
        self.assertTrue(status["timed_out"])
        self.assertNotEqual(status["exit"], 0)

    def test_abort_is_durable_and_prevents_launch(self):
        atomic(self.root / "abort.json", {"requested": True})
        status = self.terminal(self.start())
        self.assertTrue(status["aborted"])
        self.assertNotIn("payload", status)

    def test_abort_while_payload_runs(self):
        child = self.start(behavior={"sleep": 2})
        self.running()
        atomic(self.root / "abort.json", {"requested": True})
        status = self.terminal(child)
        self.assertTrue(status["aborted"])
        self.assertNotEqual(status["exit"], 0)

    def test_complete_json_with_nonzero_exit_is_not_success(self):
        status = self.terminal(self.start(behavior={"exit": 7}))
        self.assertEqual(status["result_status"], "complete")
        self.assertEqual(status["exit"], 7)

    def test_invalid_result_is_recorded(self):
        status = self.terminal(self.start(behavior={"invalid": True}))
        self.assertEqual(status["result_status"], "invalid")

    def test_fifo_result_cannot_block_validation_past_the_wall_limit(self):
        child = self.start([sys.executable, "-c", "import os; os.mkfifo('review.json')"], timeout=0.2)
        status = self.terminal(child)
        self.assertEqual(status["result_status"], "invalid")

    def test_previous_physical_permit_owner_blocks_reuse(self):
        from review_lock import boot_id
        old = self.root / "old-attempt"
        mkdir(old)
        atomic(old / "launch.json", {"boot": boot_id(), "backend": "plain"})
        atomic(old / "status.json", {"boot": boot_id(), "pid": 999999999, "start": 0, "empty": False})
        atomic(self.root / "owners" / (str(region("permit:claude:1")) + ".json"), {"attempts": [str(old)]})
        status = self.terminal(self.start())
        self.assertIn("previous permit", status.get("error", ""))
        self.assertNotIn("payload", status)

    def test_a_finished_transient_unit_is_not_a_live_payload(self):
        # systemctl answers "not loaded" for a transient unit that already
        # exited; under systemd that was read as blocked, and every later
        # permit in the pool was refused
        # needs the user manager: the mutation harness runs without a bus,
        # and "cannot ask" is deliberately not "finished"
        probe = subprocess.run(["systemctl", "--user", "show", "--property=Version"],
                               capture_output=True, text=True)
        if probe.returncode != 0:
            self.skipTest("no user systemd bus")
        from review_lock import boot_id
        old = self.root / "old-attempt"
        mkdir(old)
        atomic(old / "launch.json", {"boot": boot_id(), "backend": "systemd",
                                     "unit": "review-attempt-never-existed.service"})
        atomic(old / "status.json", {"boot": boot_id(), "pid": 999999999, "start": 0, "empty": False,
                                     "cgroup": str(self.root / "no-such-cgroup")})
        self.assertTrue(cleanup_attempt(old, time.monotonic() + 5))

    def test_the_payload_gets_the_environment_the_job_names(self):
        # the guardian may run under systemd with the user manager's
        # environment; what the payload needs travels in the job
        self.env.stop()
        os.environ.pop("REVIEW_AI_STUB", None)
        os.environ["REVIEW_GUARDIAN_PLAIN"] = "1"
        job = dict(candidate(), schema=1, run="run", job="primary", attempt="attempt", generation=1,
                   kind="primary", pr=PR, provider="claude", account="stub", pool_size=2,
                   wall_timeout=3, permit_timeout=1, abort_path=str(self.root / "abort.json"),
                   env={"REVIEW_AI_STUB": "1"},
                   command=[sys.executable, str(BIN / "review_stub.py")], stub={})
        atomic(self.attempt / "job.json", job)
        child = launch(self.root, self.attempt, self.pr)
        self.addCleanup(lambda: cleanup_attempt(self.attempt, time.monotonic() + 3))
        status = self.terminal(child)
        self.assertEqual(status["exit"], 0, (self.attempt / "payload.log").read_text())
        self.assertEqual(status["result_status"], "complete")
        self.env.start()

    def test_durable_quota_pause_prevents_payload(self):
        atomic(self.root / "quota.json", {"account:claude/stub": {"paused": True, "reason": "exhausted"}})
        status = self.terminal(self.start())
        self.assertTrue(status.get("quota_blocked"))
        self.assertNotIn("payload", status)

    def test_provider_pause_also_applies_to_an_active_account(self):
        atomic(self.root / "quota.json", {"claude": {"paused": True}, "account:claude/stub": {"paused": False}})
        status = self.terminal(self.start())
        self.assertTrue(status.get("quota_blocked"))
        self.assertNotIn("payload", status)


class ReviewResults(unittest.TestCase):
    def setUp(self):
        self.job = dict(candidate(), run="run", job="job", attempt="attempt", generation=1,
                        kind="reconciliation", finding_ids=[])
        self.result = canned(self.job)

    def test_identity_unknown_fields_and_incomplete_status(self):
        for field, value in (("attempt", "old"), ("extra", 1), ("status", "incomplete")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate(dict(self.result, **{field: value}), self.job)

    def outcome(self, ident, **changes):
        return dict(id=ident, blocking=True, actionable=True, disposition="retained", rationale="",
                    evidence={"commands": [], "artifacts": [], "configuration": "stub"}, **changes)

    def test_a_complete_review_may_name_what_it_could_not_exercise(self):
        # the first live cold pass on the box did honest work, listed two
        # things the sandbox would not let it run, and was thrown away as
        # "incomplete" because a complete result was not allowed any gaps
        complete = dict(self.result, gaps=["UNCONFIRMED: SITL not run, no network in the sandbox"])
        try:
            self.assertEqual(validate(complete, self.job)["status"], "complete")
        except ValueError as error:
            self.fail("a complete result with gaps was rejected: %s" % error)
        with self.assertRaises(ValueError):
            validate(dict(self.result, status="incomplete", gaps=[]), self.job)   # must say why

    def test_accept_cannot_hide_an_unrefuted_blocker(self):
        self.job["finding_ids"] = ["primary:F1"]
        self.result["outcomes"] = [self.outcome("primary:F1")]
        with self.assertRaises(ValueError):
            validate(self.result, self.job)
        self.result["verdict"] = "REQUEST CHANGES"
        self.assertEqual(validate(self.result, self.job)["verdict"], "REQUEST CHANGES")

    def test_missing_duplicate_and_cyclic_outcomes_are_rejected(self):
        self.job["finding_ids"] = ["primary:F1", "cold:F1"]
        for outcomes in ([], [self.outcome("primary:F1")] * 2):
            with self.assertRaises(ValueError):
                validate(dict(self.result, outcomes=outcomes), self.job)
        outcomes = [self.outcome(x) for x in self.job["finding_ids"]]
        for i, outcome in enumerate(outcomes):
            outcome.update(disposition="merged", rationale="duplicate", target=outcomes[1-i]["id"])
        with self.assertRaises(ValueError):
            validate(dict(self.result, outcomes=outcomes), self.job)


if __name__ == "__main__":
    unittest.main()
