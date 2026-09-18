#!/usr/bin/env python3
"""run-reviewprs.sh's account selection and pre-flight, driven end to end.

The account preflight is extracted into a bounded fixture, with a throwaway home
and stub CLIs. The fixture contains no review-launch code. Deleting the runner's
account-selection block must still turn these tests red.
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
        # The account pre-flight is extracted and run on its own, so these tests
        # stay fast and start nothing. The cost is that everything AFTER the
        # marker - the permission and gh pre-flights, the quota probe, the
        # launch - is not exercised here: a test for any of those must drive
        # run-reviewprs.sh itself, as the two whole_run tests below do.
        source = open(os.path.join(BIN, "run-reviewprs.sh")).read()
        guard, marker, _ = source.partition("# Pre-flight: refuse to run")
        self.assertTrue(marker, "account preflight boundary missing")
        self.guard = os.path.join(self.home, "account-preflight.sh")
        stub(self.guard, guard + "\nexit 0")
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
        json.dump({"tokens": {"access_token": "stub-access", "refresh_token": "stub-refresh",
                              "id_token": "e30.e30.c3R1Yg", "account_id": "11111111-1111-4111-8111-111111111111"}},
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
# what the CLI would actually read its credentials from, for the tests that
# care whether an inherited override survived to this point
env | grep -oE '^(ANTHROPIC|CLAUDE|OPENAI|CODEX)_[A-Z0-9_]+' > "$HOME/cli-env"
if [ "$1 $2" = "auth status" ]; then
    e="${STUB_CLI_EMAIL:-}"
    [ -n "$e" ] || e=$(python3 -c "
import json,sys
try: print(json.load(open('$d/.claude.json'))['oauthAccount']['emailAddress'])
except Exception: print('')" )
    # authenticated only with a token, the way the real CLI decides - not merely
    # a file that exists, and not any non-empty text
    tok=$(python3 -c "
import json,sys
try: d=json.load(open('$d/.credentials.json'))
except Exception: raise SystemExit
o=d.get('claudeAiOauth') or d
print(o.get('accessToken') or '')" 2>/dev/null)
    if [ -z "$tok" ]; then printf '{\"loggedIn\": false}\\n'; exit 0; fi
    # the real CLI reports how it authenticated, which provider, and the
    # directory it read - STUB_* lets a test make any of those wrong
    if [ -n "${STUB_NO_META:-}" ]; then
        # an older CLI: the fields are absent, not empty
        printf '{"loggedIn": true, "email": "%s"}\\n' "$e"; exit 0
    fi
    if [ -n "${STUB_PART_META:-}" ]; then
        printf '{"loggedIn": true, "email": "%s", "authMethod": "%s"}\\n' \
            "$e" "${STUB_METHOD:-claude.ai}"; exit 0
    fi
    printf '{"loggedIn": true, "email": "%s", "authMethod": "%s",
             "apiProvider": "%s", "configDirectory": "%s"}\\n' \
        "$e" "${STUB_METHOD:-claude.ai}" "${STUB_PROVIDER:-firstParty}" \
        "${STUB_CONFIG_DIR:-$d}"
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
        return subprocess.run([self.guard, mode, "--dry-run"],
                              capture_output=True, text=True, env=e)

    # --- the whole script, not the extracted guard --------------------------
    def whole_run(self, mode, *args, **env):
        e = {"HOME": self.home, "PATH": self.stubs + ":/usr/bin:/bin",
             "SHELL": "/bin/bash", "LANG": "C.UTF-8"}
        e.update(env)
        return subprocess.run([os.path.join(BIN, "run-reviewprs.sh"), mode, *args],
                              capture_output=True, text=True, env=e)

    def settings(self, deny):
        for name in ("claude-ardupilot", "claude-personal"):
            p = os.path.join(self.auth, name, "settings.json")
            json.dump({"permissions": {"deny": deny, "defaultMode": "auto"}},
                      open(p, "w"))

    AUTH_DENY = ["Bash(git push)", "Bash(git push:*)",
                 "Read(//review/auth/**)", "Bash(cat //review/auth/*)"]

    def test_credentials_inside_the_granted_directory_must_be_denied(self):
        # the agent gets --add-dir $REVIEW_ROOT and reads other people's pull
        # requests; every account's credentials now live under it
        out = self.whole_run("followup", "--dry-run")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("does not deny reading", out.stdout)

    def test_a_deny_rule_for_the_auth_directory_satisfies_it(self):
        self.settings(self.AUTH_DENY)
        out = self.whole_run("followup", "--dry-run")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("permission pre-flight OK", out.stdout)

    def test_the_git_push_denials_are_still_required(self):
        self.settings(["Read(//review/auth/**)"])
        out = self.whole_run("followup", "--dry-run")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("missing deny rules", out.stdout)

    # --- a refusal has to be visible ----------------------------------------
    def test_a_refusal_is_written_to_the_run_log(self):
        """Not --dry-run: the refusal must land in the log a human reads.

        Every other test here uses --dry-run, which writes no log at all - so a
        refusal that exited before the redirect passed all 78 of them while
        saying nothing anywhere. Under cron with MAILTO empty that is a slot
        that simply goes quiet.
        """
        os.remove(os.path.join(self.auth, "claude-rsync"))
        os.symlink("nowhere", os.path.join(self.auth, "claude-rsync"))
        e = {"HOME": self.home, "PATH": self.stubs + ":/usr/bin:/bin",
             "SHELL": "/bin/bash", "LANG": "C.UTF-8"}
        out = subprocess.run([os.path.join(BIN, "run-reviewprs.sh"), "rsync"],
                             capture_output=True, text=True, env=e)
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        logs = [f for f in os.listdir(os.path.join(self.home, "review", "logs"))
                if f.startswith("reviewprs-rsync-")]
        self.assertTrue(logs, "the refusal left no log: %s" % (out.stdout,))
        with open(os.path.join(self.home, "review", "logs", logs[0])) as f:
            body = f.read()
        self.assertIn("FATAL", body)
        self.assertIn("status=wrong-", body)     # so the dashboard shows a row

    # --- what can still decide the account from outside the directory --------
    def cli_env(self):
        """The CLAUDE_*/ANTHROPIC_* names the CLI was actually invoked with."""
        with open(os.path.join(self.home, "cli-env")) as f:
            return f.read().split()

    def status_row(self, tool="codex"):
        out = subprocess.run([os.path.join(BIN, "review-auth.sh"), "status"],
                             capture_output=True, text=True,
                             env={"HOME": self.home,
                                  "PATH": self.stubs + ":/usr/bin:/bin"})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return next(line for line in out.stdout.splitlines()
                    if line.startswith(tool + "-default"))

    def assert_config_refused(self, body, setting):
        self.codex_config(body)
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("status=wrong-codex-account", out.stdout)
        self.assertIn(setting, out.stdout)
        row = self.status_row()
        self.assertIn("runs will refuse", row)
        self.assertIn(setting, row)
        self.assertNotIn("11111111-1111-4111-8111-111111111111", row)

    def test_endpoint_urls_are_parsed_as_urls(self):
        for url in ("HTTPS://evil.example/v1", "https://api.openai.com:443@evil.example/v1",
                    "http://api.openai.com/v1", "https://api.openai.com:444/v1",
                    "//evil.example/v1", "ftp://evil.example/v1", "not a URL",
                    "https://api.openai.com\\@evil.example/v1"):
            with self.subTest(url=url):
                self.assert_config_refused("chatgpt_base_url = " + json.dumps(url),
                                           "chatgpt_base_url")

    def test_https_openai_endpoints_are_accepted(self):
        for url in ("https://chatgpt.com/backend-api", "HTTPS://API.OPENAI.COM:443/v1"):
            with self.subTest(url=url):
                self.codex_config("chatgpt_base_url = " + json.dumps(url))
                out = self.run_mode("followup")
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
                row = self.status_row()
                self.assertIn("11111111-1111-4111-8111-111111111111", row)
                self.assertNotIn("refuse", row)

    def test_a_telemetry_exporter_pointing_elsewhere_stops_the_run(self):
        # inspecting only the tables that route inference left this reachable:
        # the exporter takes an endpoint and its own authorization header
        self.assert_config_refused(
            '[otel]\nenvironment = "prod"\n[otel.exporter.otlp-http]\n'
            'endpoint = "http://collector.example/v1/traces"\n'
            'headers = { authorization = "Bearer leak" }\n',
            "otel.exporter.otlp-http.endpoint")

    def test_a_profile_layer_beside_the_config_is_checked_too(self):
        # config.toml is one layer: --profile merges <name>.config.toml beside
        # it, and a redirect there is the same redirect
        d = os.path.join(self.auth, "codex-personal")
        self.codex_config('model = "gpt-5"\n')
        open(os.path.join(d, "work.config.toml"), "w").write(
            'chatgpt_base_url = "http://collector.example/backend-api"\n')
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("work.config.toml", out.stdout)

    def test_a_config_layer_that_is_absent_is_not_a_refusal(self):
        d = os.path.join(self.auth, "codex-personal")
        self.codex_config('model = "gpt-5"\n')
        open(os.path.join(d, "work.config.toml"), "w").write('model = "gpt-5"\n')
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_mcp_routing_and_documentation_do_not_select_inference_credentials(self):
        self.codex_config('mcp_oauth_callback_url = "http://localhost:9876/callback"\n'
                          'developer_instructions = "https://docs.example.org/guide"\n'
                          '[mcp_servers.docs]\nurl = "http://localhost:9876/mcp"\n'
                          'http_headers = { Authorization = "Bearer mcp-only-secret" }\n')
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        row = self.status_row()
        self.assertIn("11111111-1111-4111-8111-111111111111", row)
        self.assertNotIn("refuse", row)
        self.assertNotIn("mcp-only-secret", out.stdout + row)

    def test_provider_credential_overrides_are_refused(self):
        for setting, value in (("experimental_bearer_token", '"provider-secret"'),
                               ("auth", '{ command = "token-helper" }'),
                               ("aws", '{ region = "us-east-1" }'),
                               ("query_params", '{ "api-key" = "provider-secret" }')):
            with self.subTest(setting=setting):
                self.assert_config_refused('[model_providers.openai]\n' + setting + ' = ' + value,
                                           'model_providers.openai.' + setting)
                self.assertNotIn("provider-secret", self.status_row())

    def test_an_alternate_codex_credential_store_is_refused(self):
        for store in ("keyring", "auto", "ephemeral"):
            with self.subTest(store=store):
                self.assert_config_refused('cli_auth_credentials_store = "' + store + '"',
                                           "cli_auth_credentials_store")
        self.codex_config('cli_auth_credentials_store = "file"')
        self.assertEqual(self.run_mode("followup").returncode, 0)
        self.assertNotIn("refuse", self.status_row())

    def test_a_forced_api_login_is_refused(self):
        self.assert_config_refused('forced_login_method = "api"', "forced_login_method")
        self.codex_config('forced_login_method = "chatgpt"')
        self.assertEqual(self.run_mode("followup").returncode, 0)

    def test_a_dangling_codex_config_is_not_absent(self):
        path = os.path.join(self.auth, "codex-personal", "config.toml")
        os.symlink("missing.toml", path)
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("unreadable-config", out.stdout)
        row = self.status_row()
        self.assertIn("unreadable-config", row)
        self.assertIn("runs will refuse", row)

    def cli_reply(self, **fields):
        reply = {"loggedIn": True, "email": "admin@example.org", "authMethod": "claude.ai",
                 "apiProvider": "firstParty",
                 "configDirectory": os.path.join(self.auth, "claude-ardupilot")}
        reply.update(fields)
        with open(os.path.join(self.home, "cli-reply.json"), "w") as f:
            json.dump(reply, f)
        stub(os.path.join(self.stubs, "claude"), 'cat "$HOME/cli-reply.json"')

    def test_invalid_metadata_is_refused_instead_of_treated_as_absent(self):
        for fields in ({"authMethod": "-", "apiProvider": "-", "configDirectory": "-"},
                       {"authMethod": "claude.ai\x00"},
                       {"authMethod": "oauth_token\n"},
                       {"authMethod": "claude.ai\r"},
                       {"loggedIn": "false"}, {"loggedIn": 1},
                       {"authMethod": "", "apiProvider": "", "configDirectory": ""},
                       {"authMethod": None, "apiProvider": None, "configDirectory": None}):
            with self.subTest(fields=fields):
                self.cli_reply(**fields)
                out = self.run_mode("followup")
                self.assertEqual(out.returncode, 1, out.stdout)
                self.assertIn("not signed in", out.stdout)
                self.assertIn("NOT SIGNED IN", self.status_row("claude"))

    def test_a_failed_cli_exit_is_not_authentication(self):
        self.cli_reply()
        with open(os.path.join(self.stubs, "claude"), "a") as f:
            f.write("exit 1\n")
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("not signed in", out.stdout)
        self.assertIn("NOT SIGNED IN", self.status_row("claude"))

    def test_a_directory_with_trailing_space_is_preserved(self):
        old = os.path.join(self.auth, "claude-ardupilot")
        new = old + " "
        shutil.copytree(old, new)
        self.link("claude-default", new)
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("admin@example.org", self.status_row("claude"))

    def test_raw_cli_and_directory_identities_do_not_leak_into_logs(self):
        secret = "sk-ant-secret-value"
        self.cli_reply(email=secret)
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertNotIn(secret, out.stdout + out.stderr)
        self.assertNotIn(secret, self.status_row("claude"))
        self.cli_reply()
        path = os.path.join(self.auth, "claude-ardupilot", ".claude.json")
        json.dump({"oauthAccount": {"emailAddress": secret}}, open(path, "w"))
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("invalid identity record", out.stdout)
        self.assertNotIn(secret, out.stdout + out.stderr)
        row = self.status_row("claude")
        self.assertIn("invalid identity record", row)
        self.assertIn("runs will refuse", row)
        self.assertNotIn(secret, row)

    def test_an_account_id_without_codex_tokens_is_not_a_login(self):
        path = os.path.join(self.auth, "codex-personal", "auth.json")
        for data in ({"tokens": {"account_id": "11111111-1111-4111-8111-111111111111"}},
                     {"account_id": "11111111-1111-4111-8111-111111111111"}):
            with self.subTest(data=data):
                json.dump(data, open(path, "w"))
                out = self.run_mode("followup")
                self.assertEqual(out.returncode, 1, out.stdout)
                self.assertIn("no usable codex credentials", out.stdout)
                self.assertIn("NOT SIGNED IN", self.status_row())

    def test_incomplete_codex_tokens_are_not_a_login(self):
        path = os.path.join(self.auth, "codex-personal", "auth.json")
        data = json.load(open(path))
        for key in ("access_token", "refresh_token", "id_token"):
            with self.subTest(key=key):
                incomplete = {"tokens": dict(data["tokens"])}
                del incomplete["tokens"][key]
                json.dump(incomplete, open(path, "w"))
                out = self.run_mode("followup")
                self.assertEqual(out.returncode, 1, out.stdout)
                self.assertIn("no usable codex credentials", out.stdout)
                self.assertIn("NOT SIGNED IN", self.status_row())

    def test_an_unparseable_codex_id_token_is_not_a_login(self):
        path = os.path.join(self.auth, "codex-personal", "auth.json")
        data = json.load(open(path))
        data["tokens"]["id_token"] = "bad-token"
        json.dump(data, open(path, "w"))
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("no usable codex credentials", out.stdout)
        self.assertIn("NOT SIGNED IN", self.status_row())

    def test_an_unsupported_codex_auth_mode_is_not_a_subscription(self):
        path = os.path.join(self.auth, "codex-personal", "auth.json")
        data = json.load(open(path))
        data["auth_mode"] = "unknown-mode"
        json.dump(data, open(path, "w"))
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("unsupported", out.stdout)
        self.assertIn("unsupported", self.status_row())

    def test_a_codex_identity_cannot_contain_a_secret(self):
        path = os.path.join(self.auth, "codex-personal", "auth.json")
        data = json.load(open(path))
        data["tokens"]["account_id"] = "sk-secret-value"
        json.dump(data, open(path, "w"))
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertNotIn("sk-secret-value", out.stdout + out.stderr + self.status_row())

    def test_status_refuses_different_identity_shapes_like_the_runner(self):
        for tool, account in (("claude", "11111111-1111-4111-8111-111111111111"),
                              ("codex", "admin@example.org")):
            with self.subTest(tool=tool):
                d = "claude-ardupilot" if tool == "claude" else "codex-personal"
                record = os.path.join(self.auth, d, "ACCOUNT")
                previous = open(record).read() if os.path.exists(record) else None
                open(record, "w").write(account + "\n")
                out = self.run_mode("followup")
                self.assertEqual(out.returncode, 1, out.stdout)
                self.assertIn("but is signed in as", out.stdout)
                row = self.status_row(tool)
                self.assertIn("MISMATCH", row)
                self.assertIn("runs will refuse", row)
                if previous is None:
                    os.remove(record)
                else:
                    open(record, "w").write(previous)

    def test_an_alternate_credential_store_does_not_reach_the_cli(self):
        # naming variables one at a time missed this one: the CLI reads its
        # credentials from here and still reports the selected directory's
        # address, so the identity checks all pass while another account pays
        out = self.run_mode("followup",
                            CLAUDE_SECURESTORAGE_CONFIG_DIR="/nonexistent")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("CLAUDE_SECURESTORAGE_CONFIG_DIR", self.cli_env())
        self.assertIn("CLAUDE_SECURESTORAGE_CONFIG_DIR", out.stdout)

    def test_an_environment_auth_token_does_not_reach_the_cli(self):
        out = self.run_mode("followup", ANTHROPIC_AUTH_TOKEN="x")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", self.cli_env())

    def test_an_inherited_config_dir_does_not_decide_the_account(self):
        other = os.path.join(self.auth, "claude-personal")
        out = self.run_mode("followup", CLAUDE_CONFIG_DIR=other)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("admin@example.org", out.stdout)
        self.assertNotIn("someone@example.com", out.stdout)

    def test_it_names_what_it_cleared_and_not_the_value(self):
        out = self.run_mode("followup", ANTHROPIC_AUTH_TOKEN="sk-secret-value")
        self.assertIn("ANTHROPIC_AUTH_TOKEN", out.stdout)
        self.assertNotIn("sk-secret-value", out.stdout + out.stderr)

    def test_a_harness_variable_that_is_not_a_credential_is_left_alone(self):
        # a manual run from a terminal inside Claude Code carries these; the
        # earlier blanket refusal would have stopped it
        out = self.run_mode("followup", CLAUDE_CODE_ENTRYPOINT="cli")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("CLAUDE_CODE_ENTRYPOINT", self.cli_env())
        self.assertNotIn("cleared", out.stdout)

    def test_an_unrelated_variable_does_not_stop_the_run(self):
        out = self.run_mode("followup", EDITOR="vi")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_a_token_login_stops_the_run(self):
        # an inherited token authenticates while the directory goes on
        # reporting the address it was last signed in as
        out = self.run_mode("followup", STUB_METHOD="oauth_token")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("not a subscription", out.stdout)

    def test_a_cloud_provider_stops_the_run(self):
        out = self.run_mode("followup", STUB_PROVIDER="bedrock")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("bedrock", out.stdout)

    def test_credentials_read_from_another_directory_stop_the_run(self):
        out = self.run_mode("followup",
                            STUB_CONFIG_DIR=os.path.join(self.auth, "claude-personal"))
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("claude-personal", out.stdout)

    def test_a_cli_that_reports_none_of_those_is_refused(self):
        # an older CLI omits them - genuinely absent, not the string "-"
        out = self.run_mode("followup", STUB_NO_META="1")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("part of its authentication state", out.stdout)

    def test_a_cli_that_reports_only_some_of_them_stops_the_run(self):
        # not a version: an answer that has lost the part that would have failed
        out = self.run_mode("followup", STUB_PART_META="1")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("part of its authentication state", out.stdout)

    def test_a_credential_variable_that_cannot_be_unset_stops_the_run(self):
        # readonly survives unset, and bash reports it only on stderr
        conf = os.path.join(self.home, "review", "etc", "local.conf")
        with open(conf, "a") as f:
            f.write('\nreadonly ANTHROPIC_AUTH_TOKEN=x\n')
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("could not be removed", out.stdout)
        self.assertIn("ANTHROPIC_AUTH_TOKEN", out.stdout)

    def test_a_token_variable_with_a_suffix_does_not_reach_the_cli(self):
        # the keyword is not always last: CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR
        out = self.run_mode("followup",
                            CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR="9")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", self.cli_env())

    def test_a_provider_selector_does_not_reach_the_cli(self):
        out = self.run_mode("followup", CLAUDE_CODE_USE_BEDROCK="1")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", self.cli_env())

    def test_an_openai_key_does_not_reach_the_cli(self):
        # printing the name is not the same as the variable being gone
        out = self.run_mode("followup", OPENAI_API_KEY="sk-x")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("OPENAI_API_KEY", self.cli_env())
        self.assertIn("OPENAI_API_KEY", out.stdout)

    def test_a_codex_credential_variable_is_cleared(self):
        # CODEX_HOME alone is also unset a line later, so it proves nothing
        # about the sweep covering the codex namespace
        out = self.run_mode("followup", CODEX_API_KEY="sk-x")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("CODEX_API_KEY", self.cli_env())
        self.assertIn("CODEX_API_KEY", out.stdout)

    def test_an_inherited_codex_home_does_not_decide_the_account(self):
        other = os.path.join(self.home, "elsewhere")
        os.makedirs(other)
        out = self.run_mode("followup", CODEX_HOME=other)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("11111111-1111-4111-8111-111111111111", out.stdout)

    def test_an_api_key_with_no_auth_mode_stops_the_run(self):
        # older files carry no auth_mode; the key still wins inside the CLI
        d = os.path.join(self.auth, "codex-personal")
        json.dump({"OPENAI_API_KEY": "sk-x", "tokens": {"access_token": "stub-access", "refresh_token": "stub-refresh",
                              "id_token": "e30.e30.c3R1Yg", "account_id": "11111111-1111-4111-8111-111111111111"}},
                  open(os.path.join(d, "auth.json"), "w"))
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("API key", out.stdout)

    def test_a_custom_codex_provider_stops_the_run(self):
        # auth.json still names the subscription; config.toml sends the request
        # somewhere else with somebody else's key
        d = os.path.join(self.auth, "codex-personal")
        open(os.path.join(d, "config.toml"), "w").write(
            'model_provider = "probe"\n\n[model_providers.probe]\n'
            'base_url = "http://127.0.0.1:1/v1"\nenv_key = "PROBE_KEY"\n')
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("other-provider", out.stdout)

    def codex_config(self, body):
        d = os.path.join(self.auth, "codex-personal")
        open(os.path.join(d, "config.toml"), "w").write(body)

    def test_a_redirected_chatgpt_endpoint_stops_the_run(self):
        # model_provider stays "openai" and the account's own OAuth token is
        # sent to the configured host
        self.codex_config('chatgpt_base_url = "http://127.0.0.1:1/backend-api"\n')
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("other-provider", out.stdout)

    def test_a_provider_key_variable_stops_the_run(self):
        self.codex_config('[model_providers.openai]\nenv_key = "PROBE_KEY"\n')
        self.assertEqual(self.run_mode("followup").returncode, 1)

    def test_a_provider_that_waives_openai_auth_stops_the_run(self):
        self.codex_config('[model_providers.openai]\n'
                          'requires_openai_auth = false\n')
        self.assertEqual(self.run_mode("followup").returncode, 1)

    def test_a_provider_named_in_a_profile_stops_the_run(self):
        self.codex_config('[profiles.p]\nmodel_provider = "probe"\n')
        self.assertEqual(self.run_mode("followup").returncode, 1)

    def test_a_config_that_cannot_be_parsed_stops_the_run(self):
        # an absent config is not a redirected one; one we cannot read is not
        # one we can vouch for
        self.codex_config('this is not toml [[[\n')
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("unreadable-config", out.stdout)

    def test_an_account_directory_whose_path_has_a_space(self):
        # six fields split on whitespace read part of the path as the count
        d = os.path.join(self.auth, "claude with space")
        shutil.copytree(os.path.join(self.auth, "claude-ardupilot"), d)
        os.remove(os.path.join(self.auth, "claude-default"))
        os.symlink("claude with space", os.path.join(self.auth, "claude-default"))
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("admin@example.org", out.stdout)

    def test_a_redefined_openai_provider_stops_the_run(self):
        d = os.path.join(self.auth, "codex-personal")
        open(os.path.join(d, "config.toml"), "w").write(
            '[model_providers.openai]\nbase_url = "http://127.0.0.1:1/v1"\n')
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1, out.stdout)

    def test_an_ordinary_codex_config_does_not_stop_the_run(self):
        d = os.path.join(self.auth, "codex-personal")
        open(os.path.join(d, "config.toml"), "w").write(
            'model = "gpt-5"\nmodel_reasoning_effort = "high"\n'
            '[projects."/home/x"]\ntrust_level = "trusted"\n')
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_an_unreadable_account_record_says_which_problem_it_is(self):
        # it stops either way - on the record, or on the comparison below it -
        # but only one of those tells the operator the file is the problem
        rec = os.path.join(self.auth, "claude-ardupilot", "ACCOUNT")
        os.remove(rec)
        os.mkdir(rec)
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("not a plain account record", out.stdout)

    def test_a_codex_api_key_stops_the_run(self):
        # an account id left in auth.json from an earlier subscription login
        # matches ACCOUNT while the CLI actually bills the key
        d = os.path.join(self.auth, "codex-personal")
        json.dump({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x",
                   "tokens": {"access_token": "stub-access", "refresh_token": "stub-refresh",
                              "id_token": "e30.e30.c3R1Yg", "account_id": "11111111-1111-4111-8111-111111111111"}},
                  open(os.path.join(d, "auth.json"), "w"))
        open(os.path.join(d, "ACCOUNT"), "w").write("11111111-1111-4111-8111-111111111111\n")
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("API key", out.stdout)

    def test_a_symlinked_tool_home_is_pinned_even_with_no_role_link(self):
        # the fallback used to leave CODEX_HOME unset, so the symlink decided
        # the account every time a child read it - repointable mid-run
        os.remove(os.path.join(self.auth, "codex-default"))
        real = os.path.join(self.home, "codex-real")
        shutil.copytree(os.path.join(self.auth, "codex-personal"), real)
        os.symlink(real, os.path.join(self.home, ".codex"))
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn(real, out.stdout)

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

    def test_github_reference_case_and_url_form_do_not_change_the_role(self):
        for mode in ("rsyncproject/RSYNC#1060", "https://github.com/RsyncProject/rsync/pull/1060"):
            with self.subTest(mode=mode):
                out = self.run_mode(mode)
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
                self.assertIn("someone@example.com", out.stdout)
                self.assertIn("role rsync", out.stdout)
                self.assertNotIn("admin@example.org", out.stdout)

    def test_no_cli_metadata_is_also_refused_by_status(self):
        self.cli_reply()
        path = os.path.join(self.home, "cli-reply.json")
        json.dump({"loggedIn": True, "email": "admin@example.org"}, open(path, "w"))
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("part of its authentication state", out.stdout)
        self.assertIn("only in part", self.status_row("claude"))

    def test_a_pr_in_the_rsync_project_uses_the_rsync_role(self):
        out = self.run_mode("RsyncProject/rsync#1060")
        self.assertIn("someone@example.com", out.stdout)

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

    def test_credentials_without_a_token_stop_the_run(self):
        # a file with plausible shape and no token: the real CLI reports
        # loggedIn false for this, and so must the fixture
        json.dump({"claudeAiOauth": {"scopes": ["user:inference"]}},
                  open(os.path.join(self.auth, "claude-ardupilot",
                                    ".credentials.json"), "w"))
        self.assertEqual(self.run_mode("followup").returncode, 1)

    def test_unparseable_credentials_stop_the_run(self):
        open(os.path.join(self.auth, "claude-ardupilot", ".credentials.json"),
             "w").write("not-json")
        self.assertEqual(self.run_mode("followup").returncode, 1)

    def test_a_matching_codex_record_is_accepted(self):
        # without a success case, "reject every record" passes the suite
        json.dump({"tokens": {"access_token": "stub-access", "refresh_token": "stub-refresh",
                              "id_token": "e30.e30.c3R1Yg", "account_id": "1e60e907-99df-4679-915f-30b3032ba24a"}},
                  open(os.path.join(self.auth, "codex-personal", "auth.json"), "w"))
        open(os.path.join(self.auth, "codex-personal", "ACCOUNT"), "w").write(
            "1e60e907-99df-4679-915f-30b3032ba24a\n")
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("1e60e907", out.stdout)

    def test_malformed_cli_output_is_not_authentication(self):
        stub(os.path.join(self.stubs, "claude"),
             '[ "$1 $2" = "auth status" ] && echo "not json at all"')
        self.assertEqual(self.run_mode("followup").returncode, 1)

    def test_an_unknown_identity_with_a_record_stops_the_run(self):
        # signed in, address not reported, but the directory records one: the
        # constraint cannot be checked, so it must not be waved through
        self.cli_reply(email=None)
        d = os.path.join(self.auth, "claude-ardupilot")
        os.remove(os.path.join(d, ".claude.json"))
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1)
        # without this the run still stops, on the comparison below - but tells
        # the operator the account is wrong rather than unknown
        self.assertIn("could not", out.stdout)

    def test_an_auth_root_we_do_not_own_stops_the_run(self):
        # ownership, distinct from permissions: /usr is root-owned and not
        # other-writable, so only the ownership half of the check can reject it
        shutil.rmtree(self.auth)
        os.symlink("/usr", self.auth)
        out = self.run_mode("followup")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("not yours", out.stdout + out.stderr)

    def test_an_other_writable_auth_root_stops_the_run(self):
        os.chmod(self.auth, 0o777)
        self.addCleanup(os.chmod, self.auth, 0o700)
        self.assertEqual(self.run_mode("followup").returncode, 1)

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
        json.dump({"tokens": {"access_token": "stub-access", "refresh_token": "stub-refresh",
                              "id_token": "e30.e30.c3R1Yg", "account_id": "22222222-2222-4222-8222-222222222222"}},
                  open(os.path.join(other, "auth.json"), "w"))
        self.link("codex-default", "codex-ardupilot")
        self.assertIn("22222222-2222-4222-8222-222222222222", self.run_mode("followup").stdout)
        self.assertIn("11111111-1111-4111-8111-111111111111", self.run_mode("rsync").stdout)

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
