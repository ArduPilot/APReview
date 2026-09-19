#!/usr/bin/env python3
"""Which account a task runs as, and what stops it running as the wrong one.

These drive the shell functions and review-auth.sh directly, against throwaway
homes. An earlier version checked path suffixes and ignored return codes, and a
mutation that deleted the runner's whole account-selection logic passed it.
"""
import os
import json
import shutil
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(os.path.dirname(HERE), "bin")
ENV_SH = os.path.join(BIN, "review-env.sh")
AUTH_SH = os.path.join(BIN, "review-auth.sh")


def sh(script, home, *args, path=None, **extra):
    # Built from nothing rather than inherited: a BASH_ENV that merely resets
    # PATH made the positive stub test run the real CLI and fail.
    env = {"HOME": home, "PATH": "/usr/bin:/bin", "SHELL": "/bin/bash",
           "LANG": "C.UTF-8"}
    if path:
        env["PATH"] = path + os.pathsep + env["PATH"]
    env.update(extra)
    return subprocess.run(["bash", "-c", script, "_", *args],
                          capture_output=True, text=True, env=env)


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.auth = os.path.join(self.home, "review.auth")
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
                     "env | grep -oE '^(ANTHROPIC|CLAUDE|OPENAI|CODEX)_[A-Z0-9_]+'"
                     ' > "$HOME/cli-env"\n'
                     'if [ -n "${STUB_PART_META:-}" ]; then\n'
                     '  printf \'{"loggedIn": true, "email": "x@y.z",'
                     ' "authMethod": "claude.ai"}\\n\'; exit 0\nfi\n'
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
        _json.dump({"tokens": {"access_token": "stub-access", "refresh_token": "stub-refresh",
                              "id_token": "e30.e30.c3R1Yg", "account_id": "11111111-1111-4111-8111-111111111111"}},
                   open(os.path.join(d, "auth.json"), "w"))
        open(os.path.join(d, "config.toml"), "w").write(
            'model_provider = "probe"\n\n[model_providers.probe]\n'
            'base_url = "http://127.0.0.1:1/v1"\nenv_key = "PROBE_KEY"\n')
        self.link("codex-default", "codex-personal")
        out = self.status()
        line = [l for l in out.stdout.splitlines() if l.startswith("codex-default")][0]
        self.assertIn("config.toml", line)
        self.assertNotIn("11111111-1111-4111-8111-111111111111", line)

    def test_a_partial_answer_from_the_cli_is_flagged(self):
        # the runner refuses it; a healthy row here would be the reassuring
        # half of a broken setup
        self.link("claude-default", "claude-ardupilot")
        self.signed_in("claude-ardupilot", "admin@example.org")
        out = sh('STUB_PART_META=1 "$1" status', self.home, AUTH_SH,
                 path=self.stub_cli())
        self.assertIn("only in part", out.stdout)

    def test_a_credential_variable_that_cannot_be_unset_is_reported(self):
        self.link("claude-default", "claude-ardupilot")
        self.signed_in("claude-ardupilot", "admin@example.org")
        # readonly is a shell attribute, so it has to come from the file the
        # scripts source, the way it would on the box
        etc = os.path.join(self.home, "review", "etc")
        os.makedirs(etc, exist_ok=True)
        open(os.path.join(etc, "local.conf"), "w") \
            .write("readonly ANTHROPIC_AUTH_TOKEN=x\n")
        out = self.status(path=self.stub_cli())
        self.assertIn("ANTHROPIC_AUTH_TOKEN", out.stdout)
        self.assertIn("runs will refuse", out.stdout)
        self.assertEqual(out.returncode, 1)
        self.assertFalse(os.path.exists(os.path.join(self.home, "cli-env")))

    def test_a_redirected_chatgpt_endpoint_is_flagged(self):
        d = os.path.join(self.auth, "codex-personal")
        import json as _json
        _json.dump({"tokens": {"access_token": "stub-access", "refresh_token": "stub-refresh",
                              "id_token": "e30.e30.c3R1Yg", "account_id": "11111111-1111-4111-8111-111111111111"}},
                   open(os.path.join(d, "auth.json"), "w"))
        open(os.path.join(d, "config.toml"), "w").write(
            'chatgpt_base_url = "http://127.0.0.1:1/backend-api"\n')
        self.link("codex-default", "codex-personal")
        out = self.status()
        line = [l for l in out.stdout.splitlines() if l.startswith("codex-default")][0]
        self.assertIn("config.toml", line)
        self.assertNotIn("11111111-1111-4111-8111-111111111111", line)

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
                    "tokens": {"access_token": "stub-access", "refresh_token": "stub-refresh",
                              "id_token": "e30.e30.c3R1Yg", "account_id": "11111111-1111-4111-8111-111111111111"}},
                   open(os.path.join(d, "auth.json"), "w"))
        self.link("codex-default", "codex-personal")
        out = self.status()
        line = [l for l in out.stdout.splitlines() if l.startswith("codex-default")][0]
        self.assertIn("API KEY", line)
        self.assertNotIn("11111111-1111-4111-8111-111111111111", line)

    def test_a_missing_non_default_role_is_shown_as_fatal(self):
        out = self.status()
        line = [l for l in out.stdout.splitlines() if l.startswith("claude-rsync")][0]
        self.assertIn("REFUSE", line)


class AuthRootMove(unittest.TestCase):
    """The one-time move of the accounts out of $REVIEW_ROOT.

    It renames a directory holding live credentials and rewrites the deny rule
    every account carries, so it gets the same treatment as anything else here.
    """

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.old = os.path.join(self.home, "review", "auth")
        self.new = os.path.join(self.home, "review.auth")
        os.makedirs(os.path.join(self.home, "review", "bin"))
        for b in ("review-env.sh", "migrate-auth-root.sh"):
            os.symlink(os.path.join(BIN, b),
                       os.path.join(self.home, "review", "bin", b))
        os.makedirs(self.old, mode=0o700)

    def account(self, name, deny=("Bash(git push)", "Bash(git push:*)",
                                  "Read(~/review/auth/**)")):
        d = os.path.join(self.old, name)
        os.makedirs(d, mode=0o700, exist_ok=True)
        with open(os.path.join(d, "settings.json"), "w") as f:
            json.dump({"permissions": {"defaultMode": "auto", "deny": list(deny)}}, f)
        return d

    def migrate(self, *args, **extra):
        return subprocess.run(
            [os.path.join(self.home, "review", "bin", "migrate-auth-root.sh"), *args],
            capture_output=True, text=True,
            env={"HOME": self.home, "PATH": "/usr/bin:/bin", **extra})

    def deny(self, path):
        with open(path) as f:
            return json.load(f)["permissions"]["deny"]

    def test_it_moves_the_accounts_and_repoints_the_rule(self):
        self.account("claude-ardupilot")
        os.symlink("claude-ardupilot", os.path.join(self.old, "claude-default"))
        out = self.migrate()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertFalse(os.path.exists(self.old))
        self.assertIn("Read(~/review.auth/**)",
                      self.deny(os.path.join(self.new, "claude-ardupilot",
                                             "settings.json")))
        # a relative role link survives the rename
        self.assertEqual(os.readlink(os.path.join(self.new, "claude-default")),
                         "claude-ardupilot")

    def test_existing_denials_are_preserved(self):
        self.account("claude-personal", deny=("Bash(git push)", "Bash(git push:*)",
                                              "Read(~/review/**)", "Edit(//elsewhere/review/auth/**)"))
        self.migrate()
        rules = self.deny(os.path.join(self.new, "claude-personal", "settings.json"))
        self.assertIn("Read(~/review.auth/**)", rules)
        self.assertIn("Read(~/review/**)", rules)
        self.assertIn("Edit(//elsewhere/review/auth/**)", rules)

    def test_a_dry_run_changes_nothing_but_names_every_edit(self):
        self.account("claude-ardupilot")
        before = self.snapshot()
        out = self.migrate("--dry-run")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("claude-ardupilot", out.stdout)   # the edit it would make
        self.assertTrue(os.path.isdir(self.old))
        self.assertFalse(os.path.exists(self.new))
        self.assertIn("Read(~/review/auth/**)",
                      self.deny(os.path.join(self.old, "claude-ardupilot",
                                             "settings.json")))

    def test_it_refuses_to_merge_two_account_roots(self):
        self.account("claude-ardupilot")
        os.makedirs(self.new, mode=0o700)
        out = self.migrate()
        self.assertNotEqual(out.returncode, 0)
        self.assertTrue(os.path.isdir(self.old), "it moved anyway")

    def test_running_it_twice_is_harmless(self):
        self.account("claude-ardupilot")
        self.assertEqual(self.migrate().returncode, 0)
        before = self.snapshot()
        again = self.migrate()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertIn("already at", again.stdout)
        self.assertIn("Read(~/review.auth/**)",
                      self.deny(os.path.join(self.new, "claude-ardupilot",
                                             "settings.json")))

    def snapshot(self):
        result = {}
        for directory, dirs, files in os.walk(self.home):
            for name in dirs + files:
                p = os.path.join(directory, name)
                st = os.lstat(p)
                value = os.readlink(p) if stat.S_ISLNK(st.st_mode) else None
                if stat.S_ISREG(st.st_mode):
                    with open(p, "rb") as f:
                        value = f.read()
                result[os.path.relpath(p, self.home)] = (st.st_mode, st.st_mtime_ns,
                                                        st.st_ctime_ns, value)
        return result

    def fault(self, code):
        # Faults are injected into the interpreter, never production switches.
        d = os.path.join(self.home, "faults")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "python3")
        with open(p, "w") as f:
            f.write('#!/usr/bin/python3\nimport os, sys, errno\n'
                    + code + '\nsys.argv = sys.argv[1:]\n'
                    'exec(compile(sys.stdin.read(), "migration", "exec"))\n')
        os.chmod(p, 0o700)
        return d + ":/usr/bin:/bin"

    def test_all_settings_are_validated_before_any_write(self):
        self.account("claude-a")
        bad = self.account("claude-z")
        with open(os.path.join(bad, "settings.json"), "w") as f:
            f.write('{"permissions": {"deny": {}}}')
        before = self.snapshot()
        out = self.migrate()
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn("invalid settings", out.stderr)
        self.assertIn("claude-z", out.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_an_unreadable_inventory_is_not_an_empty_root(self):
        self.account("claude-a")
        path = self.fault('''def scandir(path):
    raise PermissionError(errno.EACCES, "injected unreadable inventory", path)
os.scandir = scandir''')
        before = self.snapshot()
        out = self.migrate(PATH=path)
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn("unreadable inventory", out.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_a_failed_write_is_reported_and_a_rerun_finishes(self):
        self.account("claude-a")
        self.account("claude-z")
        path = self.fault('''real_replace = os.replace
def replace(src, dst):
    if dst.endswith("claude-z/settings.json"):
        raise OSError(errno.EIO, "injected settings failure")
    return real_replace(src, dst)
os.replace = replace''')
        out = self.migrate(PATH=path)
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn("injected settings failure", out.stderr)
        self.assertNotIn("done.", out.stdout)
        self.assertFalse(os.path.exists(self.old))
        a = os.path.join(self.new, "claude-a", "settings.json")
        z = os.path.join(self.new, "claude-z", "settings.json")
        self.assertIn("Read(~/review.auth/**)", self.deny(a))
        self.assertNotIn("Read(~/review.auth/**)", self.deny(z))
        backup = open(a + ".pre-authmove", "rb").read()
        again = self.migrate()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("Read(~/review.auth/**)", self.deny(z))
        self.assertEqual(open(a + ".pre-authmove", "rb").read(), backup)

    def test_an_already_migrated_box_needs_no_writes(self):
        self.account("claude-a", deny=("Bash(git push)", "Bash(git push:*)",
                                       "Read(~/review.auth/**)"))
        os.rename(self.old, self.new)
        before = self.snapshot()
        out = self.migrate()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_an_interruption_leaves_complete_settings_and_can_resume(self):
        a = self.account("claude-a")
        with open(os.path.join(a, "settings.json"), "rb") as f:
            original = f.read()
        path = self.fault('''real_replace = os.replace
def replace(src, dst):
    if dst.endswith("/settings.json"):
        os._exit(91)
    return real_replace(src, dst)
os.replace = replace''')
        out = self.migrate(PATH=path)
        self.assertEqual(out.returncode, 91, out.stdout + out.stderr)
        self.assertNotIn("done.", out.stdout)
        a = os.path.join(self.new, "claude-a", "settings.json")
        with open(a, "rb") as f:
            self.assertEqual(f.read(), original)
        with open(a + ".pre-authmove", "rb") as f:
            self.assertEqual(f.read(), original)
        again = self.migrate()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("Read(~/review.auth/**)", self.deny(a))

    def test_custom_roots_are_not_globs(self):
        self.account("claude-a")
        new = os.path.join(self.home, "review.[auth]")
        # A resumed migration scans the new root, not the old plain pathname.
        os.rename(self.old, new)
        out = self.migrate("--auth-root", new)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(r"Read(~/review.\[auth\]/**)",
                      self.deny(os.path.join(new, "claude-a", "settings.json")))

    def test_symlink_roots_are_refused_without_moving(self):
        self.account("claude-a")
        secret = os.path.join(self.home, "review", "secret")
        os.rename(self.old, secret)
        os.symlink(secret, self.old)
        before = self.snapshot()
        out = self.migrate()
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn("not symlinks", out.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_a_destination_alias_into_review_is_refused(self):
        self.account("claude-a")
        alias = os.path.join(self.home, "alias")
        os.symlink(os.path.join(self.home, "review"), alias)
        before = self.snapshot()
        out = self.migrate("--auth-root", os.path.join(alias, "secret"))
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn("inside the granted", out.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_resuming_cannot_treat_home_as_the_account_root(self):
        self.account("claude-a")
        os.rename(self.old, self.new)
        before = self.snapshot()
        out = self.migrate("--auth-root", self.home)
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn("destination contains the review root", out.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_an_unrepresentable_destination_is_refused_before_moving(self):
        self.account("claude-a")
        before = self.snapshot()
        out = self.migrate("--auth-root", os.path.join(self.home, "auth\nroot"))
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn("supported permission rule", out.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_cross_filesystem_rename_failure_never_copies(self):
        self.account("claude-a")
        path = self.fault('''def rename(src, dst):
    raise OSError(errno.EXDEV, "injected cross-filesystem rename")
os.rename = rename''')
        before = self.snapshot()
        out = self.migrate(PATH=path)
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn("cross-filesystem", out.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_dry_run_refuses_different_filesystems(self):
        self.account("claude-a")
        path = self.fault('''real_stat = os.stat
def device_stat(path, *args, **kwargs):
    value = real_stat(path, *args, **kwargs)
    if path == os.path.join(os.environ["HOME"], "review", "auth"):
        fields = list(value)
        fields[2] += 1
        return os.stat_result(fields)
    return value
os.stat = device_stat''')
        before = self.snapshot()
        out = self.migrate("--dry-run", PATH=path)
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn("cross-filesystem move refused", out.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_the_moved_root_is_checked_again_before_writes(self):
        self.account("claude-a")
        path = self.fault('''real_rename = os.rename
def rename(src, dst):
    target = os.path.join(os.environ["HOME"], "review", "secret")
    real_rename(src, target)
    os.symlink(target, dst)
os.rename = rename''')
        out = self.migrate(PATH=path)
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn("moved root is inside", out.stderr)
        self.assertNotIn("Read(~/review.auth/**)", self.deny(
            os.path.join(self.home, "review", "secret", "claude-a", "settings.json")))

    def test_links_whose_meaning_changes_are_refused_before_the_move(self):
        a = self.account("claude-a")
        own = os.path.join(self.home, ".codex")
        os.makedirs(own, mode=0o700)
        bridge = os.path.join(self.home, "bridge")
        os.symlink(a, bridge)
        cases = [(os.path.join(self.old, "claude-default"), a, "absolute symlink"),
                 (os.path.join(self.old, "claude-default"), bridge, "absolute symlink"),
                 (os.path.join(self.home, ".claude"), a, "tool home symlink"),
                 (os.path.join(self.old, "codex-personal"), "../../.codex", "relative symlink")]
        for link, target, diagnostic in cases:
            with self.subTest(link=link):
                os.symlink(target, link)
                before = self.snapshot()
                out = self.migrate()
                self.assertNotEqual(out.returncode, 0, out.stdout)
                self.assertIn(diagnostic, out.stderr)
                self.assertEqual(self.snapshot(), before)
                os.unlink(link)

    def test_a_dangling_link_is_refused_before_the_move(self):
        self.account("claude-a")
        os.symlink("missing", os.path.join(self.old, "codex-default"))
        before = self.snapshot()
        out = self.migrate()
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn("dangling symlink", out.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_planted_write_destinations_cannot_overwrite_credentials(self):
        a = self.account("claude-a")
        credential = os.path.join(a, ".credentials.json")
        with open(credential, "w") as f:
            f.write("synthetic credential")
        for suffix in (".new", ".pre-authmove"):
            with self.subTest(suffix=suffix):
                link = os.path.join(a, "settings.json" + suffix)
                os.symlink(".credentials.json", link)
                before = self.snapshot()
                out = self.migrate()
                self.assertNotEqual(out.returncode, 0, out.stdout)
                self.assertIn("not a plain file", out.stderr)
                self.assertEqual(self.snapshot(), before)
                os.unlink(link)

    def test_deployed_environment_is_not_needed(self):
        self.account("claude-a")
        p = os.path.join(self.home, "review", "bin", "review-env.sh")
        os.unlink(p)
        with open(p, "w") as f:
            f.write('export REVIEW_AUTH="$HOME/review/auth"\n')
        out = self.migrate()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertTrue(os.path.isdir(self.new))

    def test_a_name_planted_after_validation_cannot_redirect_a_write(self):
        a = self.account("claude-a")
        with open(os.path.join(a, ".credentials.json"), "w") as f:
            f.write("synthetic credential")
        path = self.fault('''real_rename = os.rename
def rename(src, dst):
    real_rename(src, dst)
    os.symlink(".credentials.json", os.path.join(dst, "claude-a", "settings.json.new"))
os.rename = rename''')
        out = self.migrate(PATH=path)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        a = os.path.join(self.new, "claude-a")
        self.assertEqual(open(os.path.join(a, ".credentials.json")).read(), "synthetic credential")
        self.assertIn("Read(~/review.auth/**)", self.deny(os.path.join(a, "settings.json")))
        self.assertEqual(os.stat(os.path.join(a, "settings.json.pre-authmove")).st_mode & 0o777, 0o600)


class UsageProbe(Base):
    """The hourly meter has to follow the role, or a switch freezes it."""

    def resolved(self, role="default", **env):
        out = sh('. "$1" >/dev/null 2>&1; role_config_dir claude "$2"',
                 self.home, ENV_SH, role, **env)
        return out.stdout.strip()

    def test_it_reads_the_account_the_default_role_selects(self):
        self.link("claude-default", "claude-personal")
        self.assertEqual(self.resolved(),
                         os.path.join(self.auth, "claude-personal"))

    def test_it_follows_the_role_when_the_link_moves(self):
        # `use claude default personal` is the headline command; a meter that
        # keeps sampling the old account is worse than no meter, because the
        # frozen reading looks like a real one
        self.link("claude-default", "claude-ardupilot")
        first = self.resolved()
        self.link("claude-default", "claude-personal")
        self.assertNotEqual(first, self.resolved())
        self.assertEqual(self.resolved(),
                         os.path.join(self.auth, "claude-personal"))

    def test_the_tools_own_directory_reads_as_leave_it_unset(self):
        # setting CLAUDE_CONFIG_DIR to ~/.claude makes the CLI report no
        # address at all, so the answer there is emptiness, not the path
        own = os.path.join(self.home, ".claude")
        os.makedirs(own, mode=0o700)
        os.rmdir(os.path.join(self.auth, "claude-ardupilot"))
        os.symlink(own, os.path.join(self.auth, "claude-ardupilot"))
        self.link("claude-default", "claude-ardupilot")
        self.assertEqual(self.resolved(), "")

    def probe(self, **env):
        """Run the probe with a stub CLI that reports whatever directory it read."""
        d = os.path.join(self.home, "stubs")
        os.makedirs(d, exist_ok=True)
        f = os.path.join(d, "claude")
        with open(f, "w") as fh:
            fh.write('#!/bin/sh\n'
                     'echo "$1 $2" >> "$HOME/probe-cli-calls"\n'
                     'e="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"\n'
                     'case "$1 $2" in\n'
                     '  "auth status") printf \'{"loggedIn": true, "email": "%s@x.y"}\\n\''
                     ' "$(basename "$e")"; exit 0 ;;\n'
                     'esac\n'
                     'echo "Current session: 7%"\n'
                     'echo "Current week (all models): 11%"\n')
        os.chmod(f, 0o755)
        os.makedirs(os.path.join(self.home, "review", "logs"), exist_ok=True)
        os.makedirs(os.path.join(self.home, "review", "bin"), exist_ok=True)
        for n in ("review-env.sh", "claude-usage-probe.sh"):
            link = os.path.join(self.home, "review", "bin", n)
            if not os.path.exists(link):
                os.symlink(os.path.join(BIN, n), link)
        self.probe_result = sh('"$1" hourly', self.home,
           os.path.join(self.home, "review", "bin", "claude-usage-probe.sh"),
           path=d, **env)
        p = os.path.join(self.home, "review", "logs", "claude-usage.jsonl")
        if not os.path.exists(p):
            return []
        import json as _json
        with open(p) as fh:
            return [_json.loads(l) for l in fh if l.strip()]

    def test_the_hourly_probe_reads_the_account_the_role_selects(self):
        # the hourly trace has no environment from a run, so it must resolve the
        # role itself - otherwise it samples ~/.claude for ever after a switch
        self.link("claude-default", "claude-personal")
        recs = self.probe()
        self.assertTrue(recs, "the probe recorded nothing")
        self.assertEqual(recs[-1].get("account"), "claude-personal@x.y")

    def test_a_broken_hourly_role_does_not_probe_the_fallback(self):
        os.makedirs(os.path.join(self.home, ".claude"), mode=0o700)
        self.link("claude-default", "missing")
        self.assertEqual(self.probe(), [])
        self.assertEqual(self.probe_result.returncode, 1)
        self.assertIn("cannot use the claude account", self.probe_result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, "probe-cli-calls")))

    def test_an_unconfigured_hourly_default_still_probes_the_tools_home(self):
        os.makedirs(os.path.join(self.home, ".claude"), mode=0o700)
        recs = self.probe()
        self.assertEqual(self.probe_result.returncode, 0, self.probe_result.stderr)
        self.assertTrue(recs)
        self.assertEqual(recs[-1].get("account"), ".claude@x.y")

    def test_a_run_using_the_tools_home_does_not_follow_a_later_switch(self):
        os.makedirs(os.path.join(self.home, ".claude"), mode=0o700)
        self.link("claude-default", "claude-personal")
        recs = self.probe(REVIEW_ROLE="default")
        self.assertTrue(recs)
        self.assertEqual(recs[-1].get("account"), ".claude@x.y")

    def test_a_run_probe_keeps_the_account_the_run_selected(self):
        # inside a run there is nothing to decide: the run already chose, and
        # re-resolving would misdirect the rsync probes to the default account
        self.link("claude-default", "claude-personal")
        recs = self.probe(CLAUDE_CONFIG_DIR=os.path.join(self.auth, "claude-ardupilot"),
                          REVIEW_ROLE="rsync")
        self.assertTrue(recs, "the probe recorded nothing")
        self.assertEqual(recs[-1].get("account"), "claude-ardupilot@x.y")

    def test_an_unresolvable_role_reads_as_the_tools_own_directory(self):
        # the probe only reads a meter: it reports what a run would use, and a
        # run of that role refuses separately
        os.makedirs(os.path.join(self.home, ".claude"), mode=0o700)
        self.assertEqual(self.resolved("rsync"), "")


class Switching(Base):
    """review-auth.sh use - the command reached for when quota runs out."""

    def use(self, tool, role, acct, path=None):
        return sh('"$1" use "$2" "$3" "$4"', self.home, AUTH_SH, tool, role, acct,
                  path=path)

    def test_unknown_tools_cannot_create_login_directories(self):
        before = sorted(os.listdir(self.auth))
        for tool in ("../../work/x", "other", "Claude", ""):
            with self.subTest(tool=tool):
                out = sh('"$1" login "$2" personal', self.home, AUTH_SH, tool)
                self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
                self.assertIn("unknown tool:", out.stdout)
                self.assertEqual(sorted(os.listdir(self.auth)), before)
                self.assertFalse(os.path.exists(os.path.join(self.home, "work")))

    def test_unknown_tools_cannot_switch_roles(self):
        # Make the directory exist: a missing account must not be what refuses
        # this call, and traversal must be stopped before any lock or link.
        os.makedirs(os.path.join(self.home, "work", "x-personal"), mode=0o700)
        for tool in ("../../work/x", "other", "Claude", ""):
            with self.subTest(tool=tool):
                out = self.use(tool, "default", "personal")
                self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
                self.assertIn("unknown tool:", out.stdout)
                self.assertFalse(os.path.lexists(os.path.join(self.home, "work", "x-default")))
                self.assertFalse(any(n.endswith(".lock") for n in os.listdir(self.auth)))

    def test_role_and_account_names_are_checked_before_paths_are_used(self):
        self.link("claude-default", "claude-ardupilot")
        for name in ("", ".", "..", "-option", "../../work/x", "has space"):
            for command in (("use", "claude", name, "personal"),
                            ("use", "claude", "default", name), ("login", "claude", name)):
                with self.subTest(command=command):
                    out = sh('"$@"', self.home, AUTH_SH, *command)
                    self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
                    self.assertIn("not a plain name", out.stderr)
                    self.assertEqual(os.readlink(os.path.join(self.auth, "claude-default")),
                                     "claude-ardupilot")
                    self.assertFalse(any(n.endswith(".lock") for n in os.listdir(self.auth)))

    def test_plain_account_names_allow_dots_underscores_and_hyphens(self):
        for tool in ("claude", "codex"):
            out = sh('"$1" login "$2" personal_2-old.work', self.home, AUTH_SH, tool)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertTrue(os.path.isdir(os.path.join(self.auth, tool + "-personal_2-old.work")))

    def test_an_alias_of_the_current_role_is_refused_before_switching(self):
        self.link("claude-default", "claude-ardupilot")
        self.link("claude-alias", "claude-default")
        out = self.use("claude", "default", "alias")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("is the role itself", out.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.auth, ".claude-default.lock")))
        self.assertEqual(os.readlink(os.path.join(self.auth, "claude-default")), "claude-ardupilot")

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

    def test_switching_to_the_account_already_in_place_is_still_checked(self):
        # "it already points there, nothing to do" skips the check that the
        # role resolves at all, so a broken role reports success
        d = os.path.join(self.auth, "claude-personal")
        os.chmod(d, 0o755)                # other-readable: the resolver refuses
        self.addCleanup(os.chmod, d, 0o700)
        self.link("claude-default", "claude-personal")
        out = self.use("claude", "default", "personal")
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn("does not resolve", out.stdout)

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

    def test_an_unsafe_account_is_refused_before_the_cli_reads_it(self):
        path = self.stub_cli()
        self.link("claude-default", "claude-ardupilot")
        for kind in ("root", "outside", "readable"):
            with self.subTest(kind=kind):
                account = self.leaves_root() if kind == "outside" else "personal"
                mode_path = self.auth if kind == "root" else os.path.join(self.auth, "claude-personal")
                os.chmod(mode_path, 0o777 if kind == "root" else 0o755)
                try:
                    out = self.use("claude", "default", account, path=path)
                finally:
                    os.chmod(mode_path, 0o700)
                self.assertNotEqual(out.returncode, 0, out.stdout + out.stderr)
                self.assertFalse(os.path.exists(os.path.join(self.home, "cli-env")),
                                 "CLI read a rejected account directory")

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
