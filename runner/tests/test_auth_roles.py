#!/usr/bin/env python3
"""Which account a task runs as, and what stops it running as the wrong one.

These drive the shell functions and review-auth.sh directly, against throwaway
homes. An earlier version checked path suffixes and ignored return codes, and a
mutation that deleted the runner's whole account-selection logic passed it.
"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(os.path.dirname(HERE), "bin")
ENV_SH = os.path.join(BIN, "review-env.sh")
AUTH_SH = os.path.join(BIN, "review-auth.sh")


def sh(script, home, *args):
    return subprocess.run(["bash", "-c", script, "_", *args],
                          capture_output=True, text=True,
                          env=dict(os.environ, HOME=home))


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.auth = os.path.join(self.home, "review", "auth")
        os.makedirs(self.auth, mode=0o700)
        for d in ("claude-ardupilot", "claude-personal", "codex-personal"):
            os.makedirs(os.path.join(self.auth, d), mode=0o700)

    def link(self, name, target):
        p = os.path.join(self.auth, name)
        if os.path.islink(p):
            os.remove(p)
        os.symlink(target, p)

    def resolve(self, tool, role):
        out = sh('. "$1" >/dev/null 2>&1; review_auth "$2" "$3"', self.home,
                 ENV_SH, tool, role)
        return out.stdout.strip(), out.returncode, out.stderr.strip()


class Resolution(Base):
    def test_a_role_resolves_to_exactly_its_targets_real_path(self):
        self.link("claude-default", "claude-ardupilot")
        path, rc, _ = self.resolve("claude", "default")
        self.assertEqual(rc, 0)
        self.assertEqual(path, os.path.realpath(
            os.path.join(self.auth, "claude-ardupilot")))

    def test_each_role_resolves_to_its_own_account(self):
        self.link("claude-default", "claude-ardupilot")
        self.link("claude-rsync", "claude-personal")
        self.assertEqual(self.resolve("claude", "rsync")[0],
                         os.path.realpath(os.path.join(self.auth, "claude-personal")))
        self.assertEqual(self.resolve("claude", "default")[0],
                         os.path.realpath(os.path.join(self.auth, "claude-ardupilot")))

    def test_a_missing_non_default_role_is_an_error_not_a_fallback(self):
        # falling back is how "the rsync target never spends the project's
        # subscription" would quietly stop being true
        self.link("claude-default", "claude-ardupilot")
        path, rc, err = self.resolve("claude", "rsync")
        self.assertEqual(rc, 2)
        self.assertEqual(path, "")
        self.assertIn("no account configured", err)

    def test_a_missing_default_role_leaves_the_tools_own_default(self):
        path, rc, _ = self.resolve("claude", "default")
        self.assertEqual(rc, 1)
        self.assertEqual(path, "")

    def test_a_dangling_link_is_an_error(self):
        self.link("claude-rsync", "claude-deleted")
        path, rc, err = self.resolve("claude", "rsync")
        self.assertEqual(rc, 2)
        self.assertIn("dangling", err)

    def test_a_target_outside_the_auth_root_is_refused(self):
        outside = os.path.join(self.home, "elsewhere")
        os.makedirs(outside, mode=0o700)
        self.link("claude-rsync", outside)
        path, rc, err = self.resolve("claude", "rsync")
        self.assertEqual(rc, 2)
        self.assertIn("outside", err)

    def test_a_directory_other_users_can_read_is_refused(self):
        os.chmod(os.path.join(self.auth, "claude-personal"), 0o755)
        self.link("claude-rsync", "claude-personal")
        path, rc, err = self.resolve("claude", "rsync")
        self.assertEqual(rc, 2)
        self.assertIn("accessible by other users", err)

    def test_a_target_that_is_a_file_is_refused(self):
        f = os.path.join(self.auth, "claude-notadir")
        open(f, "w").close()
        self.link("claude-rsync", "claude-notadir")
        self.assertEqual(self.resolve("claude", "rsync")[1], 2)

    def test_the_tools_do_not_share_a_role(self):
        self.link("claude-default", "claude-ardupilot")
        self.link("codex-default", "codex-personal")
        self.assertEqual(self.resolve("codex", "default")[0],
                         os.path.realpath(os.path.join(self.auth, "codex-personal")))

    def test_switching_the_link_switches_the_account(self):
        self.link("claude-default", "claude-ardupilot")
        before = self.resolve("claude", "default")[0]
        self.link("claude-default", "claude-personal")
        after = self.resolve("claude", "default")[0]
        self.assertNotEqual(before, after)
        self.assertTrue(after.endswith("claude-personal"))


class AccountRecords(Base):
    """ACCOUNT files name an account. They must not be able to leak a token."""

    def read(self, name, content, as_symlink_to=None):
        p = os.path.join(self.home, name)
        if as_symlink_to:
            os.symlink(as_symlink_to, p)
        else:
            open(p, "w").write(content)
        out = sh('. "$1" >/dev/null 2>&1; read_account_file "$2"', self.home, ENV_SH, p)
        return out.stdout.strip(), out.returncode

    def test_an_address_is_accepted(self):
        self.assertEqual(self.read("a", "admin@ardupilot.org\n"),
                         ("admin@ardupilot.org", 0))

    def test_a_uuid_is_accepted(self):
        v = "1e60e907-99df-4679-915f-30b3032ba24a"
        self.assertEqual(self.read("b", v + "\n"), (v, 0))

    def test_a_token_is_refused_rather_than_echoed(self):
        out, rc = self.read("c", "sk-ant-oat01-SECRET\n")
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")

    def test_a_symlink_to_credentials_is_refused(self):
        creds = os.path.join(self.home, "creds.json")
        open(creds, "w").write('{"accessToken": "sk-ant-oat01-SECRET"}')
        out, rc = self.read("d", "", as_symlink_to=creds)
        self.assertNotEqual(rc, 0)
        self.assertNotIn("SECRET", out)

    def test_a_symlink_is_refused_even_when_it_points_at_a_valid_record(self):
        # the format check alone would accept this; following symlinks out of the
        # account directory is what is being refused
        target = os.path.join(self.home, "elsewhere.txt")
        open(target, "w").write("someone@example.com\n")
        out, rc = self.read("e", "", as_symlink_to=target)
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")

    def test_a_long_record_is_refused_on_length_not_on_content(self):
        # every character here is legal in an address, so only the size limit
        # rejects it - the earlier test passed because the runner compared it
        # and found a mismatch, which proves nothing about this rule
        out, rc = self.read("f", "a@b." + "c" * 400 + "\n")
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")


class StatusView(Base):
    """review-auth.sh status - the thing an operator reads before switching."""

    def status(self):
        return sh('"$1" status', self.home, AUTH_SH)

    def test_it_lists_every_role(self):
        self.link("claude-default", "claude-ardupilot")
        out = self.status()
        self.assertEqual(out.returncode, 0, out.stderr)
        for role in ("claude-default", "claude-rsync", "codex-default", "codex-rsync"):
            self.assertIn(role, out.stdout)

    def test_a_role_with_no_link_is_shown_as_unset(self):
        out = self.status()
        self.assertIn("(not set)", out.stdout)

    def test_a_directory_with_no_credentials_is_not_reported_as_signed_in(self):
        self.link("claude-default", "claude-ardupilot")
        out = self.status()
        self.assertIn("NOT SIGNED IN", out.stdout)


class Switching(Base):
    """review-auth.sh use - the command reached for when quota runs out."""

    def use(self, tool, role, acct):
        return sh('"$1" use "$2" "$3" "$4"', self.home, AUTH_SH, tool, role, acct)

    def test_it_repoints_the_role(self):
        self.link("claude-default", "claude-ardupilot")
        out = self.use("claude", "default", "personal")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(os.readlink(os.path.join(self.auth, "claude-default")),
                         "claude-personal")

    def test_it_refuses_to_write_inside_a_real_directory(self):
        # `ln -sfn` into a directory silently creates the link inside it and
        # reports success, leaving the role pointing where it always did
        os.makedirs(os.path.join(self.auth, "claude-rsync"), mode=0o700)
        out = self.use("claude", "rsync", "personal")
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(os.path.islink(os.path.join(self.auth, "claude-rsync")))
        self.assertFalse(os.path.exists(
            os.path.join(self.auth, "claude-rsync", "claude-personal")))

    def test_an_unknown_account_is_refused(self):
        out = self.use("claude", "default", "nosuch")
        self.assertNotEqual(out.returncode, 0)

    def test_a_role_pointed_at_itself_is_refused(self):
        self.link("claude-default", "claude-ardupilot")
        out = self.use("claude", "default", "default")
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(os.readlink(os.path.join(self.auth, "claude-default")),
                         "claude-ardupilot")

    def test_an_account_that_leaves_the_root_is_put_back(self):
        # reaches the post-switch check: the account directory looks fine on its
        # own, and only resolving it as the role shows it leaves the auth root
        outside = os.path.join(self.home, "elsewhere")
        os.makedirs(outside, exist_ok=True)
        os.symlink(outside, os.path.join(self.auth, "claude-out"))
        self.link("claude-default", "claude-ardupilot")
        out = self.use("claude", "default", "out")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("reverted", out.stdout)
        self.assertEqual(os.readlink(os.path.join(self.auth, "claude-default")),
                         "claude-ardupilot")

    def test_a_reader_never_sees_the_role_missing_during_a_switch(self):
        # unlink-then-symlink leaves a window in which a starting run resolves
        # nothing; the replacement must be atomic
        import threading
        self.link("claude-default", "claude-ardupilot")
        seen = []
        stop = threading.Event()

        def watch():
            p = os.path.join(self.auth, "claude-default")
            while not stop.is_set():
                seen.append(os.path.islink(p))

        t = threading.Thread(target=watch)
        t.start()
        targets = []
        try:
            for i in range(20):
                acct = "personal" if i % 2 else "ardupilot"
                out = self.use("claude", "default", acct)
                self.assertEqual(out.returncode, 0, out.stderr)
                targets.append(os.readlink(os.path.join(self.auth, "claude-default")))
        finally:
            stop.set()
            t.join()
        self.assertTrue(seen, "watcher never ran")
        self.assertNotIn(False, seen, "the role vanished mid-switch")
        # a switch that never happens would also never be seen missing
        self.assertEqual(targets[0], "claude-ardupilot")   # i=0 -> ardupilot
        self.assertEqual(targets[1], "claude-personal")
        self.assertEqual(len(set(targets)), 2)

    def test_login_creates_a_private_directory(self):
        out = sh('"$1" login claude newacct', self.home, AUTH_SH)
        self.assertEqual(out.returncode, 0, out.stderr)
        d = os.path.join(self.auth, "claude-newacct")
        self.assertTrue(os.path.isdir(d))
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.auth).st_mode), 0o700)

    def test_it_leaves_no_temporary_link_behind(self):
        self.link("claude-default", "claude-ardupilot")
        self.use("claude", "default", "personal")
        leftovers = [f for f in os.listdir(self.auth) if f.startswith(".claude-")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
