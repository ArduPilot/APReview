#!/usr/bin/env python3
"""Migration guards, exercised with local stores and bounded subprocesses."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from review_fixtures import BIN, PR, candidate, stop, until, workspace
from review_control import abort, reap, signal_identity
from review_dashboard import render, summaries
from review_discovery import Discovery
from review_guardian import identity
from review_handoff import cutover, handoff, import_manifest, pending, refresh_mirror
from review_lock import try_lock
from review_routing import DEFAULT, candidates, load, route, validate
from review_store import Store, atomic, digest, read
import repos


class Integration(unittest.TestCase):
    def setUp(self):
        self.root = workspace(self)
        self.home = self.root / "home"
        self.home.mkdir()
        self.site = self.home / "review"
        (self.site / "etc").mkdir(parents=True)
        self.data = self.root / "data"
        self.store = Store(self.data)
        self.env = dict(HOME=str(self.home), PATH="/usr/bin:/bin", REVIEW_ROOT=str(self.site),
                        REVIEW_DATA=str(self.data), PYTHONPATH=str(BIN),
                        REVIEW_REPO_CONFIG=repos.config_path())
        self.routing = dict(DEFAULT, repositories=["rsyncproject/rsync"])

    def run_cli(self, name, *args, **kw):
        return subprocess.run([sys.executable, str(BIN / name), *map(str, args)],
                              capture_output=True, text=True, env=self.env, timeout=10, **kw)

    def test_default_and_every_rsync_pr_spelling(self):
        self.assertEqual(load(self.site), DEFAULT)
        for value in ("rsync", "RSYNC", "--rsync", "/rsync", "rsync#1", "RsyncProject/rsync#1",
                      "https://github.com/RsyncProject/rsync/pull/1/files?x=2"):
            self.assertEqual(route(value, DEFAULT, repos.load()), "old", value)
            self.assertEqual(route(value, self.routing, repos.load()), "new", value)
        self.assertEqual(route("followup", self.routing, repos.load()), "old")
        self.assertEqual(route("AIReview", dict(DEFAULT, labels=["AIReview"]), repos.load()), "new")

    def test_author_routing_preserves_label_precedence(self):
        routing = dict(DEFAULT, modes=["author"], repositories=["owner/repo"])
        with patch("review_discovery.Discovery.resolve", return_value="CustomLabel"):
            self.assertEqual(route("CustomLabel", routing, repos.load()), "old")
        with patch("review_discovery.Discovery.resolve", return_value="@a-user"):
            self.assertEqual(route("a-user", routing, repos.load()), "new")
        from review_routing import owner
        self.assertEqual(owner(routing, "@a-user"), "new")

    def test_bad_config_never_defaults_to_old(self):
        for value in (dict(DEFAULT, schema=2), dict(DEFAULT, typo=[]), dict(DEFAULT, repositories=["bad"]), dict(DEFAULT, repositories="rsyncproject/rsync"), dict(DEFAULT, labels="AIReview"), dict(DEFAULT, modes=["typo"])):
            atomic(self.site / "etc/routing.json", value)
            out = self.run_cli("review-route.py", "rsync")
            self.assertNotEqual(out.returncode, 0, value)
            self.assertNotEqual(out.stdout.strip(), "old")

    def test_old_helper_filters_all_sources_including_followup(self):
        atomic(self.site / "etc/routing.json", self.routing)
        rows = [dict(repository="RsyncProject/rsync", number=1), dict(repository="ArduPilot/ardupilot", number=2)]
        for mode in ("all", "followup", "@author", "AIReview", "pr"):
            out = self.run_cli("review-route.py", mode, "--filter", "old", input=json.dumps(rows))
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout), rows[1:])
        command = (BIN.parents[1] / "commands/reviewprs.md").read_text()
        self.assertIn('--filter old < candidates.json > admitted.json', command)
        self.assertIn('including followup', command)

    def discovery(self, routing):
        d = Discovery(Mock(), dict(repos=repos.load(), routing=routing))
        d.resolve = lambda x: x
        d.swept = lambda: {}
        d.manifests = lambda mode: {}
        d.search = lambda *a: {"pr:rsyncproject/rsync#1", "pr:ardupilot/ardupilot#2"}
        d.candidate = lambda pr, mode, manifests: dict(pr=pr, created_at="2020", classification="REVIEW")
        return d

    def test_new_discovery_excludes_old_and_cross_owner_pages(self):
        self.assertEqual(self.discovery(DEFAULT).discover("AIReview"), [])
        self.assertEqual(self.discovery(dict(DEFAULT, labels=["AIReview"])).discover("AIReview"), [])
        self.assertEqual(self.discovery(self.routing).discover("AIReview"), [])
        out = self.discovery(self.routing).discover("rsync")
        self.assertEqual([c["pr"] for c in out], ["pr:rsyncproject/rsync#1"])

    def test_credential_probe_cannot_enter_a_guardian_account(self):
        home = self.root / "account"
        home.mkdir()
        native = home / ".oauth_refresh.lock"
        native.mkdir()
        with try_lock(self.store.locks, "account:claude/" + str(home)):
            result = self.run_cli("review-credential.py", home)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(native.exists())
        result = self.run_cli("review-credential.py", home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(native.exists())

    def test_account_shell_descriptors_must_reference_the_shared_inode(self):
        import shlex
        with try_lock(self.store.locks, "pause"):
            pass
        other = self.root / "wrong-lock"
        other.touch()
        out = subprocess.run(["bash", "-c", "exec 10<>" + shlex.quote(str(other)) +
                              " 11<>" + shlex.quote(str(other)) + "; exec python3 " +
                              shlex.quote(str(BIN / "review-admit.py")) + " accounts /one /two"],
                             env=self.env, capture_output=True, text=True, timeout=8)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("not the shared lock file", out.stderr)

    def test_quota_reader_obeys_the_account_lease(self):
        home = self.root / "account"
        home.mkdir()
        marker = self.root / "probe-launched"
        code = ("import json,sys; from pathlib import Path; import quota; "
                "quota.READERS['claude']=lambda d: (Path(sys.argv[2]).touch() or {}); "
                "print(json.dumps(quota.read('claude',sys.argv[1])))")
        with try_lock(self.store.locks, "account:claude/" + str(home)):
            out = subprocess.run([sys.executable, "-c", code, str(home), str(marker)],
                                 env=self.env, capture_output=True, text=True, timeout=8)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(marker.exists())
        self.assertIn("lease busy", json.loads(out.stdout).get("error", ""))

    def test_abort_is_durable_under_busy_run_lock_and_checks_identity(self):
        directory = self.data / "runs/r"
        atomic(directory / "run.json", dict(schema=1))
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        self.addCleanup(stop, child)
        bad = dict(identity(child.pid), start=-1)
        atomic(directory / "summary.json", bad)
        with try_lock(self.store.locks, "run:" + str(directory)):
            result = self.run_cli("review-control.py", "abort", directory, "--grace", "0.05")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(child.poll(), "stale pid identity killed unrelated process")
        self.assertTrue((directory / "abort.json").exists())
        request = (directory / "abort.json").read_bytes()
        abort(directory, grace=0)
        self.assertEqual((directory / "abort.json").read_bytes(), request)
        self.assertTrue(signal_identity(identity(child.pid), signal.SIGTERM))
        child.wait(timeout=3)

    def test_review_now_abort_never_waits_on_the_legacy_lock(self):
        import shutil
        scripts = self.site / "bin"
        scripts.mkdir()
        (scripts / "review-env.sh").write_text("")
        shutil.copy(BIN / "review-control.py", scripts)
        directory = self.data / "runs/r"
        atomic(directory / "run.json", dict(schema=1))
        with open(self.site / "etc/reviewprs.lock", "a") as old:
            fcntl.flock(old, fcntl.LOCK_EX)
            result = subprocess.run(["bash", str(BIN / "review-now.sh"), "--abort", "r"],
                                    env=self.env, capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((directory / "abort.json").exists())

    def test_review_now_resolves_mode_after_stripping_interactive(self):
        import shutil
        scripts = self.site / "bin"
        scripts.mkdir()
        (scripts / "review-env.sh").write_text("")
        shutil.copy(BIN / "review-route.py", scripts)
        wrapper = scripts / "run-reviewprs.sh"
        wrapper.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$HOME/now-args"\n')
        wrapper.chmod(0o755)
        atomic(self.site / "etc/routing.json", self.routing)
        result = subprocess.run(["bash", str(BIN / "review-now.sh"), "--interactive", "rsync", "--dry-run"],
                                env=self.env, capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / "now-args").read_text().splitlines(), ["rsync", "--interactive", "--dry-run"])

    def test_reaper_ignores_live_guardians_and_unregistered_processes(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], cwd=self.data)
        self.addCleanup(stop, child)
        directory = self.data / "runs/r/attempts/a"
        atomic(directory / "launch.json", dict(identity(child.pid), backend="plain"))
        atomic(directory / "status.json", dict(identity(child.pid), state="running", heartbeat=0))
        with patch("review_control.cleanup_attempt", return_value=True) as cleanup:
            self.assertEqual(reap(self.data), [])
            self.assertFalse(cleanup.called)
        self.assertIsNone(child.poll())
        atomic(directory / "status.json", dict(identity(child.pid), start=-1))
        with patch("review_control.cleanup_attempt", return_value=True) as cleanup:
            reap(self.data)
            self.assertTrue(cleanup.called)

    def test_signal_checks_identity_before_and_after_pidfd_open(self):
        with patch("review_control.alive", return_value=False), patch("os.pidfd_open") as opened:
            self.assertFalse(signal_identity({"pid": os.getpid()}, signal.SIGTERM))
            self.assertFalse(opened.called)
        with patch("review_control.alive", side_effect=[True, False]), \
                patch("os.pidfd_open", return_value=123), patch("os.close"), \
                patch("signal.pidfd_send_signal") as sent:
            self.assertFalse(signal_identity({"pid": os.getpid()}, signal.SIGTERM))
            self.assertFalse(sent.called)

    def test_resume_uses_frozen_config_and_refuses_transferred_ownership(self):
        directory = self.data / "runs/r"
        cfg = dict(schema=1, data=str(self.data), mode="rsync", candidates=[],
                   configuration={"repos": repos.load(), "providers": {"claude": {"home": "frozen"}}})
        atomic(directory / "run.json", cfg)
        out = self.run_cli("review-resume.py", directory, "--dry=1")
        self.assertNotEqual(out.returncode, 0)
        atomic(self.site / "etc/routing.json", self.routing)
        out = self.run_cli("review-resume.py", directory, "--dry=1")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("would resume", out.stdout)
        self.assertEqual(read(directory / "run.json"), cfg)
        cfg["candidates"] = [candidate(repository_override="unused")]
        atomic(directory / "run.json", cfg)
        self.assertNotEqual(self.run_cli("review-resume.py", directory, "--dry=1").returncode, 0)

    def test_pause_fences_new_admission_while_old_run_finishes(self):
        with open(self.site / "etc/reviewprs.lock", "a") as old:
            fcntl.flock(old, fcntl.LOCK_EX)
            result = self.run_cli("review-pause.py", "1")
            self.addCleanup(self.run_cli, "review-pause.py", "resume")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNone(try_lock(self.store.locks, "pause", shared=True))

    def test_dashboard_publisher_holds_page_region_through_rsync(self):
        import shutil
        scripts = self.site / "bin"
        scripts.mkdir()
        shutil.copy(BIN / "review-lock.py", scripts)
        (scripts / "review-env.sh").write_text("")
        # Test the real shell wrapper. Both children probe the actual kernel
        # region, not a count of inherited descriptors.
        probe = ("from review_lock import try_lock\nfrom pathlib import Path\nimport os\n"
                 "lock=try_lock(Path(os.environ['REVIEW_DATA'])/'locks', 'page:review/DevCallReviews/runs.html')\n"
                 "assert lock is None, 'publication page is not locked'\n")
        (scripts / "make-runs-page.py").write_text(probe + "Path(os.environ['HOME']+'/render-held').touch()\n")
        stub = self.root / "rsync"
        stub.write_text("#!/usr/bin/python3\n" + probe + "Path(os.environ['HOME']+'/rsync-held').touch()\n")
        stub.chmod(0o755)
        env = dict(self.env, PATH=str(self.root) + ":/usr/bin:/bin", REVIEW_PUBLISH="unused", RSYNC_AUTH="")
        result = subprocess.run(["bash", str(BIN / "publish-runs-page.sh")], env=env, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertTrue((self.home / "render-held").exists())
        self.assertTrue((self.home / "rsync-held").exists())

    def test_pause_holds_both_paths_and_resume_verifies_identity(self):
        result = self.run_cli("review-pause.py", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(self.run_cli, "review-pause.py", "resume")
        self.assertIsNone(try_lock(self.store.locks, "pause", shared=True))
        self.assertEqual(self.run_cli("review-admit.py", "pause").returncode, 75)
        with open(self.site / "etc/reviewprs.lock", "a") as old:
            def held():
                try:
                    fcntl.flock(old, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(old, fcntl.LOCK_UN)
                    return False
                except BlockingIOError:
                    return True
            until(self, held, seconds=3)

    def test_dashboard_identity_session_deduplication_and_debt(self):
        directory = self.data / "runs/r"
        atomic(directory / "summary.json", dict(schema=1, **identity(), state="running", heartbeat=0,
                                               prs={PR: {"review": "deferred"}}, delivery_deferred=["d"]))
        for name in ("a", "duplicate"):
            path = directory / "attempts" / name
            atomic(path / "status.json", dict(schema=1, **identity(), attempt=name, provider="codex",
                                               account="one", heartbeat=0, state="running"))
            (path / "payload.log").write_text(json.dumps({"type": "thread.started", "thread_id": "session-1"}) + "\n" +
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 123}}) + "\n")
        rows = summaries(self.data)
        self.assertEqual(rows[0]["liveness"], "live")
        self.assertEqual(rows[0]["counts"], {"deferred": 1})
        self.assertEqual(rows[0]["usage"], {"input_tokens": 123})
        page = render(rows)
        self.assertIn("codex", page)
        self.assertIn("session-1", page)
        self.assertIn("<pre>1</pre>", page)
        atomic(directory / "summary.json", dict(read(directory / "summary.json"), start=-1))
        self.assertEqual(summaries(self.data)[0]["liveness"], "dead")
        unknown = read(directory / "summary.json")
        del unknown["pid"]
        atomic(directory / "summary.json", unknown)
        self.assertEqual(summaries(self.data)[0]["liveness"], "unknown")

    def mirror(self):
        mirror = self.root / "pages"
        source = mirror / "RsyncReviews/index.html"
        source.parent.mkdir(parents=True)
        source.write_text('<!-- reviewprs-manifest v1 heads="1:aaaaaaaaaa" -->\n<section id="pr1">Original review</section>')
        shared = mirror / "UserReviews/person.html"
        shared.parent.mkdir()
        shared.write_text('<!-- reviewprs-manifest v1 heads="rsync#1:aaaaaaaaaa 2:bbbbbbbbbb" -->'
                          '<section id="prrsync-1">rsync</section><section id="pr2">keep</section>')
        return mirror

    def shared_mirror(self):
        mirror = self.root / "pages"
        page = mirror / "DevCallReviews/AIReview/devcall_pr_reviews.html"
        page.parent.mkdir(parents=True)
        page.write_text('<!-- reviewprs-manifest v1 label="AIReview" heads="1:aaaaaaaaaa MAVProxy-2:bbbbbbbbbb" -->\n'
                        '<h2>Reviews</h2>\n<div class="pr new" id="pr1">\n<h3>one</h3>\n</div>\n'
                        '<div class="pr" id="prMAVProxy-2">\n<h3>two</h3>\n</div>\n<h2>Summary</h2>\n')
        other = mirror / "DevCallReviews/DevCallEU/devcall_pr_reviews.html"
        other.parent.mkdir(parents=True)
        # the same PR at an older head on a second page
        other.write_text('<!-- reviewprs-manifest v1 label="DevCallEU" heads="1:cccccccccc" -->\n'
                         '<h2>Reviews</h2>\n<div class="pr" id="pr1">\n<h3>older one</h3>\n</div>\n<h2>Summary</h2>\n')
        person = mirror / "UserReviews/person.html"
        person.parent.mkdir()
        person.write_text('<!-- reviewprs-manifest v1 heads="wiki-3:dddddddddd" -->\n'
                          '<div class="pr" id="prwiki-3">\n<h3>wiki</h3>\n</div>\n<h2>Summary</h2>\n')
        (mirror / "UserReviews/files.html").write_text("<p>index</p>")
        return mirror

    def test_full_cutover_imports_every_shared_page_and_transfers_all_modes(self):
        mirror = self.shared_mirror()
        atomic(self.site / "etc/routing.json", self.routing)
        plan = cutover(self.site, self.data, mirror, dry=True)
        self.assertEqual(plan["problems"], [])
        self.assertEqual(plan["imported_prs"], 4)
        self.assertEqual(plan["pages"], ["page:review/DevCallReviews/AIReview/devcall_pr_reviews.html",
                                         "page:review/DevCallReviews/DevCallEU/devcall_pr_reviews.html",
                                         "page:review/UserReviews/person.html"])
        self.assertEqual(load(self.site)["modes"], [])
        for _ in range(2):
            result = cutover(self.site, self.data, mirror, wait=1)
        self.assertEqual(load(self.site), dict(self.routing, modes=["all"]))
        self.assertEqual(result["conflicts"], [dict(pr="pr:ardupilot/ardupilot#1",
                                                    page="page:review/DevCallReviews/DevCallEU/devcall_pr_reviews.html",
                                                    kept="aaaaaaaaaa", skipped="cccccccccc")])
        one = self.store.bundle("pr:ardupilot/ardupilot#1")
        self.assertTrue(one["legacy"])
        self.assertIn("<h3>one</h3>", one["results"]["reconciliation"]["section_md"])
        two = self.store.bundle("pr:ardupilot/mavproxy#2")
        self.assertEqual(two["inputs"]["manifest_key"], "MAVProxy-2")
        self.assertIsNotNone(self.store.bundle("pr:ardupilot/ardupilot_wiki#3"))
        for target, prs in {"page:review/DevCallReviews/AIReview/devcall_pr_reviews.html":
                                {"pr:ardupilot/ardupilot#1", "pr:ardupilot/mavproxy#2"},
                            "page:review/DevCallReviews/DevCallEU/devcall_pr_reviews.html":
                                {"pr:ardupilot/ardupilot#1"},
                            "page:review/UserReviews/person.html": {"pr:ardupilot/ardupilot_wiki#3"}}.items():
            rows = read(self.data / "membership" / (digest(target) + ".json"))
            self.assertEqual({pr for pr, row in rows.items() if not row["removed"]}, prs)
            self.assertTrue(all(row["generation"] == 0 for row in rows.values()))
        # the imported section renders without its old wrapper, under the new anchor
        from review_render import Renderer
        page = Renderer(self.store).render("page:review/DevCallReviews/AIReview/devcall_pr_reviews.html").decode()
        self.assertIn('<section id="prMAVProxy-2"', page)
        self.assertNotIn('<div class="pr new"', page)
        self.assertIn("<h3>one</h3>", page)
        self.assertFalse(pending(self.data))

    def test_mirror_refresh_copies_a_plain_tree_whole(self):
        publish = self.root / "publish"
        (publish / "DevCallReviews").mkdir(parents=True)
        (publish / "DevCallReviews/a.html").write_text("a")
        (publish / "UserReviews").mkdir()
        (publish / "UserReviews/b.html").write_text("b")
        mirror = self.root / "mirror"
        refresh_mirror(str(publish), [], mirror, time.monotonic() + 30)
        self.assertEqual((mirror / "DevCallReviews/a.html").read_text(), "a")
        self.assertEqual((mirror / "UserReviews/b.html").read_text(), "b")

    def test_full_cutover_refuses_a_key_it_cannot_place_or_a_missing_section(self):
        mirror = self.shared_mirror()
        page = mirror / "DevCallReviews/DevCallTopic/devcall_pr_reviews.html"
        page.parent.mkdir()
        page.write_text('<!-- reviewprs-manifest v1 heads="Nobody-7:eeeeeeeeee 9:ffffffffff" -->\n<h2>Reviews</h2>\n')
        plan = cutover(self.site, self.data, mirror, dry=True)
        self.assertEqual(plan["problems"], ["DevCallReviews/DevCallTopic/devcall_pr_reviews.html: unknown key Nobody-7",
                                            "DevCallReviews/DevCallTopic/devcall_pr_reviews.html: no section for pr:ardupilot/ardupilot#9"])
        with self.assertRaises(ValueError):
            cutover(self.site, self.data, mirror, wait=1)
        self.assertEqual(load(self.site), DEFAULT)
        self.assertIsNone(self.store.bundle("pr:ardupilot/ardupilot#1"))

    def test_handoff_dry_run_import_scrub_and_idempotent_rollback(self):
        mirror = self.mirror()
        before = (mirror / "UserReviews/person.html").read_text()
        result = handoff(self.site, self.data, "RsyncProject/rsync", "new", mirror, dry=True)
        self.assertEqual(result["imported_prs"], 1)
        self.assertEqual(load(self.site), DEFAULT)
        self.assertEqual((mirror / "UserReviews/person.html").read_text(), before)
        for _ in range(2):
            handoff(self.site, self.data, "RsyncProject/rsync", "new", mirror, wait=1)
        self.assertEqual(load(self.site)["repositories"], ["rsyncproject/rsync"])
        bundle = self.store.bundle("pr:rsyncproject/rsync#1")
        self.assertIsNotNone(bundle)
        self.assertTrue(bundle["legacy"])
        self.assertIn("Original review", bundle["results"]["reconciliation"]["section_md"])
        self.assertNotIn("rsync#1", (mirror / "UserReviews/person.html").read_text())
        self.assertIn('id="pr2"', (mirror / "UserReviews/person.html").read_text())
        member = read(self.data / "membership" / (digest("page:review/RsyncReviews/index.html") + ".json"))
        self.assertIn("pr:rsyncproject/rsync#1", member)
        handoff(self.site, self.data, "RsyncProject/rsync", "old", mirror, dry=True)
        self.assertEqual(load(self.site)["repositories"], ["rsyncproject/rsync"])
        for _ in range(2):
            handoff(self.site, self.data, "RsyncProject/rsync", "old", mirror, wait=1)
        self.assertEqual(load(self.site), DEFAULT)
        self.assertIn("Original review", (mirror / "RsyncReviews/index.html").read_text())
        self.assertEqual(self.store.bundle("pr:rsyncproject/rsync#1"), bundle)

    def test_handoff_refuses_undrained_debt_and_old_jobs(self):
        mirror = self.mirror()
        debt = self.data / "outbox/d.json"
        atomic(debt, dict(pr="pr:rsyncproject/rsync#1"))
        with self.assertRaises(TimeoutError):
            handoff(self.site, self.data, "RsyncProject/rsync", "new", mirror, wait=1)
        self.assertEqual(load(self.site), DEFAULT)

        debt.unlink()
        with open(self.site / "etc/reviewprs.lock", "a") as old:
            fcntl.flock(old, fcntl.LOCK_EX)
            with self.assertRaises(TimeoutError):
                handoff(self.site, self.data, "RsyncProject/rsync", "new", mirror, wait=1)
        self.assertEqual(load(self.site), DEFAULT)

    def test_handoff_missing_section_or_live_attempt_cannot_transfer(self):
        mirror = self.mirror()
        path = mirror / "RsyncReviews/index.html"
        source = path.read_text()
        path.write_text('<!-- reviewprs-manifest v1 heads="1:aaaaaaaaaa" -->')
        with self.assertRaises(ValueError):
            handoff(self.site, self.data, "RsyncProject/rsync", "new", mirror, dry=True)
        path.write_text(source)
        attempt = self.data / "runs/r/attempts/a"
        atomic(attempt / "job.json", dict(pr="pr:rsyncproject/rsync#1"))
        atomic(attempt / "status.json", dict(identity(), empty=False))
        with self.assertRaises(TimeoutError):
            handoff(self.site, self.data, "RsyncProject/rsync", "new", mirror, wait=.1)
        self.assertEqual(load(self.site), DEFAULT)

    def test_handoff_retries_failed_publication_before_route_commit(self):
        mirror = self.mirror()
        import review_handoff
        original = subprocess.run
        uploads = []
        def transport(argv, **kwargs):
            if argv[0] != "rsync":
                return original(argv, **kwargs)
            if "-a" not in argv:
                uploads.append(argv[-1])
                if len(uploads) == 1:
                    raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 0)
        with patch.object(review_handoff.subprocess, "run", side_effect=transport):
            with self.assertRaises(subprocess.CalledProcessError):
                handoff(self.site, self.data, "RsyncProject/rsync", "new", mirror, publish="remote:pages", wait=2)
            self.assertEqual(load(self.site), DEFAULT)
            handoff(self.site, self.data, "RsyncProject/rsync", "new", mirror, publish="remote:pages", wait=2)
        self.assertEqual(uploads, ["remote:pages/UserReviews/person.html"] * 2)
        self.assertEqual(load(self.site)["repositories"], ["rsyncproject/rsync"])

    def test_handoff_fences_a_queued_controller_without_attempts(self):
        mirror = self.mirror()
        directory = self.data / "runs/queued"
        atomic(directory / "run.json", dict(candidates=[dict(repository="rsyncproject/rsync")]))
        atomic(directory / "controller.json", identity())
        with self.assertRaises(TimeoutError):
            handoff(self.site, self.data, "RsyncProject/rsync", "new", mirror, wait=1)
        self.assertEqual(load(self.site), DEFAULT)

    def test_handoff_fences_unmaterialized_page_journals(self):
        mirror = self.mirror()
        self.store.journal("old-run", "discovery", "pr:rsyncproject/rsync#1",
                           [dict(kind="publish", target="page:review/RsyncReviews/index.html", gate="page")])
        self.assertFalse(list((self.data / "outbox").glob("*.json")))
        with self.assertRaisesRegex(TimeoutError, "delivery debts"):
            handoff(self.site, self.data, "RsyncProject/rsync", "old", mirror, wait=.1)
        self.assertEqual(load(self.site), DEFAULT)

    def test_handoff_fences_unmaterialized_bundle_intents(self):
        mirror = self.mirror()
        import_manifest(self.store, "rsyncproject/rsync",
                        {"pr:rsyncproject/rsync#1": dict(head="aaaaaaaaaa", section='<section id="pr1">old</section>')},
                        "page:review/RsyncReviews/index.html", {})
        next((self.data / "receipts").glob("*.json")).unlink()
        self.assertFalse(list((self.data / "outbox").glob("*.json")))
        with self.assertRaisesRegex(TimeoutError, "delivery debts"):
            handoff(self.site, self.data, "RsyncProject/rsync", "old", mirror, wait=.1)
        self.assertEqual(load(self.site), DEFAULT)

    def test_handoff_waits_for_legacy_open_files_outside_its_cwd(self):
        mirror = self.mirror()
        with open(self.data / "old-job-output", "w") as stream:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"],
                                     cwd=self.root, pass_fds=(stream.fileno(),))
        self.addCleanup(stop, child)
        with self.assertRaises(TimeoutError):
            handoff(self.site, self.data, "RsyncProject/rsync", "new", mirror, wait=1)
        self.assertEqual(load(self.site), DEFAULT)


if __name__ == "__main__":
    unittest.main()
