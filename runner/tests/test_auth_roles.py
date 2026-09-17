#!/usr/bin/env python3
"""How a task resolves which account it runs as.

review_auth() is a shell function, so these run it through bash against a
throwaway auth directory. The failure that matters is silent: a role resolving
somewhere unintended means a run that works and spends the wrong subscription.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_SH = os.path.join(os.path.dirname(HERE), "bin", "review-env.sh")


class Roles(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.auth = os.path.join(self.home, "review", "auth")
        os.makedirs(self.auth)
        for d in ("claude-ardupilot", "claude-personal", "codex-personal"):
            os.makedirs(os.path.join(self.auth, d))

    def link(self, name, target):
        os.symlink(target, os.path.join(self.auth, name))

    def resolve(self, tool, role):
        out = subprocess.run(
            ["bash", "-c", '. "$1" >/dev/null 2>&1; review_auth "$2" "$3"',
             "_", ENV_SH, tool, role],
            capture_output=True, text=True, env=dict(os.environ, HOME=self.home))
        return out.stdout.strip(), out.returncode

    def test_a_role_resolves_to_what_its_symlink_points_at(self):
        self.link("claude-default", "claude-ardupilot")
        self.link("claude-rsync", "claude-personal")
        self.assertTrue(self.resolve("claude", "default")[0].endswith("claude-ardupilot"))
        self.assertTrue(self.resolve("claude", "rsync")[0].endswith("claude-personal"))

    def test_an_unset_role_falls_back_to_default(self):
        self.link("claude-default", "claude-ardupilot")
        path, rc = self.resolve("claude", "somenewrole")
        self.assertEqual(rc, 0)
        self.assertTrue(path.endswith("claude-ardupilot"))

    def test_no_default_either_means_no_answer_not_a_wrong_one(self):
        # the caller then leaves the tool's own default in place
        path, rc = self.resolve("claude", "default")
        self.assertEqual(path, "")
        self.assertNotEqual(rc, 0)

    def test_the_tools_do_not_share_a_role(self):
        self.link("claude-default", "claude-ardupilot")
        self.link("codex-default", "codex-personal")
        self.assertTrue(self.resolve("codex", "default")[0].endswith("codex-personal"))
        self.assertTrue(self.resolve("claude", "default")[0].endswith("claude-ardupilot"))

    def test_switching_the_symlink_switches_the_account(self):
        self.link("claude-default", "claude-ardupilot")
        before = self.resolve("claude", "default")[0]
        os.remove(os.path.join(self.auth, "claude-default"))
        self.link("claude-default", "claude-personal")
        after = self.resolve("claude", "default")[0]
        self.assertTrue(before.endswith("claude-ardupilot"))
        self.assertTrue(after.endswith("claude-personal"))

    def test_the_resolved_path_is_real_not_the_symlink(self):
        # run-reviewprs.sh compares it against ~/.claude to decide whether to set
        # CLAUDE_CONFIG_DIR at all, so it must be resolved
        self.link("claude-default", "claude-ardupilot")
        self.assertNotIn("claude-default", self.resolve("claude", "default")[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
