#!/usr/bin/env python3
"""The alert that an account a role uses can no longer run.

These run check-auth.py against throwaway homes, with the real review-auth.sh
and stub claude and sendmail, so a row the runner would refuse has to reach
the mail - not just a status line the check happens to grep for.
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
CHECK = os.path.join(BIN, "check-auth.py")
CODEX_ID = "11111111-1111-4111-8111-111111111111"


class CheckAuth(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.auth = os.path.join(self.home, "review.auth")
        self.logs = os.path.join(self.home, "review", "logs")
        os.makedirs(self.logs)
        for d in ("claude-ardupilot", "claude-personal", "codex-personal"):
            os.makedirs(os.path.join(self.auth, d), mode=0o700)
        for role, target in (("claude-default", "claude-ardupilot"),
                             ("claude-rsync", "claude-personal"),
                             ("codex-default", "codex-personal"),
                             ("codex-rsync", "codex-personal")):
            os.symlink(target, os.path.join(self.auth, role))
        json.dump({"tokens": {"access_token": "a", "refresh_token": "r",
                              "id_token": "e30.e30.c3R1Yg", "account_id": CODEX_ID}},
                  open(os.path.join(self.auth, "codex-personal", "auth.json"), "w"))
        self.stubs = os.path.join(self.home, "stubs")
        os.makedirs(self.stubs)
        # signed in everywhere except a directory named in $HOME/signed-out
        self.stub("claude",
                  'out=$(cat "$HOME/signed-out" 2>/dev/null)\n'
                  'if [ -n "$out" ] && [ "$CLAUDE_CONFIG_DIR" = "$out" ]; then\n'
                  '  echo \'{"loggedIn": false, "authMethod": "none"}\'; exit 0; fi\n'
                  'printf \'{"loggedIn": true, "email": "a@b.org", "authMethod": "claude.ai",'
                  ' "apiProvider": "firstParty", "configDirectory": "%s"}\\n\' "$CLAUDE_CONFIG_DIR"')
        self.mailbox = os.path.join(self.home, "mail")
        self.stub("sendmail", 'echo "args: $*" >> "$HOME/mail"; cat >> "$HOME/mail"')

    def stub(self, name, body):
        p = os.path.join(self.stubs, name)
        with open(p, "w") as f:
            f.write("#!/bin/bash\n" + body + "\n")
        os.chmod(p, 0o755)

    def run_check(self, *args, mail="ops@example.org", **extra):
        env = {"HOME": self.home, "PATH": self.stubs + ":/usr/bin:/bin",
               "LANG": "C.UTF-8", "REVIEW_SENDMAIL": os.path.join(self.stubs, "sendmail"),
               "REVIEW_BOX_NAME": "box"}
        if mail:
            env["REVIEW_ALERT_MAIL"] = mail
        env.update(extra)
        return subprocess.run([CHECK, *args], capture_output=True, text=True, env=env)

    def mailed(self):
        try:
            return open(self.mailbox).read()
        except FileNotFoundError:
            return ""

    def probe(self, directory, error=None, ago=datetime.timedelta(minutes=30), **extra):
        at = datetime.datetime.now().astimezone() - ago
        rec = {"at": at.isoformat(), "tool": "claude", "dir": directory,
               "windows": [], "free_pct": None if error else 50.0}
        if error:
            rec["error"] = error
        rec.update(extra)
        with open(os.path.join(self.logs, "quota.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")

    def test_all_signed_in_sends_nothing(self):
        out = self.run_check()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertTrue(out.stdout.strip().endswith(" ok"), out.stdout)
        self.assertEqual(self.mailed(), "")

    def test_a_signed_out_account_is_mailed_with_how_to_sign_it_in(self):
        personal = os.path.realpath(os.path.join(self.auth, "claude-personal"))
        open(os.path.join(self.home, "signed-out"), "w").write(personal)
        out = self.run_check()
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        mail = self.mailed()
        self.assertIn("args: -t -oi", mail)
        self.assertIn("To: ops@example.org", mail)
        self.assertIn("Subject: APReview box: 1 account role(s) cannot run", mail)
        self.assertIn("claude-rsync", mail)
        self.assertIn("NOT SIGNED IN", mail)
        # the directory itself, not the role or account name: the suffix-only
        # argument to review-auth.sh login is easy to get wrong
        self.assertIn("CLAUDE_CONFIG_DIR=%s claude auth login" % personal, mail)
        self.assertNotIn("claude-default ", mail.split("review-auth.sh status:")[0])
        self.assertIn("mailed ops@example.org", out.stdout)

    def test_the_sender_is_the_header_and_the_envelope(self):
        open(os.path.join(self.home, "signed-out"), "w").write(
            os.path.realpath(os.path.join(self.auth, "claude-ardupilot")))
        out = self.run_check(REVIEW_ALERT_FROM="box@example.net")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        mail = self.mailed()
        self.assertIn("args: -t -oi -f box@example.net", mail)
        self.assertIn("From: APReview box <box@example.net>", mail)

    def test_without_an_address_it_says_so_and_sends_nothing(self):
        open(os.path.join(self.home, "signed-out"), "w").write(
            os.path.realpath(os.path.join(self.auth, "claude-ardupilot")))
        out = self.run_check(mail=None)
        self.assertEqual(out.returncode, 1)
        self.assertIn("REVIEW_ALERT_MAIL is not set", out.stdout)
        self.assertEqual(self.mailed(), "")

    def test_dry_run_prints_the_mail_instead(self):
        open(os.path.join(self.home, "signed-out"), "w").write(
            os.path.realpath(os.path.join(self.auth, "claude-ardupilot")))
        out = self.run_check("--dry-run")
        self.assertIn("claude-default", out.stdout)
        self.assertIn("Subject: APReview box:", out.stdout)
        self.assertEqual(self.mailed(), "")

    def test_a_failed_probe_of_an_account_in_use_is_mailed(self):
        # signed in locally, but the server no longer takes the login
        d = os.path.realpath(os.path.join(self.auth, "claude-ardupilot"))
        self.probe(d, error="no usage_report in the reply")
        self.run_check()
        mail = self.mailed()
        self.assertIn("claude-default", mail)
        self.assertIn("hourly probe at", mail)
        self.assertIn("no usage_report in the reply", mail)

    def test_only_the_newest_probe_counts(self):
        d = os.path.realpath(os.path.join(self.auth, "claude-ardupilot"))
        self.probe(d, error="no usage_report in the reply", ago=datetime.timedelta(minutes=90))
        self.probe(d)
        self.assertEqual(self.run_check().returncode, 0)
        self.assertEqual(self.mailed(), "")

    def test_a_busy_or_stale_probe_is_not_an_expiry(self):
        d = os.path.realpath(os.path.join(self.auth, "claude-ardupilot"))
        self.probe(d, error="TimeoutError: account credential lease busy", busy=True)
        p = os.path.realpath(os.path.join(self.auth, "claude-personal"))
        self.probe(p, error="no usage_report in the reply", ago=datetime.timedelta(hours=5))
        self.assertEqual(self.run_check().returncode, 0)
        self.assertEqual(self.mailed(), "")

    def test_an_account_no_role_uses_is_not_mailed(self):
        os.makedirs(os.path.join(self.auth, "claude-spare"))
        self.probe(os.path.realpath(os.path.join(self.auth, "claude-spare")),
                   error="no usage_report in the reply")
        self.assertEqual(self.run_check().returncode, 0)
        self.assertEqual(self.mailed(), "")


if __name__ == "__main__":
    unittest.main()
