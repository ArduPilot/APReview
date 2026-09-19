#!/usr/bin/env python3
"""Reading what each account has left, without acting on it.

These drive runner/bin/quota.py against stub CLIs. The point of the module is
that it observes only, so the tests care as much about what it does NOT do -
never raising, never choosing an account - as about the figures it parses.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(os.path.dirname(HERE), "bin")
QUOTA = os.path.join(BIN, "quota.py")

# what `claude -p /usage --output-format stream-json` puts on the stream
CLAUDE_REPLY = {
    "type": "assistant",
    "usage_report": {"session": {"total_cost_usd": 0},
                     "rate_limits": {"limits": [
                         {"kind": "session", "group": "session", "percent": 12,
                          "resets_at": "2026-09-19T22:00:00+00:00", "scope": None},
                         {"kind": "weekly_all", "group": "weekly", "percent": 42,
                          "resets_at": "2026-09-20T06:00:00+00:00", "scope": None},
                         {"kind": "weekly_scoped", "group": "weekly", "percent": 90,
                          "resets_at": "2026-09-20T06:00:00+00:00",
                          "scope": {"model": {"display_name": "Fable"}}}]}}}

# what the app-server answers to account/rateLimits/read
CODEX_REPLY = {"id": 2, "result": {
    "ordinaryUsageAllowed": True,
    "rateLimits": {"limitId": "codex", "planType": "pro",
                   "primary": {"usedPercent": 92, "windowDurationMins": 10080,
                               "resetsAt": 1790151439},
                   "secondary": None,
                   "credits": {"hasCredits": False, "unlimited": False}}}}


def stub(path, body):
    with open(path, "w") as f:
        f.write("#!/bin/bash\n" + body + "\n")
    os.chmod(path, 0o755)


class Quota(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.auth = os.path.join(self.home, "review.auth")
        os.makedirs(self.auth, mode=0o700)
        os.makedirs(os.path.join(self.home, "review", "logs"))
        self.stubs = os.path.join(self.home, "stubs")
        os.makedirs(self.stubs)
        self.claude("printf '%s\\n' " + repr(json.dumps(CLAUDE_REPLY)))
        self.codex(json.dumps(CODEX_REPLY))

    def account(self, name, ident):
        d = os.path.join(self.auth, name)
        os.makedirs(d, exist_ok=True)
        if name.startswith("claude"):
            json.dump({"oauthAccount": {"emailAddress": ident}},
                      open(os.path.join(d, ".claude.json"), "w"))
        else:
            json.dump({"tokens": {"account_id": ident}},
                      open(os.path.join(d, "auth.json"), "w"))
        return d

    def claude(self, body):
        stub(os.path.join(self.stubs, "claude"), body)

    def codex(self, reply):
        """An app-server that shuts down on stdin EOF, as the real one does.

        Modelling that is the point: a caller which writes its requests and
        closes the pipe gets no answer, and a stub that replied anyway would
        let that regression through.
        """
        path = os.path.join(self.stubs, "codex")
        with open(path, "w") as f:
            f.write("#!/usr/bin/env python3\n"
                    "import select, sys\n"
                    "reply = %r\n"
                    "while True:\n"
                    "    line = sys.stdin.readline()    # not iteration: it reads ahead\n"
                    "    if line == '':\n"
                    "        sys.exit(0)\n"
                    "    if 'rateLimits' not in line:\n"
                    "        continue\n"
                    "    # still open, or has the caller written and gone?\n"
                    "    r, _, _ = select.select([sys.stdin], [], [], 0.3)\n"
                    "    if r and sys.stdin.readline() == '':\n"
                    "        sys.exit(0)\n"
                    "    print(reply, flush=True)\n"
                    "    sys.exit(0)\n" % reply)
        os.chmod(path, 0o755)

    def run_quota(self, *args, **env):
        e = {"HOME": self.home, "PATH": self.stubs + ":/usr/bin:/bin",
             "REVIEW_AUTH": self.auth, "LANG": "C.UTF-8",
             "REVIEW_LOGS": os.path.join(self.home, "review", "logs")}
        e.update(env)
        return subprocess.run(["python3", QUOTA, *args],
                              capture_output=True, text=True, env=e)

    def records(self, *args, **env):
        out = self.run_quota("--json", *args, **env)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return json.loads(out.stdout)

    # --- the figures ---------------------------------------------------------
    def test_it_reads_claudes_structured_windows(self):
        self.account("claude-a", "a@example.org")
        r = [x for x in self.records("claude") if x["dir"].endswith("claude-a")][0]
        kinds = {w["kind"]: w["used_pct"] for w in r.get("windows", [])}
        self.assertEqual(kinds, {"session": 12, "weekly_all": 42, "weekly_scoped": 90})
        self.assertEqual(r.get("account"), "a@example.org")
        self.assertEqual(r.get("source"), "usage_report")

    def test_free_is_what_is_left_of_the_worst_window(self):
        self.account("claude-a", "a@example.org")
        r = [x for x in self.records("claude") if x["dir"].endswith("claude-a")][0]
        self.assertEqual(r.get("free_pct"), 58.0)          # 100 - 42, not 100 - 12

    def test_a_per_model_window_is_recorded_but_does_not_decide(self):
        # a spent model is not an account that cannot start
        self.account("claude-a", "a@example.org")
        r = [x for x in self.records("claude") if x["dir"].endswith("claude-a")][0]
        scoped = [w for w in r.get("windows", []) if w["scoped"]]
        self.assertEqual(len(scoped), 1)
        self.assertEqual(scoped[0]["used_pct"], 90)    # the worst window there is
        self.assertEqual(r.get("free_pct"), 58.0)          # and it still does not decide

    def test_it_reads_codexs_live_meter_not_the_rollout_files(self):
        d = self.account("codex-a", "11111111-1111-4111-8111-111111111111")
        # a rollout saying something different: the live answer must win
        roll = os.path.join(d, "sessions", "2026", "09", "20")
        os.makedirs(roll)
        with open(os.path.join(roll, "rollout-x.jsonl"), "w") as f:
            f.write(json.dumps({"payload": {"type": "token_count", "info": {"rate_limits":
                    {"primary": {"used_percent": 5, "window_minutes": 10080}}}}}) + "\n")
        r = [x for x in self.records("codex") if x["dir"].endswith("codex-a")][0]
        self.assertEqual(r.get("source"), "app-server")
        self.assertEqual(r.get("free_pct"), 8.0)           # 100 - 92, not 100 - 5
        self.assertEqual(r.get("plan"), "pro")
        self.assertIs(r.get("ordinary_usage_allowed"), True)
        self.assertIs(r.get("has_credits"), False)

    # --- what it must not do -------------------------------------------------
    def test_a_cli_that_fails_is_recorded_not_raised(self):
        self.account("claude-a", "a@example.org")
        self.claude("exit 3")
        r = [x for x in self.records("claude") if x["dir"].endswith("claude-a")][0]
        self.assertIn("exited 3", r.get("error", ""))
        self.assertIsNone(r.get("free_pct"))

    def test_a_cli_that_answers_with_rubbish_is_recorded_not_raised(self):
        self.account("claude-a", "a@example.org")
        self.claude("echo 'not json at all'")
        r = [x for x in self.records("claude") if x["dir"].endswith("claude-a")][0]
        self.assertIn("no usage_report", r.get("error", ""))

    def test_a_missing_cli_is_recorded_not_raised(self):
        self.account("codex-a", "11111111-1111-4111-8111-111111111111")
        os.remove(os.path.join(self.stubs, "codex"))
        out = self.run_quota("--json", "codex")
        self.assertEqual(out.returncode, 0, out.stderr)
        r = [x for x in json.loads(out.stdout) if x["dir"].endswith("codex-a")][0]
        self.assertTrue(r.get("error"), r)

    def test_an_app_server_that_says_nothing_is_recorded_not_raised(self):
        self.account("codex-a", "11111111-1111-4111-8111-111111111111")
        stub(os.path.join(self.stubs, "codex"), "exit 0")
        r = [x for x in self.records("codex") if x["dir"].endswith("codex-a")][0]
        self.assertIn("no answer", r.get("error", ""))

    def test_it_chooses_nothing(self):
        # observation only: no winner, no ordering, no side effect on a run
        self.account("claude-a", "a@example.org")
        self.account("claude-b", "b@example.org")
        out = self.run_quota("claude")
        self.assertEqual(out.returncode, 0)
        for word in ("selected", "chosen", "using", "role"):
            self.assertNotIn(word, out.stdout.lower())

    # --- discovery and recording --------------------------------------------
    def test_every_account_is_read_once(self):
        self.account("claude-a", "a@example.org")
        self.account("claude-b", "b@example.org")
        os.symlink("claude-a", os.path.join(self.auth, "claude-default"))
        dirs = [r.get("dir") for r in self.records("claude")]
        self.assertEqual(len(dirs), len(set(dirs)), dirs)
        self.assertEqual(sum(d.endswith("claude-a") for d in dirs), 1)

    def test_record_appends_one_line_per_account(self):
        self.account("claude-a", "a@example.org")
        self.run_quota("--record", "claude")
        self.run_quota("--record", "claude")
        p = os.path.join(self.home, "review", "logs", "quota.jsonl")
        self.assertTrue(os.path.exists(p), "--record wrote nothing")
        with open(p) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["account"], "a@example.org")

    def test_it_writes_nothing_unless_asked(self):
        self.account("claude-a", "a@example.org")
        self.run_quota("claude")
        self.assertFalse(os.path.exists(
            os.path.join(self.home, "review", "logs", "quota.jsonl")))

    def test_an_unknown_tool_is_refused(self):
        out = self.run_quota("gemini")
        self.assertEqual(out.returncode, 2)
        self.assertIn("unknown tool", out.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
