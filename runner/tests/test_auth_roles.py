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


def sh(script, home, *args, path=None):
    # Built from nothing rather than inherited: a BASH_ENV that merely resets
    # PATH made the positive stub test run the real CLI and fail.
    env = {"HOME": home, "PATH": "/usr/bin:/bin", "SHELL": "/bin/bash",
           "LANG": "C.UTF-8"}
    if path:
        env["PATH"] = path + os.pathsep + env["PATH"]
    return subprocess.run(["bash", "-c", script, "_", *args],
                          capture_output=True, text=True, env=env)


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.auth = os.path.join(self.home, "review", "auth")
        os.makedirs(self.auth, mode=0o700)
        for d in ("claude-ardupilot", "claude-personal", "codex-personal"):
            os.makedirs(os.path.join(self.auth, d), mode=0o700)

    def stub_cli(self, email="admin@example.org"):
        """A claude that reports a signed-in identity.

        Without it account_of reaches the real CLI, which reads the fixture's
        made-up credentials as not signed in - so every status assertion could
        only ever be a negative one.
        """
        d = os.path.join(self.home, "stubs")
        os.makedirs(d, exist_ok=True)
        f = os.path.join(d, "claude")
        with open(f, "w") as fh:
            fh.write('#!/bin/sh\n'
                     "env | grep -oE '^(ANTHROPIC|CLAUDE)_[A-Z0-9_]+' > \"$HOME/cli-env\"\n"
                     'printf \'{"loggedIn": true, "email": "%s", "authMethod": "%s",'
                     ' "apiProvider": "%s", "configDirectory": "%s"}\\n\' '
                     '"${STUB_EMAIL:-' + email + '}" "${STUB_METHOD:-claude.ai}" '
                     '"${STUB_PROVIDER:-firstParty}" '
                     '"${STUB_DIR:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}}"\n')
        os.chmod(f, 0o755)
        return d

    def signed_in(self, name, email):
        d = os.path.join(self.auth, name)
        import json as _json
        _json.dump({"oauthAccount": {"emailAddress": email}},
                   open(os.path.join(d, ".claude.json"), "w"))
        open(os.path.join(d, "ACCOUNT"), "w").write(email + "\n")

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

    def status(self, path=None):
        return sh('"$1" status', self.home, AUTH_SH, path=path)

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

    def test_it_reports_the_address_a_signed_in_role_will_run_as(self):
        # the positive case: without it, an account_of that always answers
        # nothing satisfies every other assertion here
        self.link("claude-default", "claude-ardupilot")
        self.signed_in("claude-ardupilot", "admin@example.org")
        out = self.status(path=self.stub_cli())
        self.assertIn("admin@example.org", out.stdout)
        self.assertNotIn("NOT SIGNED IN", out.stdout)

    def test_a_record_the_runner_cannot_read_is_flagged(self):
        # an unreadable ACCOUNT record stops a run, so status may not show the
        # role as healthy
        self.link("claude-default", "claude-ardupilot")
        self.signed_in("claude-ardupilot", "admin@example.org")
        rec = os.path.join(self.auth, "claude-ardupilot", "ACCOUNT")
        os.remove(rec)
        os.mkdir(rec)
        out = self.status(path=self.stub_cli())
        self.assertIn("BAD ACCOUNT RECORD", out.stdout)

    def test_a_directory_that_disagrees_with_the_cli_is_flagged(self):
        self.link("claude-default", "claude-ardupilot")
        self.signed_in("claude-ardupilot", "someone@example.com")
        out = self.status(path=self.stub_cli("admin@example.org"))
        self.assertIn("CONFLICT", out.stdout)
        self.assertIn("someone@example.com", out.stdout)

    def test_a_missing_default_role_is_shown_as_the_tools_own_account(self):
        # the runner leaves CLAUDE_CONFIG_DIR unset here and runs as ~/.claude;
        # reporting "not set" describes neither the config nor the outcome
        os.makedirs(os.path.join(self.home, ".claude"), mode=0o700)
        out = self.status(path=self.stub_cli())
        line = [l for l in out.stdout.splitlines() if l.startswith("claude-default")][0]
        self.assertIn("~/.claude", line)
        self.assertIn("admin@example.org", line)

    def test_a_matching_identity_is_not_flagged_as_a_conflict(self):
        # flagging every role satisfies the conflict test on its own
        self.link("claude-default", "claude-ardupilot")
        self.signed_in("claude-ardupilot", "admin@example.org")
        out = self.status(path=self.stub_cli("admin@example.org"))
        self.assertNotIn("CONFLICT", out.stdout)
        self.assertNotIn("MISMATCH", out.stdout)

    def test_a_record_naming_another_account_is_flagged(self):
        self.link("claude-default", "claude-ardupilot")
        self.signed_in("claude-ardupilot", "admin@example.org")
        open(os.path.join(self.auth, "claude-ardupilot", "ACCOUNT"), "w") \
            .write("someone@example.com\n")
        out = self.status(path=self.stub_cli("admin@example.org"))
        self.assertIn("MISMATCH", out.stdout)
        self.assertIn("someone@example.com", out.stdout)

    def test_a_target_outside_the_root_is_not_shown_as_healthy(self):
        outside = os.path.join(self.home, "elsewhere")
        os.makedirs(outside)
        self.link("claude-default", outside)
        out = self.status(path=self.stub_cli())
        line = [l for l in out.stdout.splitlines() if l.startswith("claude-default")][0]
        self.assertIn("REFUSE", line)

    def test_an_unsafe_root_is_not_shown_as_healthy(self):
        self.link("claude-default", "claude-ardupilot")
        self.signed_in("claude-ardupilot", "admin@example.org")
        os.chmod(self.auth, 0o777)
        self.addCleanup(os.chmod, self.auth, 0o700)
        out = self.status(path=self.stub_cli())
        self.assertIn("REFUSE", out.stdout)
        self.assertNotIn("admin@example.org", out.stdout)

    def test_the_fallback_account_is_judged_like_any_other(self):
        # the missing-default branch used to print and continue, skipping every
        # check below it
        own = os.path.join(self.home, ".claude")
        os.makedirs(own, mode=0o700)
        open(os.path.join(own, "ACCOUNT"), "w").write("someone@example.com\n")
        out = self.status(path=self.stub_cli("admin@example.org"))
        line = [l for l in out.stdout.splitlines() if l.startswith("claude-default")][0]
        self.assertIn("MISMATCH", line)

    def test_a_fallback_with_no_credentials_is_not_shown_as_signed_in(self):
        os.makedirs(os.path.join(self.home, ".claude"), mode=0o700)
        out = self.status()
        line = [l for l in out.stdout.splitlines() if l.startswith("claude-default")][0]
        self.assertIn("NOT SIGNED IN", line)

    def test_a_token_login_is_not_shown_as_the_subscription(self):
        self.link("claude-default", "claude-ardupilot")
        self.signed_in("claude-ardupilot", "admin@example.org")
        out = self.status(path=self.stub_cli())
        self.assertIn("admin@example.org", out.stdout)
        out = sh('STUB_METHOD=oauth_token "$1" status', self.home, AUTH_SH,
                 path=self.stub_cli())
        self.assertIn("not the subscription", out.stdout)

    def test_it_asks_with_the_environment_the_runner_will_have(self):
        # reading an identity that an inherited override supplied would report
        # an account no run can use
        self.link("claude-default", "claude-ardupilot")
        self.signed_in("claude-ardupilot", "admin@example.org")
        sh('CLAUDE_SECURESTORAGE_CONFIG_DIR=/x "$1" status', self.home, AUTH_SH,
           path=self.stub_cli())
        with open(os.path.join(self.home, "cli-env")) as f:
            self.assertNotIn("CLAUDE_SECURESTORAGE_CONFIG_DIR", f.read().split())

    def test_credentials_read_from_another_directory_are_flagged(self):
        self.link("claude-default", "claude-ardupilot")
        self.signed_in("claude-ardupilot", "admin@example.org")
        d = self.stub_cli()
        # a stub that reports reading somewhere other than what it was given
        with open(os.path.join(d, "claude"), "a") as fh:
            pass
        out = sh('STUB_DIR="$2" "$1" status', self.home, AUTH_SH,
                 os.path.join(self.auth, "claude-personal"), path=d)
        self.assertIn("another directory", out.stdout)

    def test_an_identity_that_cannot_be_checked_is_flagged(self):
        # signed in, no address, and a record that therefore cannot hold
        self.link("claude-default", "claude-ardupilot")
        d = os.path.join(self.auth, "claude-ardupilot")
        open(os.path.join(d, "ACCOUNT"), "w").write("admin@example.org\n")
        out = sh('STUB_EMAIL= "$1" status', self.home, AUTH_SH,
                 path=self.stub_cli(""))
        self.assertIn("IDENTITY UNKNOWN", out.stdout)

    def test_a_cloud_provider_is_not_shown_as_the_subscription(self):
        self.link("claude-default", "claude-ardupilot")
        self.signed_in("claude-ardupilot", "admin@example.org")
        out = sh('STUB_PROVIDER=bedrock "$1" status', self.home, AUTH_SH,
                 path=self.stub_cli())
        self.assertIn("not the subscription", out.stdout)

    def test_a_custom_codex_provider_is_not_shown_as_the_account(self):
        d = os.path.join(self.auth, "codex-personal")
        import json as _json
        _json.dump({"tokens": {"account_id": "acct-1234"}},
                   open(os.path.join(d, "auth.json"), "w"))
        open(os.path.join(d, "config.toml"), "w").write(
            'model_provider = "probe"\n\n[model_providers.probe]\n'
            'base_url = "http://127.0.0.1:1/v1"\nenv_key = "PROBE_KEY"\n')
        self.link("codex-default", "codex-personal")
        out = self.status()
        line = [l for l in out.stdout.splitlines() if l.startswith("codex-default")][0]
        self.assertIn("config.toml", line)
        self.assertNotIn("acct-1234", line)

    def test_a_warning_on_stderr_is_not_part_of_the_directory(self):
        # the resolver warns and still succeeds for the tool's own directory
        own = os.path.join(self.home, ".claude")
        os.makedirs(own, mode=0o755)          # made by the tool, not by us
        os.rmdir(os.path.join(self.auth, "claude-personal"))
        os.symlink(own, os.path.join(self.auth, "claude-personal"))
        self.link("claude-default", "claude-personal")
        out = self.status(path=self.stub_cli())
        line = [l for l in out.stdout.splitlines() if l.startswith("claude-default")][0]
        self.assertIn("admin@example.org", line)
        self.assertNotIn("NOT SIGNED IN", line)

    def test_a_codex_api_key_is_not_shown_as_an_account(self):
        import json as _json
        d = os.path.join(self.auth, "codex-personal")
        _json.dump({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x",
                    "tokens": {"account_id": "acct-1234"}},
                   open(os.path.join(d, "auth.json"), "w"))
        self.link("codex-default", "codex-personal")
        out = self.status()
        line = [l for l in out.stdout.splitlines() if l.startswith("codex-default")][0]
        self.assertIn("API KEY", line)
        self.assertNotIn("acct-1234", line)

    def test_a_missing_non_default_role_is_shown_as_fatal(self):
        out = self.status()
        line = [l for l in out.stdout.splitlines() if l.startswith("claude-rsync")][0]
        self.assertIn("REFUSE", line)


class Switching(Base):
    """review-auth.sh use - the command reached for when quota runs out."""

    def use(self, tool, role, acct, path=None):
        return sh('"$1" use "$2" "$3" "$4"', self.home, AUTH_SH, tool, role, acct,
                  path=path)

    def leaves_root(self):
        """An account directory that only fails once the role points at it."""
        outside = os.path.join(self.home, "elsewhere")
        os.makedirs(outside, exist_ok=True)
        p = os.path.join(self.auth, "claude-out")
        if not os.path.islink(p):
            os.symlink(outside, p)
        return "out"

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

    def test_a_revert_to_an_option_shaped_target_is_not_claimed_falsely(self):
        # ln reads a leading - as an option: without --, the revert silently
        # does nothing while the command still says it reverted
        self.link("claude-default", "-old")
        out = self.use("claude", "default", self.leaves_root())
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(os.readlink(os.path.join(self.auth, "claude-default")), "-old")
        self.assertIn("reverted", out.stdout)

    def test_a_revert_that_fails_says_so(self):
        # the operator has to know the role is left on the rejected account
        d = os.path.join(self.home, "faulty")
        os.makedirs(d)
        f = os.path.join(d, "mv")
        with open(f, "w") as fh:
            fh.write("#!/bin/sh\ncase \"$*\" in *.revert.*) exit 1;; esac\n"
                     "exec /bin/mv \"$@\"\n")
        os.chmod(f, 0o755)
        self.link("claude-default", "claude-ardupilot")
        out = self.use("claude", "default", self.leaves_root(), path=d)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("COULD NOT REVERT", out.stdout)
        self.assertEqual(os.readlink(os.path.join(self.auth, "claude-default")),
                         "claude-out")

    def test_one_switch_at_a_time(self):
        # a slow switch that is going to fail must not revert over a switch that
        # succeeded while it ran
        import fcntl, threading, time
        lock = os.path.join(self.auth, ".claude-default.lock")
        fh = open(lock, "w")
        fcntl.flock(fh, fcntl.LOCK_EX)
        self.link("claude-default", "claude-ardupilot")
        done = []
        t = threading.Thread(target=lambda: done.append(
            self.use("claude", "default", "personal")))
        t.start()
        try:
            time.sleep(1.0)
            self.assertEqual(done, [], "the switch did not wait for the lock")
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
            t.join(30)
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].returncode, 0, done[0].stdout + done[0].stderr)

    def test_a_symlinked_lock_is_refused_rather_than_written_through(self):
        # a truncating open through a symlink empties whatever it points at
        victim = os.path.join(self.home, "credentials")
        open(victim, "w").write("secret\n")
        os.symlink(victim, os.path.join(self.auth, ".claude-default.lock"))
        self.link("claude-default", "claude-ardupilot")
        out = self.use("claude", "default", "personal")
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(open(victim).read(), "secret\n")
        self.assertEqual(os.readlink(os.path.join(self.auth, "claude-default")),
                         "claude-ardupilot")

    def test_an_unsafe_root_is_refused_before_the_lock_is_opened(self):
        os.chmod(self.auth, 0o777)
        self.addCleanup(os.chmod, self.auth, 0o700)
        self.link("claude-default", "claude-ardupilot")
        out = self.use("claude", "default", "personal")
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.auth, ".claude-default.lock")))

    def test_the_lock_is_held_until_the_switch_has_been_validated(self):
        # releasing it once acquired still passes a test that only proves
        # waiting; what matters is that nobody else can switch while this one
        # is between replacing the link and deciding whether to keep it
        import fcntl, threading, time
        slow = os.path.join(self.home, "slow")
        os.makedirs(slow)
        f = os.path.join(slow, "find")
        with open(f, "w") as fh:          # review_auth's root check calls find
            fh.write("#!/bin/sh\nsleep 2\nexec /usr/bin/find \"$@\"\n")
        os.chmod(f, 0o755)
        self.link("claude-default", "claude-ardupilot")
        done = []
        t = threading.Thread(target=lambda: done.append(
            self.use("claude", "default", "personal", path=slow)))
        t.start()
        try:
            time.sleep(2.5)               # inside the post-switch validation
            self.assertEqual(done, [], "the switch finished too early to test")
            self.assertEqual(os.readlink(os.path.join(self.auth, "claude-default")),
                             "claude-personal", "the link has not been replaced yet")
            held = False
            with open(os.path.join(self.auth, ".claude-default.lock"), "a") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fh, fcntl.LOCK_UN)
                except BlockingIOError:
                    held = True
            self.assertTrue(held, "the lock was released before validation")
        finally:
            t.join(60)

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
        # every switch, not just the first two: asserting the head alone lets a
        # "succeed without doing anything when the target is already right" pass
        self.assertEqual(targets, ["claude-personal" if i % 2 else "claude-ardupilot"
                                   for i in range(20)])

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
        # the lock is meant to persist; a half-made symlink is not
        leftovers = [f for f in os.listdir(self.auth)
                     if f.startswith(".claude-") and not f.endswith(".lock")]
        self.assertEqual(leftovers, [])
        for f in os.listdir(self.auth):
            self.assertFalse(os.path.islink(os.path.join(self.auth, f))
                             and f.startswith("."), f)


if __name__ == "__main__":
    unittest.main(verbosity=2)
