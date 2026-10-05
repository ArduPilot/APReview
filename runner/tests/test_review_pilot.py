import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_fixtures import BIN, candidate, workspace  # noqa: E402
from review_store import Store, atomic, read  # noqa: E402

FINDING = {"id": "F1", "kind": "BUG", "severity": "high", "claim": "it overflows, see /x/attempts/ab/inputs/diff.patch",
           "location": {"non_line_specific": True}, "status": "VERIFIED",
           "evidence": {"commands": [], "artifacts": [], "configuration": "stub"}}


def pilot_dir(root, production):
    """A frozen pilot as freeze leaves it, without GitHub."""
    pilot = root / "pilot"
    config = dict(repos={"repos": []}, github_accounts={}, endpoints={"review": {"url": "", "publish": "rsync://x"}},
                  github_writes=True, comment_accounts={"x": 1}, project_id="P")
    import hashlib
    atomic(pilot / "configuration-base.json", config)
    stub = {"primary": {"findings": [FINDING]}, "reconciliation": {"retain": True}}
    candidates = [dict(candidate(1), pr="pr:owner/repo#1", title="T", diff="", thread=[], stub=stub, post=False),
                  dict(candidate(2), pr="pr:owner/repo#2", title="U", diff="", thread=[], post=False)]
    atomic(pilot / "candidates.json", candidates)
    def h(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    atomic(pilot / "frozen.json", dict(at=0, prs=[c["pr"] for c in candidates],
                                       heads={c["pr"]: c["head"] for c in candidates},
                                       configuration=h(config), candidates=h(read(pilot / "candidates.json"))))
    return pilot


class PilotBase(unittest.TestCase):
    def setUp(self):
        self.root = workspace(self)
        self.production = self.root / "production"
        Store(self.production)
        atomic(self.production / "quota.json", {"paused": False})
        self.pilot = pilot_dir(self.root, self.production)
        self.env = dict(os.environ, REVIEW_AI_STUB="1", REVIEW_GUARDIAN_PLAIN="1", PYTHONDONTWRITEBYTECODE="1",
                        REVIEW_DATA=str(self.production), REVIEW_ROOT=str(self.root))

    def tool(self, *args, ok=True):
        r = subprocess.run([sys.executable, str(BIN / "review-pilot.py"), *args],
                           env=self.env, capture_output=True, text=True, timeout=120)
        if ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def run_arms(self):
        for arm in ("legacy", "paging"):
            self.tool("run", "--dir", str(self.pilot), "--arm", arm)


class Pilot(PilotBase):

    def test_two_arms_review_the_frozen_inputs_apart_and_collect_blind(self):
        self.run_arms()
        for arm, prompts in (("legacy", "v1"), ("paging", "v4-paging")):
            data = self.pilot / arm / "data"
            config = read(self.pilot / arm / "configuration.json")
            self.assertEqual(config["presentation"]["prompts"], prompts)
            self.assertEqual((config["github_writes"], config["frozen_inputs"], config["comment_accounts"],
                              config["endpoints"]["review"]["publish"]), (False, True, {}, ""))
            self.assertNotIn("routing_root", config)
            for provider in config.get("providers", {}).values():
                self.assertNotIn(str(self.root), [g for g in provider["granted_directories"]
                                                  if g in (str(self.root), str(self.production))])
            # own locks; production's quota state, live
            production_locks = self.production / "locks"
            self.assertFalse(production_locks.exists() and os.path.samefile(data / "locks", production_locks))
            self.assertEqual((data / "quota.json").resolve(), (self.production / "quota.json").resolve())
            summary = read(data / "runs" / ("pilot-" + arm) / "summary.json")
            self.assertEqual({v["review"] for v in summary["prs"].values()}, {"accepted"})
        for area in ("results", "outbox", "receipts", "membership", "operations"):
            self.assertFalse([p for p in (self.production / area).rglob("*") if p.is_file()], area)
        out = self.tool("collect", "--dir", str(self.pilot)).stdout
        pack, key = read(self.pilot / "adjudication-pack.json"), read(self.pilot / "adjudication-key.json")
        self.assertIn("2 findings over 2 PRs", out)
        self.assertEqual({k["arm"] for k in key.values()}, {"legacy", "paging"})
        for finding in pack:
            self.assertNotIn("arm", finding)
            self.assertNotIn("/attempts/", finding["claim"])          # scrubbed of what could reveal the arm
            self.assertIn("[path]", finding["claim"])
            self.assertEqual(finding["final"], "retained")
        contexts = read(self.pilot / "contexts.json")
        self.assertEqual(set(contexts["arms"]), {"legacy", "paging"})
        # stub passes leave no usage logs: every attempt is counted as unmeasured, not lost
        self.assertTrue(all(n > 0 for n in contexts["unmeasured"].values()))

    def test_collect_refuses_an_incomplete_pair(self):
        self.tool("run", "--dir", str(self.pilot), "--arm", "legacy")
        r = self.tool("collect", "--dir", str(self.pilot), ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("arms missing", r.stderr)
        self.assertFalse((self.pilot / "adjudication-pack.json").exists())

    def test_a_resume_with_other_settings_and_changed_candidates_are_refused(self):
        self.tool("run", "--dir", str(self.pilot), "--arm", "legacy")
        run_json = self.pilot / "legacy" / "data" / "runs" / "pilot-legacy" / "run.json"
        original = read(run_json)
        self.tool("run", "--dir", str(self.pilot), "--arm", "legacy")             # the same settings resume
        for key, value in (("github_writes", True), ("routing_root", "/production/routing"), ("prompts", {"x": 1})):
            changed = json.loads(json.dumps(original))
            changed["configuration"][key] = value
            atomic(run_json, changed)
            r = self.tool("run", "--dir", str(self.pilot), "--arm", "legacy", ok=False)
            self.assertIn("other settings", r.stderr, key)
        atomic(run_json, original)
        candidates = read(self.pilot / "candidates.json")
        atomic(self.pilot / "candidates.json", candidates[:1])
        r = self.tool("run", "--dir", str(self.pilot), "--arm", "paging", ok=False)
        self.assertIn("candidates have changed", r.stderr)

    def test_collect_refuses_results_of_other_inputs(self):
        self.run_arms()
        candidates = read(self.pilot / "candidates.json")
        candidates[0]["title"] = "changed after the run"
        atomic(self.pilot / "candidates.json", candidates)
        r = self.tool("collect", "--dir", str(self.pilot), ok=False)
        self.assertIn("other inputs", r.stderr)

    def test_a_pilot_inside_the_production_store_is_refused(self):
        inside = self.production / "pilot"
        r = self.tool("run", "--dir", str(inside), "--arm", "legacy", ok=False)
        self.assertIn("inside the production store", r.stderr)
        r = self.tool("freeze", "--out", str(inside), "pr:owner/repo#1", ok=False)
        self.assertIn("inside the production store", r.stderr)
        sibling = Path(str(self.production) + "-pilot")                 # shares the reaper's prefix
        r = self.tool("run", "--dir", str(sibling), "--arm", "legacy", ok=False)
        self.assertIn("inside the production store", r.stderr)
        r = self.tool("overnight", "--dir", str(inside), "--hours", "1", ok=False)
        self.assertIn("inside the production store", r.stderr)

    def test_a_changed_frozen_configuration_is_refused(self):
        config = read(self.pilot / "configuration-base.json")
        atomic(self.pilot / "configuration-base.json", dict(config, extra=1))
        r = self.tool("run", "--dir", str(self.pilot), "--arm", "legacy", ok=False)
        self.assertIn("changed since freeze", r.stderr)


class Overnight(PilotBase):
    def setUp(self):
        super().setUp()
        # a pause tool that records what it is asked, its state in a file
        self.state = self.root / "pause-state"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "pause-runs.sh").write_text(
            "#!/bin/sh\n"
            "echo \"$@\" >> %s.log\n"
            "case \"$1\" in\n"
            "  status) [ -f %s ] && echo PAUSED || echo 'not paused' ;;\n"
            "  resume) rm -f %s ;;\n"
            "  *) touch %s ;;\n"
            "esac\n" % (self.state, self.state, self.state, self.state))
        (bin_dir / "pause-runs.sh").chmod(0o755)

    def calls(self):
        return (self.root / "pause-state.log").read_text().split("\n")

    def test_overnight_pauses_runs_both_arms_resumes_and_collects(self):
        r = self.tool("overnight", "--dir", str(self.pilot), "--hours", "1")
        self.assertIn("2 findings over 2 PRs", r.stdout)
        self.assertFalse(self.state.exists())                       # production resumed
        self.assertEqual([c.split()[0] for c in self.calls() if c], ["status", "90", "resume", "status"])

    def test_someone_elses_pause_is_not_taken_over(self):
        self.state.touch()
        r = self.tool("overnight", "--dir", str(self.pilot), "--hours", "1", ok=False)
        self.assertIn("already paused", r.stderr)
        self.assertTrue(self.state.exists())                        # theirs, left as it was
        self.assertFalse((self.pilot / "legacy").exists())

    def test_a_production_run_going_ends_the_pause_and_starts_nothing(self):
        sys.path.insert(0, str(BIN))
        from review_guardian import identity
        atomic(self.production / "runs" / "all-9" / "summary.json", dict(identity(), state="running"))
        r = self.tool("overnight", "--dir", str(self.pilot), "--hours", "1", ok=False)
        self.assertIn("production work is still running", r.stderr)
        self.assertFalse(self.state.exists())
        self.assertFalse((self.pilot / "legacy").exists())


    def test_a_resume_that_fails_is_loud_and_exits_3(self):
        script = (self.root / "bin" / "pause-runs.sh").read_text().replace("resume) rm -f", "resume) true")
        (self.root / "bin" / "pause-runs.sh").write_text(script)
        r = self.tool("overnight", "--dir", str(self.pilot), "--hours", "1", ok=False)
        self.assertEqual(r.returncode, 3)
        self.assertIn("PRODUCTION IS STILL PAUSED", r.stderr)

    def test_an_unreadable_pause_state_is_not_taken_over(self):
        script = (self.root / "bin" / "pause-runs.sh").read_text().replace("status) [", "status) exit 2; [")
        (self.root / "bin" / "pause-runs.sh").write_text(script)
        r = self.tool("overnight", "--dir", str(self.pilot), "--hours", "1", ok=False)
        self.assertIn("cannot be read", r.stderr)
        self.assertFalse(self.state.exists())
        self.assertFalse((self.pilot / "legacy").exists())

    def test_a_production_guardian_alive_without_its_controller_counts(self):
        sys.path.insert(0, str(BIN))
        from review_guardian import identity
        atomic(self.production / "runs" / "all-9" / "summary.json", dict(state="running"))     # controller gone
        atomic(self.production / "runs" / "all-9" / "attempts" / "a" / "status.json",
               dict(identity(), state="running"))
        r = self.tool("overnight", "--dir", str(self.pilot), "--hours", "1", ok=False)
        self.assertIn("production work is still running", r.stderr)


    def test_a_live_pilot_guardian_keeps_production_paused(self):
        sys.path.insert(0, str(BIN))
        import importlib.util
        spec = importlib.util.spec_from_file_location("pilot", str(BIN / "review-pilot.py"))
        pilot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pilot)
        from review_guardian import identity
        # a guardian whose record says terminal but whose process lives on
        status = self.pilot / "legacy" / "data" / "runs" / "pilot-legacy" / "attempts" / "a" / "status.json"
        atomic(status, dict(identity(), state="terminal"))
        self.assertTrue(pilot.live_work(str(self.pilot / "legacy" / "data")))
        self.assertFalse(pilot.stop_arms(self.pilot, [], wait=1))
        # its arm was asked to stop even with no launcher alive, and so was
        # the arm whose run has not appeared yet
        self.assertTrue((status.parents[2] / "abort.json").exists())
        self.assertTrue((self.pilot / "paging" / "data" / "runs" / "pilot-paging" / "abort.json").exists())

    def test_a_launch_without_a_status_yet_counts_as_live(self):
        sys.path.insert(0, str(BIN))
        import importlib.util
        spec = importlib.util.spec_from_file_location("pilot", str(BIN / "review-pilot.py"))
        pilot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pilot)
        from review_guardian import identity
        data = str(self.pilot / "legacy" / "data")
        attempt = self.pilot / "legacy" / "data" / "runs" / "pilot-legacy" / "attempts" / "b"
        boot = identity()["boot"]
        atomic(attempt / "launch.json", dict(boot=boot, backend="plain"))      # as written: boot, no pid yet
        self.assertTrue(pilot.live_work(data))
        old = time.time() - 7200
        os.utime(attempt / "launch.json", (old, old))
        self.assertIsNone(pilot.live_work(data))                                # its guardian never started
        # a dead guardian whose payload was not proved gone, and still lives
        gone = dict(boot=boot, pid=4194303, start=1)
        atomic(attempt / "status.json", dict(gone, state="cleanup_blocked", empty=False, payload=identity()))
        self.assertTrue(pilot.live_work(data))
        atomic(attempt / "status.json", dict(gone, state="terminal", empty=True))
        self.assertIsNone(pilot.live_work(data))                                # proved gone
        atomic(attempt / "status.json", dict(gone, boot="an-earlier-boot", state="cleanup_blocked", empty=False))
        self.assertIsNone(pilot.live_work(data))                                # nothing survives a reboot


class Delivery(unittest.TestCase):
    def test_frozen_inputs_never_deliver_whoever_drains(self):
        sys.path.insert(0, str(BIN))
        import time as _time
        from review_delivery import Delivery as D
        delivery = D(Store(workspace(self)), None, dict(frozen_inputs=True))
        for method in (delivery.deliver, delivery.reconcile):
            with self.assertRaises(OSError):
                method(dict(kind="publish", pr="pr:owner/repo#1", generation="op", target="page:review/x.html",
                            configuration=dict(frozen_inputs=True)), _time.monotonic() + 5)


class Score(unittest.TestCase):
    def setUp(self):
        self.dir = workspace(self)
        # legacy found A (blocking) and B; paging found A and C, plus a claimed blocker that is no problem
        key = {"a1": dict(arm="legacy", pr="p1"), "b1": dict(arm="legacy", pr="p1"),
               "a2": dict(arm="paging", pr="p1"), "c2": dict(arm="paging", pr="p1"), "x2": dict(arm="paging", pr="p1")}
        atomic(self.dir / "adjudication-key.json", key)
        atomic(self.dir / "adjudication-pack.json", [dict(id=k, pr="p1", claimed_blocking=(k in ("x2", "c2")))
                                                     for k in key])

    def score(self, verdicts):
        atomic(self.dir / "verdicts.json", verdicts)
        r = subprocess.run([sys.executable, str(BIN / "review-pilot.py"), "score", "--dir", str(self.dir),
                            "--verdicts", str(self.dir / "verdicts.json")], capture_output=True, text=True)
        return r, read(self.dir / "score.json") if r.returncode == 0 else None

    BASE = {"a1": dict(real=True, blocking=True, issue="A"), "b1": dict(real=True, blocking=False, issue="B"),
            "a2": dict(real=True, blocking=True, issue="A"), "c2": dict(real=True, blocking=False, issue="C"),
            "x2": dict(real=False)}

    def test_misses_gains_and_false_blockers(self):
        r, result = self.score(self.BASE)
        self.assertEqual((result["missed_blockers"], result["missed_other"], result["gained"]), (0, 1, 1))
        # x2 is no problem, and c2 is real but should not block: both are false blockers
        self.assertEqual(result["false_blockers"], {"legacy": 0, "paging": 2})
        self.assertTrue(result["passed"])
        r, result = self.score(dict(self.BASE, a2=dict(real=False)))
        self.assertEqual(result["missed_blockers"], 1)
        self.assertFalse(result["passed"])

    def test_incomplete_or_inconsistent_verdicts_are_refused(self):
        for verdicts, needle in (({"a1": dict(real=True)}, "verdicts missing"),
                                 (dict(self.BASE, b1={}), "real must be"),
                                 (dict(self.BASE, b1=dict(real=True)), "needs blocking"),
                                 (dict(self.BASE, a2=dict(real=True, blocking=False, issue="A")), "both blocking")):
            r, result = self.score(verdicts)
            self.assertNotEqual(r.returncode, 0, needle)
            self.assertIn(needle, r.stderr)


if __name__ == "__main__":
    unittest.main()
