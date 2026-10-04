#!/usr/bin/env python3
"""The standalone drainer runs one at a time."""
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BIN = Path(__file__).resolve().parents[1] / "bin"


class Drainer(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("REVIEW_TEST_DIR", "/data/review")) / "supervisor-tests"
        base.mkdir(parents=True, exist_ok=True)
        self.data = Path(tempfile.mkdtemp(prefix="drain-", dir=base))

    def drain(self):
        return subprocess.run([sys.executable, str(BIN / "review-drain.py"), "--data", str(self.data),
                               "--budget", "1"], capture_output=True, text=True, timeout=60,
                              env=dict(os.environ, PYTHONPATH=str(BIN)))

    def test_a_second_drainer_leaves_the_work_to_the_first(self):
        with open(self.data / "drain.lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            r = self.drain()
        self.assertEqual(r.returncode, 0, r.stderr)
        # it never opened the store
        self.assertFalse((self.data / "outbox").exists())

    def test_an_idle_drainer_runs_and_returns(self):
        r = self.drain()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.data / "outbox").exists())
        self.assertEqual(os.stat(self.data / "drain.lock").st_mode & 0o777, 0o600)
        self.assertTrue((self.data / "drain-last.json").exists())


if __name__ == "__main__":
    unittest.main()
