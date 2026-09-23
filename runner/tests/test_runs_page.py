#!/usr/bin/env python3
"""The dashboard has to describe the accounts the roles actually select.

Switching a role is the point of the auth layout, and the dashboard is where
someone looks while doing it - because a quota ran out. Every check here failed
before the role layout landed: the meters were wired to a variable that no
longer exists and to one hardcoded directory each.
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
PAGE = os.path.join(BIN, "make-runs-page.py")


def build_page(home, out, **env):
    # Inspect the parsed timestamps as well as the HTML: a badge alone would
    # miss a refusal whose open time window absorbs the next run's usage.
    code = ('import json, runpy, sys; sys.argv = [sys.argv[1], sys.argv[2]]; '
            's = runpy.run_path(sys.argv[0]); '
            'print(json.dumps({k: s[k] for k in ("runs", "nfail", "cur_quota")}, default=str))')
    r = subprocess.run(["python3", "-c", code, PAGE, out], capture_output=True,
                       text=True, env={"HOME": home, "PATH": "/usr/bin:/bin", **env})
    if r.returncode:
        raise AssertionError(r.stdout + r.stderr)
    with open(out) as f:
        return json.loads(r.stdout.splitlines()[-1]), f.read()


def usage_line(when, tokens):
    return json.dumps({"timestamp": when.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       "message": {"usage": {"input_tokens": tokens,
                                             "output_tokens": 0,
                                             "cache_creation_input_tokens": 0,
                                             "cache_read_input_tokens": 0}}})


class Dashboard(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.logs = os.path.join(self.home, "review", "logs")
        self.auth = os.path.join(self.home, "review.auth")
        os.makedirs(self.logs)
        os.makedirs(self.auth, mode=0o700)
        self.out = os.path.join(self.home, "runs.html")

    def transcript(self, account, tokens, name="s.jsonl", at=None):
        """A session transcript under an account directory, recent enough to count."""
        if account == "own":
            d = os.path.join(self.home, ".claude", "projects", "-p")
        else:
            d = os.path.join(self.auth, account, "projects", "-p")
        os.makedirs(d, exist_ok=True)
        now = datetime.datetime.now().astimezone()
        with open(os.path.join(d, name), "w") as f:
            f.write(usage_line(at or (now - datetime.timedelta(minutes=5)), tokens) + "\n")

    def log(self, mode, body):
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        p = os.path.join(self.logs, "reviewprs-%s-%s.log" % (mode, stamp))
        with open(p, "w") as f:
            f.write(body)
        return p

    def build(self, **env):
        self.state, page = build_page(self.home, self.out, **env)
        return page

    def codex_quota(self, account, before, after):
        directory = (os.path.join(self.home, ".codex") if account == "own" else
                     os.path.join(self.auth, account))
        sessions = os.path.join(directory, "sessions", "2026", "09", "18")
        os.makedirs(sessions, exist_ok=True)
        json.dump({"tokens": {"account_id": account}}, open(os.path.join(directory, "auth.json"), "w"))
        now = datetime.datetime.now().astimezone()
        with open(os.path.join(sessions, "rollout-a.jsonl"), "w") as f:
            for minutes, pct in ((40, before), (5, after)):
                f.write(json.dumps({"timestamp": (now - datetime.timedelta(minutes=minutes)).isoformat(),
                                    "payload": {"type": "token_count", "rate_limits": {
                                        "plan_type": "pro", "primary": {"window_minutes": 10080,
                                        "used_percent": pct, "resets_at": int(now.timestamp()) + 86400}}}}) + "\n")
        return directory

    def codex_run(self, mode, account=None, directory=None):
        now = datetime.datetime.now().astimezone()
        body = "reviewprs mode=%s host=t start=%s\n" % (
            mode, (now - datetime.timedelta(minutes=30)).isoformat())
        if account:
            body += "codex account:  %s  (role default, home %s)\n" % (account, directory)
        body += "reviewprs mode=%s rc=0 elapsed=29m finish=%s\n" % (
            mode, (now - datetime.timedelta(minutes=1)).isoformat())
        self.log(mode, body)

    def test_codex_card_follows_a_named_default_role_not_the_publisher(self):
        self.codex_quota("codex-a", 10, 13)
        other = self.codex_quota("codex-b", 70, 89)
        os.symlink("codex-a", os.path.join(self.auth, "codex-default"))
        page = self.build(CODEX_HOME=other)
        self.assertEqual(self.state["cur_quota"], 13)
        self.assertIn("default: codex-a", page)
        self.assertIn('class="v">13%', page)

    def test_glob_characters_in_an_account_name_cannot_select_another_account(self):
        a = self.codex_quota("codex-[ab]", 10, 13)
        self.codex_quota("codex-a", 70, 89)
        os.symlink(a, os.path.join(self.auth, "codex-default"))
        self.codex_run("followup", "codex-[ab]", a)
        page = self.build()
        self.assertEqual(self.state["cur_quota"], 13)
        self.assertEqual(self.state["runs"][0]["q_delta"], 3)
        self.assertIn("+3.0%", page)
        self.assertNotIn("+19.0%", page)

    def test_glob_characters_in_the_auth_root_do_not_hide_accounts(self):
        auth = self.auth + "[one]"
        os.rename(self.auth, auth)
        self.auth = auth
        a = self.codex_quota("codex-a", 10, 13)
        os.symlink(a, os.path.join(self.auth, "codex-default"))
        self.transcript("claude-a", 2000000)
        page = self.build(REVIEW_AUTH=auth)
        self.assertEqual(self.state["cur_quota"], 13)
        self.assertIn("2.0M", page)

    def test_glob_characters_in_a_claude_directory_do_not_hide_transcripts(self):
        self.transcript("claude-[ab]", 2000000)
        self.transcript("claude-a", 7000000)
        self.assertIn("9.0M", self.build())

    def test_glob_characters_in_home_do_not_hide_run_logs(self):
        old = self.home
        self.home += "[one]"
        os.rename(old, self.home)
        self.addCleanup(shutil.rmtree, self.home, True)
        self.logs, self.auth, self.out = [p.replace(old, self.home, 1)
                                        for p in (self.logs, self.auth, self.out)]
        self.codex_run("followup")
        self.build()
        self.assertEqual(len(self.state["runs"]), 1)
        self.assertEqual(self.state["runs"][0]["status"], "ok")

    def test_a_deleted_recorded_home_does_not_borrow_another_accounts_samples(self):
        a = self.codex_quota("codex-a", 10, 13)
        b = self.codex_quota("codex-b", 70, 89)
        self.codex_run("followup", "codex-a", a)
        shutil.rmtree(a)
        os.symlink(b, os.path.join(self.auth, "codex-default"))
        page = self.build()
        self.assertEqual(self.state["cur_quota"], 89)
        self.assertEqual(len(self.state["runs"]), 1)
        self.assertIsNone(self.state["runs"][0]["q_delta"])
        self.assertIsNone(self.state["runs"][0]["q_end"])
        self.assertNotIn("+19.0%", page)

    def test_codex_deltas_follow_each_logged_account_even_after_a_switch(self):
        a = self.codex_quota("codex-a with space", 10, 13)
        b = self.codex_quota("codex-b", 70, 89)
        # Log IDs have no whitespace, but account directories can have it.
        json.dump({"tokens": {"account_id": "account-a"}}, open(os.path.join(a, "auth.json"), "w"))
        self.codex_run("followup", "account-a", a)
        self.codex_run("rsync", "codex-b", b)
        link = os.path.join(self.auth, "codex-default")
        os.symlink(a, link)
        for target in (b, a):
            os.remove(link)
            os.symlink(target, link)
            page = self.build(CODEX_HOME=target)
            rows = {r["mode"]: r for r in self.state["runs"]}
            self.assertEqual(rows["followup"]["q_delta"], 3)
            self.assertEqual(rows["rsync"]["q_delta"], 19)
            self.assertIn("+3.0%", page)
            self.assertIn("+19.0%", page)

    def test_codex_usage_without_matching_account_attribution_stays_unknown(self):
        a = self.codex_quota("codex-a", 10, 13)
        os.symlink(a, os.path.join(self.auth, "codex-default"))
        self.codex_run("old-run")
        self.codex_run("replaced-account", "former-account", a)
        self.codex_run("missing-home", "codex-a", a + "-missing")
        self.build()
        self.assertEqual(self.state["cur_quota"], 13)
        self.assertEqual(len(self.state["runs"]), 3)
        for r in self.state["runs"]:
            self.assertIsNone(r["q_delta"], r)
            self.assertIsNone(r["q_end"], r)

    def test_a_dangling_codex_role_does_not_show_the_fallbacks_meter(self):
        own = self.codex_quota("own", 20, 24)
        os.symlink("missing", os.path.join(self.auth, "codex-default"))
        page = self.build(CODEX_HOME=own)
        self.assertIsNone(self.state["cur_quota"])
        self.assertIn("default: unavailable", page)
        self.assertNotIn('class="v">24%', page)

    def test_an_absent_codex_default_role_still_uses_the_tools_own_home(self):
        self.codex_quota("own", 20, 24)
        page = self.build()
        self.assertEqual(self.state["cur_quota"], 24)
        self.assertIn("default: .codex", page)

    # --- the Quotas section --------------------------------------------------
    def quota_record(self, tool, name, free=50.0, windows=None, age_min=20,
                     account="a@example.org", error=None):
        at = (datetime.datetime.now().astimezone()
              - datetime.timedelta(minutes=age_min)).isoformat()
        rec = {"at": at, "tool": tool, "dir": os.path.join(self.auth, name),
               "account": account, "free_pct": free,
               "windows": windows if windows is not None else
               [{"kind": "weekly_all", "used_pct": 100 - free, "scoped": False,
                 "resets_at": (datetime.datetime.now().astimezone()
                               + datetime.timedelta(hours=30)).isoformat()}]}
        if error:
            rec["error"] = error
            rec["free_pct"] = None
        with open(os.path.join(self.home, "review", "logs", "quota.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
        return rec

    def quota_section(self, page):
        self.assertIn("<h2>Quotas</h2>", page)
        return page.split("<h2>Quotas</h2>", 1)[1].split("</table>", 1)[0]

    def test_the_page_is_published_so_identities_are_masked(self):
        # not credentials, but stable identifiers, and this page is public
        self.quota_record("claude", "claude-personal", account="someone@example.org")
        self.quota_record("codex", "codex-work",
                          account="efb1e83d-95c5-4919-8bde-fe1c094e1ebf")
        page = self.build()
        self.assertNotIn("someone@example.org", page)
        self.assertNotIn("efb1e83d-95c5-4919-8bde-fe1c094e1ebf", page)

    def test_a_masked_identity_still_says_which_account_it_is(self):
        # useless if you cannot tell the right account from the wrong one
        self.quota_record("codex", "codex-work",
                          account="efb1e83d-95c5-4919-8bde-fe1c094e1ebf")
        sec = self.quota_section(self.build())
        self.assertIn("efb1e83d", sec)          # enough to recognise

    def test_a_masked_address_keeps_its_domain(self):
        self.quota_record("claude", "claude-personal", account="someone@example.org")
        sec = self.quota_section(self.build())
        self.assertIn("@example.org", sec)
        self.assertNotIn("someone@", sec)

    def test_it_shows_a_row_for_every_account_it_has_a_reading_for(self):
        self.quota_record("claude", "claude-personal", account="person@example.org")
        self.quota_record("codex", "codex-work", account="efb1e83d")
        sec = self.quota_section(self.build())
        self.assertIn("claude-personal", sec)
        self.assertIn("codex-work", sec)
        self.assertIn("@example.org", sec)      # masked, but still identifiable

    def test_the_newest_reading_wins(self):
        self.quota_record("codex", "codex-work", free=90.0, age_min=300)
        self.quota_record("codex", "codex-work", free=7.0, age_min=5)
        sec = self.quota_section(self.build())
        self.assertIn("7%", sec)
        self.assertNotIn("90%", sec)
        self.assertEqual(sec.count("codex-work"), 1)

    def test_a_reading_that_failed_shows_why_rather_than_a_figure(self):
        self.quota_record("codex", "codex-work", error="no answer from the app-server")
        sec = self.quota_section(self.build())
        self.assertIn("no answer from the app-server", sec)
        self.assertNotIn("100%", sec)

    def test_rollover_is_shown_in_the_boxs_own_time_not_the_clis(self):
        """The CLIs report resets in UTC; the rest of the page is local.

        TZ is pinned rather than inherited: under a UTC clock - which is what
        CI has - local and UTC agree and this would pass without converting
        anything.
        """
        tz = "Etc/GMT-10"                       # UTC+10, no daylight saving
        utc = datetime.timezone.utc
        when = datetime.datetime.now(utc) + datetime.timedelta(hours=30)
        self.quota_record("codex", "codex-a", free=50.0, windows=[
            {"kind": "primary", "used_pct": 50, "scoped": False,
             "resets_at": when.isoformat()}])
        sec = self.quota_section(self.build(TZ=tz))
        local = (when + datetime.timedelta(hours=10)).strftime("%a %d %b %H:%M")
        self.assertIn(local, sec)
        self.assertNotIn(when.strftime("%a %d %b %H:%M"), sec)

    def test_rollover_is_the_soonest_window_that_gates_work(self):
        soon = datetime.datetime.now().astimezone() + datetime.timedelta(hours=2)
        late = datetime.datetime.now().astimezone() + datetime.timedelta(days=5)
        self.quota_record("claude", "claude-a", free=40.0, windows=[
            {"kind": "weekly_all", "used_pct": 60, "scoped": False,
             "resets_at": late.isoformat()},
            {"kind": "session", "used_pct": 10, "scoped": False,
             "resets_at": soon.isoformat()}])
        sec = self.quota_section(self.build())
        self.assertIn(soon.strftime("%H:%M"), sec)
        self.assertNotIn(late.strftime("%a %d %b %H:%M"), sec)

    def test_a_per_model_window_is_shown_but_does_not_set_the_rollover(self):
        soon = datetime.datetime.now().astimezone() + datetime.timedelta(hours=1)
        late = datetime.datetime.now().astimezone() + datetime.timedelta(days=4)
        self.quota_record("claude", "claude-a", free=55.0, windows=[
            {"kind": "weekly_all", "used_pct": 45, "scoped": False,
             "resets_at": late.isoformat()},
            {"kind": "weekly_scoped", "used_pct": 99, "scoped": True,
             "resets_at": soon.isoformat()}])
        sec = self.quota_section(self.build())
        self.assertIn("weekly_scoped", sec)                 # shown
        self.assertNotIn(soon.strftime("%a %d %b %H:%M"), sec)   # but not the rollover

    def test_an_account_at_the_threshold_is_marked(self):
        self.quota_record("codex", "codex-low", free=4.0)
        self.quota_record("codex", "codex-ok", free=60.0)
        sec = self.quota_section(self.build())
        low = [r for r in sec.split("<tr>") if "codex-low" in r][0]
        ok = [r for r in sec.split("<tr>") if "codex-ok" in r][0]
        self.assertIn('class="bad"', low)
        self.assertNotIn('class="bad"', ok)

    def test_with_no_readings_it_says_so_rather_than_showing_nothing(self):
        sec = self.quota_section(self.build())
        self.assertIn("no readings yet", sec)

    def test_building_the_page_never_starts_a_cli(self):
        """The page is rebuilt every ten minutes; asking an account for its
        quota starts the CLI, and that contends with a run for the OAuth
        refresh. The figures must come from the recorded file."""
        marker = os.path.join(self.home, "cli-was-run")
        for name in ("claude", "codex"):
            p = os.path.join(self.home, name)
            with open(p, "w") as f:
                f.write("#!/bin/sh\necho %s >> %s\n" % (name, marker))
            os.chmod(p, 0o755)
        self.quota_record("codex", "codex-work", free=42.0)
        self.build(PATH=self.home + ":/usr/bin:/bin")
        self.assertFalse(os.path.exists(marker),
                         "the page started a CLI to read a quota")

    # --- which accounts the meters look at ----------------------------------
    def test_it_counts_the_account_a_role_selects(self):
        # the whole point of the layout: `use claude default personal` moves the
        # workload, and the meter has to move with it
        self.transcript("claude-personal", 4242424)
        os.symlink("claude-personal", os.path.join(self.auth, "claude-default"))
        self.assertIn("4.2M", self.build())

    def test_it_counts_an_account_no_role_points_at_today(self):
        # yesterday's account is still what yesterday's runs spent
        self.transcript("claude-ardupilot", 7000000)
        self.assertIn("7.0M", self.build())

    def test_it_counts_the_tools_own_directory(self):
        self.transcript("own", 3000000)
        self.assertIn("3.0M", self.build())

    def test_it_does_not_count_an_account_twice_through_its_role_link(self):
        # a role is a symlink into the same set; following it would double every
        # figure for whichever account is selected
        self.transcript("claude-personal", 5000000)
        os.symlink("claude-personal", os.path.join(self.auth, "claude-default"))
        page = self.build()
        self.assertIn("5.0M", page)
        self.assertNotIn("10.0M", page)     # counted through the link as well

    def test_it_does_not_count_an_account_twice_when_the_tools_home_is_a_link(self):
        # ~/.claude is allowed to be a symlink into an account directory - the
        # runner goes out of its way to support it - so the same transcripts are
        # reachable under two names and every figure for them would double
        self.transcript("claude-personal", 6000000)
        os.symlink(os.path.join(self.auth, "claude-personal"),
                   os.path.join(self.home, ".claude"))
        page = self.build()
        self.assertIn("6.0M", page)
        self.assertNotIn("12.0M", page)

    def test_it_does_not_count_one_transcript_reached_two_ways(self):
        # two distinct account directories can still reach the same file - a
        # copied account, or a shared projects/ - and deduping directories does
        # not help there because their real paths differ
        self.transcript("claude-a", 9000000)
        b = os.path.join(self.auth, "claude-b")
        os.makedirs(b)
        os.symlink(os.path.join(self.auth, "claude-a", "projects"),
                   os.path.join(b, "projects"))
        page = self.build()
        self.assertIn("9.0M", page)
        self.assertNotIn("18.0M", page)

    # --- a run still behind the lock ----------------------------------------
    def run_row(self, page, mode):
        """The row for this mode in the Runs table.

        Not a substring search over the page: the mode names appear in the
        prose above the tables too, and matching those passes for the wrong
        reason.
        """
        table = page.split("<h2>Runs</h2>", 1)[1]
        rows = [r for r in table.split("<tr>") if "<td>%s</td>" % mode in r]
        self.assertTrue(rows, "no %s row in the Runs table" % mode)
        return rows[0]


    def queued_log(self, started=20, mode="rsync"):
        """A run that printed the lock wait and nothing since."""
        start = (datetime.datetime.now().astimezone()
                 - datetime.timedelta(minutes=started))
        return self.log(mode, "reviewprs mode=%s  host=t  start=%s\n"
                              "waiting up to 7200s for the run lock...\n"
                              % (mode, start.strftime("%Y-%m-%dT%H:%M:%S%z")))

    def test_a_run_behind_the_lock_is_not_shown_as_running(self):
        self.queued_log()
        row = self.run_row(self.build(), "rsync")
        self.assertIn("queued", row)
        self.assertNotIn("so far", row)          # it has run nothing to time

    def test_a_queued_run_shows_its_wait_where_the_wait_belongs(self):
        self.queued_log(started=22)
        row = self.run_row(self.build(), "rsync")
        self.assertIn("22m", row)

    def test_a_queued_run_is_credited_with_no_tokens(self):
        """It is waiting behind a run that is spending them.

        Runs are serialised by the lock so their working windows do not
        overlap - but a queued run's window from its own start does overlap
        the run it is waiting for, and it was being given that run's usage.
        """
        self.queued_log(started=20)
        self.transcript("claude-personal", 15_700_000)     # the other run's
        self.build()
        row = [r for r in self.state["runs"] if r["mode"] == "rsync"][0]
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["ctok"], 0)

    def test_a_run_that_waited_counts_only_from_when_it_got_the_lock(self):
        now = datetime.datetime.now().astimezone()
        start = now - datetime.timedelta(minutes=60)
        got = now - datetime.timedelta(minutes=10)
        self.log("followup",
                 "reviewprs mode=followup  host=t  start=%s\n"
                 "waiting up to 7200s for the run lock...\n"
                 "lock acquired at %s\n"
                 "reviewprs mode=followup rc=0 elapsed=10m finish=%s\n"
                 % (start.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    got.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    now.strftime("%Y-%m-%dT%H:%M:%S%z")))
        # one transcript entry while it waited, one while it worked
        self.transcript("claude-personal", 9_000_000, name="waiting.jsonl",
                        at=now - datetime.timedelta(minutes=30))
        self.transcript("claude-personal", 1_000_000, name="working.jsonl",
                        at=now - datetime.timedelta(minutes=5))
        self.build()
        row = [r for r in self.state["runs"] if r["mode"] == "followup"][0]
        self.assertEqual(row["ctok"], 1_000_000)   # not 10,000,000

    # --- how a refused run is shown -----------------------------------------
    def run_log(self, tail=""):
        # relative to now: the page discards anything outside its window, so a
        # hardcoded date turns these into tests that start failing on their own
        start = (datetime.datetime.now().astimezone()
                 - datetime.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S%z")
        return ("reviewprs mode=followup  host=t  start=%s\n" % start) + tail

    def refusal(self, status):
        end = datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
        self.log("followup", self.run_log(
            "FATAL: refused\nfinish=%s status=%s\n" % (end, status)))
        return self.build()

    def test_a_refused_claude_run_is_shown_as_refused(self):
        self.assertIn("wrong-account", self.refusal("wrong-claude-account"))

    def test_a_refused_codex_run_is_shown_as_refused(self):
        # matched by shape, not by name: an unrecognised refusal displays as a
        # run still going, with an elapsed time that climbs for ever
        page = self.refusal("wrong-codex-account")
        self.assertIn("wrong-account", page)
        self.assertNotIn(">running<", page)

    def stalled_log(self, tail="", age_minutes=180, started=240):
        """A log with no finish line, last written age_minutes ago."""
        start = (datetime.datetime.now().astimezone()
                 - datetime.timedelta(minutes=started)).strftime("%Y-%m-%dT%H:%M:%S%z")
        p = self.log("followup",
                     "reviewprs mode=followup  host=t  start=%s\n%s" % (start, tail))
        when = (datetime.datetime.now() - datetime.timedelta(minutes=age_minutes)).timestamp()
        os.utime(p, (when, when))
        return p

    def test_a_run_that_stopped_writing_is_not_still_running(self):
        # it was killed mid-flight, so it never reached its own finish line
        self.stalled_log()
        page = self.build()
        self.assertIn("died", page)
        self.assertNotIn("running", page)

    def test_a_dead_run_does_not_take_credit_for_later_tokens(self):
        # the real damage: with no end the window is [start, now], so a run that
        # lived two minutes claimed every token spent for the next three days
        self.stalled_log(age_minutes=180, started=240)
        self.transcript("claude-personal", 8000000)   # spent well after it died
        self.build()
        row = [r for r in self.state["runs"] if r["mode"] == "followup"][0]
        self.assertEqual(row["status"], "died")
        self.assertLess(row["elapsed"], 120)
        # ctok is the transcript figure, attributed over [start, finish or now]
        self.assertEqual(row["ctok"], 0)

    def test_a_run_still_writing_is_left_alone(self):
        self.stalled_log(age_minutes=0, started=30)
        self.assertIn("running", self.build())

    def test_a_run_queued_on_the_lock_is_not_called_dead(self):
        # waiting for the lock is silent: a queued run looks exactly like a dead
        # one until the timeout it stated has passed
        self.stalled_log("waiting up to 7200s for the run lock...\n",
                         age_minutes=90, started=95)
        page = self.build()
        self.assertIn("queued", self.run_row(page, "followup"))
        self.assertNotIn("died", self.run_row(page, "followup"))

    def test_a_run_past_its_own_lock_timeout_is_called_dead(self):
        self.stalled_log("waiting up to 600s for the run lock...\n",
                         age_minutes=180, started=185)
        self.assertIn("died", self.build())

    def test_a_run_with_no_finish_line_is_still_shown_as_running(self):
        self.log("followup", self.run_log())
        self.assertIn("running", self.build())


if __name__ == "__main__":
    unittest.main(verbosity=2)
