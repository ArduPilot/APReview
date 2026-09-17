#!/usr/bin/env python3
"""run-reviewprs.sh's account selection and pre-flight, driven end to end.

--dry-run does every pre-flight and starts nothing, so the guard can be exercised
against a throwaway home with stub `claude`, `codex` and `gh` on PATH. Without
this, deleting the runner's entire account-selection block left the suite green.
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(os.path.dirname(HERE), "bin")

SETTINGS = {"permissions": {"deny": ["Bash(git push)", "Bash(git push:*)"],
                            "defaultMode": "auto"}}


def stub(path, body):
    open(path, "w").write("#!/bin/bash\n" + body + "\n")
    os.chmod(path, 0o755)


class Guard(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        r = os.path.join(self.home, "review")
        for d in ("etc", "logs", "work", "data", "repositories"):
            os.makedirs(os.path.join(r, d), exist_ok=True)
        os.symlink(BIN, os.path.join(r, "bin"))
        self.auth = os.path.join(r, "auth")
        os.makedirs(self.auth, mode=0o700)

        # two Claude accounts and one Codex account, with credentials
        for name, email in (("claude-ardupilot", "admin@example.org"),
                            ("claude-personal", "someone@example.com")):
            d = os.path.join(self.auth, name)
            os.makedirs(d, mode=0o700)
            json.dump(SETTINGS, open(os.path.join(d, "settings.json"), "w"))
            json.dump({"claudeAiOauth": {"accessToken": "stub-token"}},
                      open(os.path.join(d, ".credentials.json"), "w"))
            json.dump({"oauthAccount": {"emailAddress": email}},
                      open(os.path.join(d, ".claude.json"), "w"))
            open(os.path.join(d, "ACCOUNT"), "w").write(email + "\n")
        cx = os.path.join(self.auth, "codex-personal")
        os.makedirs(cx, mode=0o700)
        json.dump({"tokens": {"account_id": "acct-1234"}},
                  open(os.path.join(cx, "auth.json"), "w"))

        self.link("claude-default", "claude-ardupilot")
        self.link("claude-rsync", "claude-personal")
        self.link("codex-default", "codex-personal")
        self.link("codex-rsync", "codex-personal")

        # stubs: report whichever account the selected directory records
        self.stubs = os.path.join(self.home, "stubs")
        os.makedirs(self.stubs)
        # STUB_CLI_EMAIL lets a test make the CLI disagree with the directory's
        # own record, which is the case worth stopping for: two local records
        # agreeing proves nothing about which subscription pays.
        stub(os.path.join(self.stubs, "claude"), '''
d="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
if [ "$1 $2" = "auth status" ]; then
    e="${STUB_CLI_EMAIL:-}"
    [ -n "$e" ] || e=$(python3 -c "
import json,sys
try: print(json.load(open('$d/.claude.json'))['oauthAccount']['emailAddress'])
except Exception: print('')" )
    if [ ! -s "$d/.credentials.json" ] || [ "$(cat "$d/.credentials.json")" = "{}" ]; then
        printf '{"loggedIn": false}\\n'; exit 0
    fi
    printf '{"loggedIn": true, "email": "%s"}\\n' "$e"
fi''')
        stub(os.path.join(self.stubs, "gh"), 'exit 0')
        stub(os.path.join(self.stubs, "codex"), 'exit 0')

    def link(self, name, target):
        p = os.path.join(self.auth, name)
        if os.path.islink(p):
            os.remove(p)
        os.symlink(target, p)

    def run_mode(self, mode, **env):
        # Built from nothing rather than inherited: BASH_ENV in an interactive
        # shell rewrote PATH and ran the real claude instead of the stub, so
        # these tests passed in CI and failed on a developer's machine.
        e = {"HOME": self.home,
             "PATH": self.stubs + ":/usr/bin:/bin",
             "SHELL": "/bin/bash",
             "LANG": "C.UTF-8"}
        e.update(env)
        return subprocess.run([os.path.join(BIN, "run-reviewprs.sh"), mode, "--dry-run"],
                              capture_output=True, text=True, env=e)

    # --- the selection itself ------------------------------------------------
    def test_a_default_run_uses_the_default_role(self):
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("admin@example.org", out.stdout)
        self.assertIn("role default", out.stdout)

    def test_an_rsync_run_uses_the_rsync_role(self):
        out = self.run_mode("rsync")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("someone@example.com", out.stdout)
        self.assertIn("role rsync", out.stdout)

    def test_a_pr_in_the_rsync_project_uses_the_rsync_role(self):
        out = self.run_mode("RsyncProject/rsync#1060")
        self.assertIn("someone@example.com", out.stdout)

    def test_an_inherited_config_dir_does_not_decide_the_account(self):
        other = os.path.join(self.auth, "claude-personal")
        out = self.run_mode("followup", CLAUDE_CONFIG_DIR=other)
        self.assertIn("admin@example.org", out.stdout)
        self.assertNotIn("someone@example.com", out.stdout)

    def test_an_inherited_config_dir_cannot_fill_an_unset_role(self):
        # the hole was here: with no link for the role, selection leaves the
        # tool's own default in place - and an inherited value then decided the
        # account, which the previous test could not see because the role
        # resolved to a directory that overwrote it anyway
        os.remove(os.path.join(self.auth, "claude-default"))
        other = os.path.join(self.auth, "claude-personal")
        out = self.run_mode("followup", CLAUDE_CONFIG_DIR=other)
        self.assertNotIn("someone@example.com", out.stdout)
        self.assertEqual(out.returncode, 1)     # the fake home has no ~/.claude

    def test_switching_the_role_switches_the_account(self):
        self.link("claude-default", "claude-personal")
        out = self.run_mode("followup")
        self.assertIn("someone@example.com", out.stdout)

    # --- what must stop a run ------------------------------------------------
    def test_a_missing_rsync_role_stops_the_run(self):
        os.remove(os.path.join(self.auth, "claude-rsync"))
        out = self.run_mode("rsync")
        self.assertEqual(out.returncode, 1)
        self.assertIn("status=wrong-claude-account", out.stdout)

    def test_an_environment_token_stops_the_run(self):
        out = self.run_mode("followup", CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-x")
        self.assertEqual(out.returncode, 1)
        self.assertIn("would", out.stdout + out.stderr)

    def test_a_directory_signed_in_as_someone_else_stops_the_run(self):
        d = os.path.join(self.auth, "claude-ardupilot")
        json.dump({"oauthAccount": {"emailAddress": "stranger@example.net"}},
                  open(os.path.join(d, ".claude.json"), "w"))
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1)
        self.assertIn("status=wrong-claude-account", out.stdout)

    def test_the_two_identity_sources_disagreeing_stops_the_run(self):
        # the directory records one address, the CLI reports another: which
        # subscription is about to be spent is then unknown
        out = self.run_mode("followup", STUB_CLI_EMAIL="stranger@example.net")
        self.assertEqual(out.returncode, 1)
        self.assertIn("status=wrong-claude-account", out.stdout)
        self.assertIn("but the CLI reports", out.stdout)

    def test_empty_claude_credentials_stop_the_run(self):
        # a file that exists and authenticates nobody: real claude reports
        # loggedIn false for this, so presence alone must not satisfy the guard
        open(os.path.join(self.auth, "claude-ardupilot", ".credentials.json"),
             "w").write("{}")
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1)
        self.assertIn("not signed in", out.stdout)

    def test_missing_claude_credentials_stop_the_run(self):
        os.remove(os.path.join(self.auth, "claude-ardupilot", ".credentials.json"))
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1)
        self.assertIn("not signed in", out.stdout)

    def test_missing_codex_credentials_stop_the_run(self):
        os.remove(os.path.join(self.auth, "codex-personal", "auth.json"))
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1)
        self.assertIn("status=wrong-codex-account", out.stdout)

    def test_a_codex_account_file_that_disagrees_stops_the_run(self):
        # a uuid, because that is what codex reports and what read_account_file
        # accepts - an "acct-9999" here is rejected as a malformed record and
        # never reaches the comparison
        open(os.path.join(self.auth, "codex-personal", "ACCOUNT"), "w").write(
            "99999999-9999-4999-9999-999999999999\n")
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1)
        self.assertIn("status=wrong-codex-account", out.stdout)

    def test_an_api_key_in_the_environment_stops_the_run(self):
        out = self.run_mode("followup", ANTHROPIC_API_KEY="sk-ant-api03-x")
        self.assertEqual(out.returncode, 1)

    def test_a_dangling_account_record_stops_the_run(self):
        # an identity constraint must not disappear because reading it failed
        d = os.path.join(self.auth, "claude-ardupilot")
        os.remove(os.path.join(d, "ACCOUNT"))
        os.symlink(os.path.join(d, "gone"), os.path.join(d, "ACCOUNT"))
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1)

    def test_an_account_record_that_is_a_directory_stops_the_run(self):
        d = os.path.join(self.auth, "codex-personal")
        os.makedirs(os.path.join(d, "ACCOUNT"))
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1)

    def test_an_over_long_account_record_stops_the_run(self):
        d = os.path.join(self.auth, "claude-ardupilot")
        open(os.path.join(d, "ACCOUNT"), "w").write("a@b.co" + "x" * 500 + "\n")
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1)

    def test_each_role_gets_its_own_codex_account(self):
        # codex was pinned to one account regardless of role
        other = os.path.join(self.auth, "codex-ardupilot")
        os.makedirs(other, mode=0o700)
        json.dump({"tokens": {"account_id": "acct-ardupilot"}},
                  open(os.path.join(other, "auth.json"), "w"))
        self.link("codex-default", "codex-ardupilot")
        self.assertIn("acct-ardupilot", self.run_mode("followup").stdout)
        self.assertIn("acct-1234", self.run_mode("rsync").stdout)

    def test_a_writable_auth_root_stops_the_run(self):
        os.chmod(self.auth, 0o777)
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1)

    def test_the_tools_own_directory_as_a_symlink_is_still_pinned(self):
        # if ~/.claude is itself a symlink it can be repointed under a run, so
        # the variable must be set to the resolved path rather than left unset
        own = os.path.join(self.home, ".claude")
        os.symlink(os.path.join(self.auth, "claude-ardupilot"), own)
        self.link("claude-default", "claude-ardupilot")
        out = self.run_mode("followup")
        self.assertIn("config " + os.path.join(self.auth, "claude-ardupilot"),
                      out.stdout)

    def test_a_world_readable_account_directory_stops_the_run(self):
        os.chmod(os.path.join(self.auth, "claude-ardupilot"), 0o755)
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
