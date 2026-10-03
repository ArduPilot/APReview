#!/usr/bin/env python3
"""The garbage collector removes only what nothing can still need."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

BIN = Path(__file__).resolve().parents[1] / "bin"
OLD = time.time() - 10 * 86400


def write(path, data="x", age=OLD):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    os.utime(path, (age, age))


def age_tree(root, age=OLD):
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + dirnames:
            os.utime(os.path.join(dirpath, name), (age, age), follow_symlinks=False)
    os.utime(root, (age, age))


class GC(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("REVIEW_TEST_DIR", "/data/review")) / "supervisor-tests"
        base.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="gc-", dir=base))
        self.data, self.cache = self.root / "data", self.root / "cache"
        (self.root / "etc").mkdir()
        run = self.data / "runs" / "followup-1"
        write(run / "run.json", {"created": OLD})
        write(run / "summary.json", {"state": "complete", "heartbeat": OLD})
        for name in ("free", "claimed", "fresh", "worktree"):
            a = run / "attempts" / name
            for keep in ("job.json", "launch.json", "review.json", "payload.log"):
                write(a / keep, {})
            write(a / "status.json", {"state": "terminal"})
            write(a / "cold-evidence" / "build" / "big.o")
        (run / "attempts" / "worktree" / "wt").mkdir()
        write(run / "attempts" / "stray.txt")
        age_tree(run)
        write(run / "attempts" / "fresh" / "cold-evidence" / "new.o", age=time.time())
        write(self.data / "results" / "o" / "r" / "1" / "claim.json",
              {"attempts": [str(run / "attempts" / "claimed")], "selected": {}})
        live = self.data / "runs" / "all-2"
        write(live / "run.json", {"created": time.time()})
        write(live / "summary.json", {"state": "running", "heartbeat": time.time()})
        write(live / "attempts" / "busy" / "cold-evidence" / "x.o")
        write(self.data / "receipts" / "r1.json", {})
        write(self.data / "pr34599" / "build" / "a.o")
        write(self.data / "uv-cache" / "blob")
        write(self.data / "pr-today" / "a.o", age=time.time())
        write(self.data / "fu_20260928_2037_EbmO" / "told.tsv")
        write(self.data / "tmp" / "old-scratch" / "f")
        write(self.cache / "venvs" / "stale" / "bin" / "python")
        write(self.cache / "venvs" / "used" / "bin" / "python", age=time.time())
        for p in (self.data / "pr34599", self.data / "uv-cache", self.data / "fu_20260928_2037_EbmO",
                  self.data / "tmp" / "old-scratch"):
            age_tree(p)
        age_tree(self.cache / "venvs" / "stale", time.time() - 20 * 86400)

    def gc(self, *args, ok=True):
        r = subprocess.run([sys.executable, str(BIN / "review-gc.py"), "--data", str(self.data),
                            "--cache", str(self.cache), *args], capture_output=True, text=True)
        if not ok:
            return r
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads((self.data / "gc" / "last.json").read_text())

    def test_a_report_deletes_nothing(self):
        report = self.gc()
        self.assertFalse(report["applied"])
        self.assertIn("litter", report["removed"])
        self.assertTrue((self.data / "pr34599").exists())
        self.assertTrue((self.data / "runs/followup-1/attempts/free/cold-evidence").exists())

    def test_apply_removes_only_unreachable_quiet_work(self):
        self.gc("--apply")
        attempts = self.data / "runs" / "followup-1" / "attempts"
        # an unpinned quiet attempt keeps its records and loses its evidence
        self.assertEqual(sorted(p.name for p in (attempts / "free").iterdir()),
                         ["job.json", "launch.json", "payload.log", "review.json", "status.json"])
        # a claim may carry this pass forward; promotion copies its evidence
        self.assertTrue((attempts / "claimed" / "cold-evidence").exists())
        self.assertTrue((attempts / "fresh" / "cold-evidence").exists())
        self.assertTrue((attempts / "worktree" / "cold-evidence").exists())
        self.assertTrue((attempts / "stray.txt").exists())
        self.assertTrue((self.data / "runs/all-2/attempts/busy/cold-evidence").exists())
        # store state is never touched
        self.assertTrue((self.data / "receipts" / "r1.json").exists())
        # litter and old in-store caches go once quiet; recent litter stays
        self.assertFalse((self.data / "pr34599").exists())
        self.assertFalse((self.data / "uv-cache").exists())
        self.assertTrue((self.data / "pr-today").exists())
        # the retired path's directories only with --legacy
        self.assertTrue((self.data / "fu_20260928_2037_EbmO").exists())
        self.assertFalse((self.data / "tmp" / "old-scratch").exists())
        self.assertFalse((self.cache / "venvs" / "stale").exists())
        self.assertTrue((self.cache / "venvs" / "used").exists())

    def test_nothing_shared_is_collected_while_a_run_is_live(self):
        sys.path.insert(0, str(BIN))
        from review_guardian import identity
        write(self.data / "runs" / "all-2" / "controller.json", identity(os.getpid()))
        report = self.gc("--apply")
        self.assertTrue((self.data / "pr34599").exists())
        self.assertTrue((self.cache / "venvs" / "stale").exists())
        self.assertIn("a run or guardian is live", report["skipped"]["all rules"])

    def test_store_state_at_the_top_is_never_litter(self):
        for name in ("held", "handoff"):
            write(self.data / name / "x")
            age_tree(self.data / name)
        write(self.data / "legacy-facts.json", {})
        write(self.data / "something-new.json", {})
        self.gc("--apply")
        for name in ("held", "handoff", "legacy-facts.json", "something-new.json"):
            self.assertTrue((self.data / name).exists(), name)

    def test_a_symlinked_run_is_not_followed(self):
        outside = self.root / "elsewhere"
        write(outside / "attempts" / "x" / "cold-evidence" / "keep.o")
        age_tree(outside)
        (self.data / "runs" / "linked").symlink_to(outside)
        self.gc("--apply")
        self.assertTrue((outside / "attempts" / "x" / "cold-evidence" / "keep.o").exists())

    def test_an_owner_record_pins_its_attempt(self):
        attempt = self.data / "runs" / "followup-1" / "attempts" / "free"
        write(self.data / "owners" / "7.json", {"attempt": str(attempt)})
        self.gc("--apply")
        self.assertTrue((attempt / "cold-evidence").exists())

    def test_an_unreadable_claim_stops_the_collector(self):
        write(self.data / "results" / "o" / "r" / "2" / "claim.json", "{not json")
        r = self.gc("--apply", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue((self.data / "pr34599").exists())

    def test_old_runs_keep_the_guardian_proofs(self):
        attempt = self.data / "runs" / "followup-1" / "attempts" / "free"
        write(attempt / "empty.json", {})
        write(attempt / "manager.json", {})
        old = time.time() - 40 * 86400
        write(self.data / "runs" / "followup-1" / "run.json", {"created": old})
        age_tree(self.data / "runs" / "followup-1")
        self.gc("--apply")
        self.assertEqual(sorted(p.name for p in attempt.iterdir()),
                         ["empty.json", "job.json", "launch.json", "manager.json", "status.json"])

    def test_a_controller_holding_maintenance_stops_all_deletion(self):
        sys.path.insert(0, str(BIN))
        from review_lock import try_lock
        with try_lock(self.data / "locks", "maintenance", shared=True):
            report = self.gc("--apply")
        self.assertIn("a controller holds the maintenance lock", report["skipped"]["all rules"])
        self.assertTrue((self.data / "pr34599").exists())
        self.assertTrue((self.data / "runs/followup-1/attempts/free/cold-evidence").exists())

    def test_a_symlinked_attempts_directory_is_not_followed(self):
        outside = self.root / "elsewhere2"
        write(outside / "x" / "cold-evidence" / "keep.o")
        write(outside / "x" / "status.json", {"state": "terminal"})
        age_tree(outside)
        run = self.data / "runs" / "rerouted"
        write(run / "run.json", {"created": OLD})
        write(run / "summary.json", {"state": "complete", "heartbeat": OLD})
        (run / "attempts").symlink_to(outside)
        age_tree(run)
        self.gc("--apply")
        self.assertTrue((outside / "x" / "cold-evidence" / "keep.o").exists())

    def test_an_attempt_whose_cleanup_is_unresolved_keeps_everything(self):
        attempt = self.data / "runs" / "followup-1" / "attempts" / "free"
        write(attempt / "status.json", {"state": "running"})
        old = time.time() - 100 * 86400
        write(self.data / "runs" / "followup-1" / "run.json", {"created": old})
        age_tree(self.data / "runs" / "followup-1")
        self.gc("--apply")
        self.assertTrue((attempt / "cold-evidence").exists())   # neither expired nor trimmed

    def test_legacy_directories_need_their_own_flag(self):
        self.gc("--apply", "--legacy")
        self.assertFalse((self.data / "fu_20260928_2037_EbmO").exists())

    def test_legacy_directories_stay_while_the_retired_path_holds_its_lock(self):
        import fcntl
        with open(self.root / "etc" / "reviewprs.lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.gc("--apply", "--legacy")
        self.assertTrue((self.data / "fu_20260928_2037_EbmO").exists())

    def test_an_attempt_with_no_status_is_kept_whole(self):
        attempt = self.data / "runs" / "followup-1" / "attempts" / "free"
        (attempt / "status.json").unlink()
        age_tree(self.data / "runs" / "followup-1")
        self.gc("--apply")
        self.assertTrue((attempt / "cold-evidence").exists())

    def test_a_directory_a_process_is_using_is_kept(self):
        child = subprocess.Popen(["sleep", "30"], cwd=self.data / "pr34599")
        try:
            self.gc("--apply")
            self.assertTrue((self.data / "pr34599").exists())
        finally:
            child.kill()
            child.wait()


if __name__ == "__main__":
    unittest.main()
