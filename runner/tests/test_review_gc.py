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
        run = self.data / "runs" / "followup-1"
        write(run / "run.json", {"created": OLD})
        write(run / "summary.json", {"state": "complete", "heartbeat": OLD})
        for name in ("free", "claimed", "fresh", "worktree"):
            a = run / "attempts" / name
            for keep in ("job.json", "status.json", "launch.json", "review.json", "payload.log"):
                write(a / keep, {})
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

    def gc(self, *args):
        r = subprocess.run([sys.executable, str(BIN / "review-gc.py"), "--data", str(self.data),
                            "--cache", str(self.cache), *args], capture_output=True, text=True)
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

    def test_legacy_directories_need_their_own_flag(self):
        self.gc("--apply", "--legacy")
        self.assertFalse((self.data / "fu_20260928_2037_EbmO").exists())

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
