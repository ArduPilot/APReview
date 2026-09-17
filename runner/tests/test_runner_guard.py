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
        return subprocess.run([os.path.join(BIN, "run-reviewprs.sh"), mode, "--dry-run"],
                              capture_output=True, text=True, env=e)

    # --- what can still decide the account from outside the directory --------
    def cli_env(self):
        """The CLAUDE_*/ANTHROPIC_* names the CLI was actually invoked with."""
        with open(os.path.join(self.home, "cli-env")) as f:
            return f.read().split()

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

    def test_a_cli_that_reports_none_of_those_is_still_accepted(self):
        # an older CLI omits them - genuinely absent, not the string "-"
        out = self.run_mode("followup", STUB_NO_META="1")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

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
        self.assertIn("acct-1234", out.stdout)

    def test_an_api_key_with_no_auth_mode_stops_the_run(self):
        # older files carry no auth_mode; the key still wins inside the CLI
        d = os.path.join(self.auth, "codex-personal")
        json.dump({"OPENAI_API_KEY": "sk-x", "tokens": {"account_id": "acct-1234"}},
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
                   "tokens": {"account_id": "acct-1234"}},
                  open(os.path.join(d, "auth.json"), "w"))
        open(os.path.join(d, "ACCOUNT"), "w").write("acct-1234\n")
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
        open(os.path.join(self.auth, "codex-personal", "ACCOUNT"), "w").write(
            "acct-1234\n")
        out = self.run_mode("followup")
        # acct-1234 is not a uuid, so the record itself is refused: use the id
        # shape codex actually reports
        json.dump({"tokens": {"account_id": "1e60e907-99df-4679-915f-30b3032ba24a"}},
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
        stub(os.path.join(self.stubs, "claude"),
             '[ "$1 $2" = "auth status" ] && printf \'{"loggedIn": true}\\n\'')
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
