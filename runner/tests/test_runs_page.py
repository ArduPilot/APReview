#!/usr/bin/env python3
"""The dashboard has to describe the accounts the roles actually select.

Switching a role is the point of the auth layout, and the dashboard is where
someone looks while doing it - because a quota ran out. Every check here failed
before the role layout landed: the meters were wired to a variable that no
longer exists and to one hardcoded directory each.
"""
import datetime
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(os.path.dirname(HERE), "bin")
PAGE = os.path.join(BIN, "make-runs-page.py")


def usage_line(when, tokens):
    return json.dumps({"timestamp": when.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       "message": {"usage": {"input_tokens": tokens,
                                             "output_tokens": 0,
                                             "cache_creation_input_tokens": 0,
                                             "cache_read_input_tokens": 0}}})


class Dashboard(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.logs = os.path.join(self.home, "review", "logs")
        self.auth = os.path.join(self.home, "review", "auth")
        os.makedirs(self.logs)
        os.makedirs(self.auth, mode=0o700)
        self.out = os.path.join(self.home, "runs.html")

    def transcript(self, account, tokens, name="s.jsonl"):
        """A session transcript under an account directory, recent enough to count."""
        if account == "own":
            d = os.path.join(self.home, ".claude", "projects", "-p")
        else:
            d = os.path.join(self.auth, account, "projects", "-p")
        os.makedirs(d, exist_ok=True)
        now = datetime.datetime.now().astimezone()
        with open(os.path.join(d, name), "w") as f:
            f.write(usage_line(now - datetime.timedelta(minutes=5), tokens) + "\n")

    def log(self, mode, body):
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        p = os.path.join(self.logs, "reviewprs-%s-%s.log" % (mode, stamp))
        with open(p, "w") as f:
            f.write(body)
        return p

    def build(self):
        r = subprocess.run(["python3", PAGE, self.out], capture_output=True,
                           text=True, env={"HOME": self.home,
                                           "PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.out) as f:
            return f.read()

    # --- which accounts the meters look at ----------------------------------
    def test_it_counts_the_account_a_role_selects(self):
        # the whole point of the layout: `use claude default personal` moves the
        # workload, and the meter has to move with it
        self.transcript("claude-personal", 4242424)
        os.symlink("claude-personal", os.path.join(self.auth, "claude-default"))
        self.assertIn("4.2M", self.build())

    def test_it_counts_an_account_no_role_points_at_today(self):
        # yesterday's account is still what yesterday's runs spent
        self.transcript("claude-ardupilot", 7000000)
        self.assertIn("7.0M", self.build())

    def test_it_counts_the_tools_own_directory(self):
        self.transcript("own", 3000000)
        self.assertIn("3.0M", self.build())

    def test_it_does_not_count_an_account_twice_through_its_role_link(self):
        # a role is a symlink into the same set; following it would double every
        # figure for whichever account is selected
        self.transcript("claude-personal", 5000000)
        os.symlink("claude-personal", os.path.join(self.auth, "claude-default"))
        page = self.build()
        self.assertIn("5.0M", page)
        self.assertNotIn("10.0M", page)     # counted through the link as well

    # --- how a refused run is shown -----------------------------------------
    def refusal(self, status):
        self.log("followup",
                 "reviewprs mode=followup  host=t  start=2026-09-18T01:00:00+10:00\n"
                 "FATAL: refused\nfinish=2026-09-18T01:00:01+10:00 status=%s\n" % status)
        return self.build()

    def test_a_refused_claude_run_is_shown_as_refused(self):
        self.assertIn("wrong-account", self.refusal("wrong-claude-account"))

    def test_a_refused_codex_run_is_shown_as_refused(self):
        # matched by shape, not by name: an unrecognised refusal displays as a
        # run still going, with an elapsed time that climbs for ever
        page = self.refusal("wrong-codex-account")
        self.assertIn("wrong-account", page)
        self.assertNotIn(">running<", page)

    def test_a_run_with_no_finish_line_is_still_shown_as_running(self):
        self.log("followup",
                 "reviewprs mode=followup  host=t  start=2026-09-18T01:00:00+10:00\n")
        self.assertIn("running", self.build())


if __name__ == "__main__":
    unittest.main(verbosity=2)
