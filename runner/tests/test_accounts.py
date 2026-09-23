#!/usr/bin/env python3
"""Choosing an account from the policy, without acting on the choice.

The module decides but never acts, so these care as much about what it refuses
to do - choose outside the list, choose on an unknown figure, choose at all
without a policy, touch anything - as about which account it picks.
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
ACCOUNTS = os.path.join(BIN, "accounts.py")

POLICY = {"min_free_pct": 5, "fresh_minutes": 15,
          "roles": {"default": {"claude": ["claude-a", "claude-b"],
                                "codex": ["codex-a", "codex-b"]},
                    "rsync": {"claude": ["claude-b"]}}}


class Accounts(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.auth = os.path.join(self.home, "review.auth")
        self.logs = os.path.join(self.home, "review", "logs")
        os.makedirs(self.auth, mode=0o700)
        os.makedirs(self.logs)
        for name in ("claude-a", "claude-b", "codex-a", "codex-b", "claude-unlisted"):
            os.makedirs(os.path.join(self.auth, name))
        self.policy(POLICY)

    def policy(self, obj):
        p = os.path.join(self.auth, "policy.json")
        with open(p, "w") as f:
            json.dump(obj, f) if not isinstance(obj, str) else f.write(obj)
        return p

    def reading(self, tool, name, free=50.0, error=None, age_min=5, ordinary=None):
        rec = {"at": (datetime.datetime.now().astimezone()
                      - datetime.timedelta(minutes=age_min)).isoformat(),
               "tool": tool, "dir": os.path.join(self.auth, name),
               "account": name + "@example.org", "free_pct": free, "windows": []}
        if ordinary is not None:
            rec["ordinary_usage_allowed"] = ordinary
        if error:
            rec["error"] = error
            rec["free_pct"] = None
        with open(os.path.join(self.logs, "quota.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")

    def cli(self, *args, **env):   # not `run`: TestCase.run is how a test is executed
        e = {"HOME": self.home, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
             "REVIEW_AUTH": self.auth, "REVIEW_LOGS": self.logs}
        e.update(env)
        return subprocess.run(["python3", ACCOUNTS, *args],
                              capture_output=True, text=True, env=e)

    def decisions(self, *args, **env):
        out = self.cli("--json", *args, **env)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return {(d["role"], d["tool"]): d for d in json.loads(out.stdout)}

    # --- the choice ----------------------------------------------------------
    def test_the_first_account_with_quota_wins(self):
        self.reading("claude", "claude-a", free=60.0)
        self.reading("claude", "claude-b", free=90.0)
        d = self.decisions("--role", "default")[("default", "claude")]
        self.assertEqual(d["chosen"], "claude-a")      # order, not the most free

    def test_a_spent_account_is_passed_over(self):
        self.reading("claude", "claude-a", free=2.0)
        self.reading("claude", "claude-b", free=70.0)
        d = self.decisions("--role", "default")[("default", "claude")]
        self.assertEqual(d["chosen"], "claude-b")
        self.assertIn("at or below", d["considered"][0]["skipped"])

    def test_the_threshold_is_a_floor_an_account_must_clear(self):
        # exactly at it is not above it
        self.reading("claude", "claude-a", free=5.0)
        self.reading("claude", "claude-b", free=5.1)
        d = self.decisions("--role", "default")[("default", "claude")]
        self.assertEqual(d["chosen"], "claude-b")

    def test_a_failed_reading_is_not_treated_as_quota(self):
        # unattended: guessing wrong spends an account that cannot pay
        self.reading("claude", "claude-a", error="no answer from the app-server")
        self.reading("claude", "claude-b", free=70.0)
        d = self.decisions("--role", "default")[("default", "claude")]
        self.assertEqual(d["chosen"], "claude-b")
        # the reason it gave, not merely that it skipped: the two are separate
        # branches and either alone would look right here
        self.assertIn("no answer from the app-server", d["considered"][0]["skipped"])

    def test_a_reading_with_no_figure_and_no_error_is_not_quota_either(self):
        # a reply that parsed but carried no window: nothing said it failed,
        # and nothing said how much was left
        self.reading("claude", "claude-a", free=None)
        self.reading("claude", "claude-b", free=70.0)
        d = self.decisions("--role", "default")[("default", "claude")]
        self.assertEqual(d["chosen"], "claude-b")
        self.assertEqual(d["considered"][0]["skipped"], "quota unknown")

    def test_with_nothing_usable_it_chooses_nothing(self):
        self.reading("claude", "claude-a", free=1.0)
        self.reading("claude", "claude-b", free=0.0)
        d = self.decisions("--role", "default")[("default", "claude")]
        self.assertIsNone(d["chosen"])
        self.assertIn("spent or unknown", d["reason"])

    def test_it_never_chooses_an_account_the_role_does_not_list(self):
        # the whole point of the list: a role cannot reach the account that
        # pays for something else by running out of its own
        self.reading("claude", "claude-b", free=90.0)
        self.reading("claude", "claude-unlisted", free=100.0)
        d = self.decisions("--role", "rsync")[("rsync", "claude")]
        self.assertEqual(d["chosen"], "claude-b")
        self.assertNotIn("claude-unlisted", json.dumps(d))

    def test_a_name_that_is_a_path_is_refused(self):
        # it has to resolve to somewhere that exists, or the check passes for
        # want of a directory rather than because the name was refused
        outside = os.path.join(self.home, "outside")
        os.makedirs(outside)
        self.policy({"roles": {"default": {"claude": ["../outside", "claude-b"]}}})
        self.reading("claude", "claude-b", free=90.0)
        d = self.decisions("--role", "default")[("default", "claude")]
        self.assertEqual(d["chosen"], "claude-b")
        self.assertIn("no usable directory", d["considered"][0]["skipped"])

    def test_a_name_with_a_separator_is_refused_even_inside_the_root(self):
        # names are directory names, not paths. Containment would allow this
        # one - it is under the root - so only the name check can refuse it,
        # and every other tool here treats an account name as flat.
        nested = os.path.join(self.auth, "sub", "claude-c")
        os.makedirs(nested)
        self.policy({"roles": {"default": {"claude": ["sub/claude-c", "claude-b"]}}})
        self.reading("claude", "claude-b", free=90.0)
        d = self.decisions("--role", "default")[("default", "claude")]
        self.assertEqual(d["chosen"], "claude-b")
        self.assertIn("no usable directory", d["considered"][0]["skipped"])

    def test_an_account_symlinked_out_of_the_root_is_refused(self):
        # the name is plain, but what it points at is not under the auth root
        outside = os.path.join(self.home, "elsewhere")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.auth, "claude-escape"))
        self.policy({"roles": {"default": {"claude": ["claude-escape", "claude-b"]}}})
        self.reading("claude", "claude-b", free=90.0)
        d = self.decisions("--role", "default")[("default", "claude")]
        self.assertEqual(d["chosen"], "claude-b")
        self.assertIn("no usable directory", d["considered"][0]["skipped"])

    # --- where the figure comes from ----------------------------------------
    def test_a_recent_reading_is_used_rather_than_asking_the_account(self):
        # asking starts the CLI, which is what makes two processes contend for
        # the OAuth refresh
        self.reading("claude", "claude-a", free=60.0, age_min=2)
        d = self.decisions("--role", "default")[("default", "claude")]
        self.assertIn("recorded", d["considered"][0]["source"])

    def test_a_stale_reading_is_not_used(self):
        self.reading("claude", "claude-a", free=60.0, age_min=600)
        d = self.decisions("--role", "default")[("default", "claude")]
        self.assertEqual(d["considered"][0]["source"], "read now")

    # --- the policy ----------------------------------------------------------
    def test_without_a_policy_it_refuses_rather_than_guessing(self):
        os.remove(os.path.join(self.auth, "policy.json"))
        out = self.cli()
        self.assertEqual(out.returncode, 1)
        self.assertIn("no account may be chosen", out.stderr)

    def test_a_policy_that_is_not_json_is_refused(self):
        self.policy("{ not json")
        self.assertEqual(self.cli().returncode, 1)

    def test_a_policy_with_no_roles_is_refused(self):
        self.policy({"min_free_pct": 5})
        self.assertEqual(self.cli().returncode, 1)

    def test_a_policy_naming_an_unknown_tool_is_refused(self):
        self.policy({"roles": {"default": {"gemini": ["x"]}}})
        out = self.cli()
        self.assertEqual(out.returncode, 1)
        self.assertIn("unknown tool", out.stderr)

    def test_a_policy_with_an_empty_list_is_refused(self):
        self.policy({"roles": {"default": {"claude": []}}})
        self.assertEqual(self.cli().returncode, 1)

    def test_a_policy_repeating_an_account_is_refused(self):
        self.policy({"roles": {"default": {"claude": ["claude-a", "claude-a"]}}})
        out = self.cli()
        self.assertEqual(out.returncode, 1)
        self.assertIn("repeats", out.stderr)

    def test_an_unknown_role_is_refused(self):
        out = self.cli("--role", "nosuch")
        self.assertEqual(out.returncode, 1)
        self.assertIn("no role", out.stderr)

    # --- deciding is not acting ----------------------------------------------
    def test_it_changes_nothing(self):
        os.symlink("claude-a", os.path.join(self.auth, "claude-default"))
        before = {p: os.path.getmtime(os.path.join(self.auth, p))
                  for p in os.listdir(self.auth)}
        link_before = os.readlink(os.path.join(self.auth, "claude-default"))
        self.reading("claude", "claude-b", free=90.0)
        self.cli()
        after = {p: os.path.getmtime(os.path.join(self.auth, p))
                 for p in os.listdir(self.auth)}
        self.assertEqual(before, after, "it touched the auth directory")
        self.assertEqual(os.readlink(os.path.join(self.auth, "claude-default")),
                         link_before, "it repointed a role")

    def test_record_writes_one_line_per_decision(self):
        self.reading("claude", "claude-a", free=60.0)
        self.cli("--record", "--role", "default")
        p = os.path.join(self.logs, "select.jsonl")
        self.assertTrue(os.path.exists(p), "--record wrote nothing")
        with open(p) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(lines), 2)                 # claude and codex
        self.assertEqual({l["tool"] for l in lines}, {"claude", "codex"})

    def test_it_writes_nothing_unless_asked(self):
        self.reading("claude", "claude-a", free=60.0)
        self.cli("--role", "default")
        self.assertFalse(os.path.exists(os.path.join(self.logs, "select.jsonl")))

    def test_the_walk_is_recorded_not_just_the_winner(self):
        # a skip for the wrong reason is the failure worth being able to see
        self.reading("claude", "claude-a", free=1.0)
        self.reading("claude", "claude-b", free=70.0)
        d = self.decisions("--role", "default")[("default", "claude")]
        self.assertEqual([s["account"] for s in d["considered"]],
                         ["claude-a", "claude-b"])
        self.assertIn("skipped", d["considered"][0])


    # --- money ---------------------------------------------------------------
    def test_an_account_past_its_included_allowance_is_passed_over(self):
        # the window can still look healthy while ordinary usage is barred:
        # what is left is paid overage, and these runs may not spend money
        self.reading("codex", "codex-a", free=90.0, ordinary=False)
        self.reading("codex", "codex-b", free=70.0)
        d = self.decisions("--role", "default")[("default", "codex")]
        self.assertEqual(d["chosen"], "codex-b")
        # the reason, not merely the skip: 90% free cannot be the threshold
        # branch, so only the overage branch can produce this
        self.assertIn("would spend credits", d["considered"][0]["skipped"])

    def test_overage_is_refused_even_as_the_last_account(self):
        self.reading("codex", "codex-a", free=90.0, ordinary=False)
        self.reading("codex", "codex-b", free=90.0, ordinary=False)
        d = self.decisions("--role", "default")[("default", "codex")]
        self.assertIsNone(d["chosen"])

    def test_a_tool_that_reports_no_allowance_field_is_not_refused(self):
        # Claude has no such field. Absent must not read as exhausted, or no
        # Claude account would ever be chosen.
        self.reading("claude", "claude-a", free=60.0)
        d = self.decisions("--role", "default")[("default", "claude")]
        self.assertEqual(d["chosen"], "claude-a")

    def test_allowed_ordinary_usage_is_not_a_reason_to_skip(self):
        self.reading("codex", "codex-a", free=60.0, ordinary=True)
        d = self.decisions("--role", "default")[("default", "codex")]
        self.assertEqual(d["chosen"], "codex-a")

    # --- answering a caller --------------------------------------------------
    def test_select_prints_one_line_a_tool_for_the_caller(self):
        self.reading("claude", "claude-a", free=60.0)
        self.reading("codex", "codex-a", free=60.0)
        out = self.cli("--select", "--role", "default")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        rows = [l.split("\t") for l in out.stdout.splitlines() if l.strip()]
        self.assertEqual({r[0] for r in rows}, {"claude", "codex"})
        by = {r[0]: r for r in rows}
        self.assertEqual(by["claude"][1], "claude-a")
        self.assertEqual(by["claude"][2], os.path.join(self.auth, "claude-a"))

    def test_select_says_nothing_usable_with_an_exit_code_of_its_own(self):
        # 3 is not 1: a caller has to tell "defer, try again later" from
        # "this policy is broken", and they are opposite responses
        self.reading("claude", "claude-a", free=1.0)
        self.reading("claude", "claude-b", free=1.0)
        self.reading("codex", "codex-a", free=60.0)
        out = self.cli("--select", "--role", "default")
        self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
        self.assertIn("no claude account usable", out.stderr)

    def test_select_answers_with_nothing_at_all_when_one_tool_is_short(self):
        # codex has quota and claude has none. Printing the half that worked
        # would let a caller run on it and skip the validation pass.
        self.reading("claude", "claude-a", free=1.0)
        self.reading("claude", "claude-b", free=1.0)
        self.reading("codex", "codex-a", free=60.0)
        out = self.cli("--select", "--role", "default")
        self.assertEqual(out.returncode, 3)
        self.assertEqual(out.stdout.strip(), "", out.stdout)

    def test_select_keeps_the_walk_off_the_answer(self):
        # stdout is parsed; a reason landing there would be read as an account
        self.reading("claude", "claude-a", free=1.0)
        self.reading("claude", "claude-b", free=60.0)
        self.reading("codex", "codex-a", free=60.0)
        out = self.cli("--select", "--role", "default")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("at or below", out.stdout)
        self.assertIn("at or below", out.stderr)
        self.assertEqual(len([l for l in out.stdout.splitlines() if l.strip()]), 2)

    def test_select_records_the_decision_it_deferred_on(self):
        # the run that did not happen is the one worth being able to explain
        self.reading("claude", "claude-a", free=1.0)
        self.reading("claude", "claude-b", free=1.0)
        self.assertEqual(self.cli("--select", "--record", "--role", "default").returncode, 3)
        with open(os.path.join(self.logs, "select.jsonl")) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertTrue(any(l["tool"] == "claude" and l["chosen"] is None for l in lines), lines)

    def test_select_needs_exactly_one_role(self):
        for args in (("--select",), ("--select", "--role", "default", "--role", "rsync")):
            with self.subTest(args=args):
                out = self.cli(*args)
                self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
                self.assertIn("exactly one --role", out.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
