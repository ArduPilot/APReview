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
            open(os.path.join(d, ".credentials.json"), "w").write("{}")
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
    [ -s "$d/.credentials.json" ] || { echo '{\\"loggedIn\\": false}'; exit 0; }
    echo "{\\"loggedIn\\": true, \\"email\\": \\"$e\\"}"
fi''')
        stub(os.path.join(self.stubs, "gh"), 'exit 0')
        stub(os.path.join(self.stubs, "codex"), 'exit 0')

    def link(self, name, target):
        p = os.path.join(self.auth, name)
        if os.path.islink(p):
            os.remove(p)
        os.symlink(target, p)

    def run_mode(self, mode, **env):
        e = dict(os.environ, HOME=self.home,
                 PATH=self.stubs + os.pathsep + os.environ["PATH"])
        e.pop("CLAUDE_CONFIG_DIR", None)
        e.pop("CODEX_HOME", None)
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
        # an inherited value used to survive whenever the role resolved to the
        # tool's own directory, and then chose the account
        other = os.path.join(self.auth, "claude-personal")
        out = self.run_mode("followup", CLAUDE_CONFIG_DIR=other)
        self.assertIn("admin@example.org", out.stdout)
        self.assertNotIn("someone@example.com", out.stdout)

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
        open(os.path.join(self.auth, "codex-personal", "ACCOUNT"), "w").write("acct-9999\n")
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1)
        self.assertIn("status=wrong-codex-account", out.stdout)

    def test_a_world_readable_account_directory_stops_the_run(self):
        os.chmod(os.path.join(self.auth, "claude-ardupilot"), 0o755)
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
