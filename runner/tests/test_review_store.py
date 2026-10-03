#!/usr/bin/env python3
"""Replay each durable boundary instead of inferring success from leftover JSON."""
import copy
from pathlib import Path
import subprocess
import time
import unittest

from review_fixtures import BIN, PR, complete_claim, python, stop, workspace
from review_lock import try_lock
from review_store import Store, StubAdapter, atomic, delivery_id, digest, read


class Crash(Exception):
    pass


class ReviewStore(unittest.TestCase):
    def setUp(self):
        self.root = workspace(self)
        self.store = Store(self.root)
        self.lock = try_lock(self.store.locks, PR)
        self.addCleanup(self.lock.close)

    def intents(self):
        return [{"kind": "publish", "target": "page:end/a"},
                {"kind": "publish", "target": "page:end/b"},
                {"kind": "comment", "target": PR}]

    def accept(self):
        claim = complete_claim(self.store, self.lock)
        return self.store.accept(self.lock, PR, claim, self.intents())

    def test_each_acceptance_crash_boundary_recovers_all_intents(self):
        boundaries = ["prepared", "bundle_file", "bundle_rename", "bundle_fsync", "bundle_complete",
                      "bundle_directory_rename", "bundle_directory_fsync", "claim_checked",
                      "current_file", "current_rename", "current_fsync", "intent_file", "intent_rename", "intent_fsync"]
        self.lock.close()
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                store = Store(self.root / boundary)
                lock = try_lock(store.locks, PR)
                with lock:
                    claim = complete_claim(store, lock)
                    def crash(point):
                        if point == boundary:
                            raise Crash(point)
                    store.crash = crash
                    with self.assertRaises(Crash):
                        store.accept(lock, PR, claim, self.intents())
                    store.crash = lambda point: None
                    store.recover_pr(lock, PR)
                    self.assertEqual(store.current(PR)["generation"], 1)
                    self.assertEqual(len(list((store.root / "outbox").glob("*.json"))), 3)
                    self.assertEqual(len(list((store.pr_dir(PR) / "generations").glob(".bundle-*"))), 0)

    def test_promotion_is_the_commit_point_and_fenced_orphans_stay_unaccepted(self):
        claim = complete_claim(self.store, self.lock)
        def crash(point):
            if point == "bundle_directory_fsync":
                raise Crash()
        self.store.crash = crash
        with self.assertRaises(Crash):
            self.store.accept(self.lock, PR, claim, self.intents())
        self.assertIsNone(self.store.current(PR))
        self.assertFalse(list((self.root / "outbox").glob("*.json")))
        self.store.crash = lambda point: None
        newer = self.store.allocate(self.lock, PR, "other", "new", claim["inputs"])
        self.assertEqual(newer["generation"], 2)
        self.store.recover_pr(self.lock, PR)
        self.assertIsNone(self.store.current(PR))

    def test_acceptance_rechecks_terminal_exit_and_selected_identity(self):
        claim = complete_claim(self.store, self.lock)
        path = Path(claim["selected"]["primary"]) / "status.json"
        status = read(path)
        for changes in ({"exit": 9}, {"aborted": True}, {"timed_out": True}, {"attempt": "stale"}, {"empty": False}):
            atomic(path, dict(status, **changes))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.accept(self.lock, PR, claim, self.intents())
        atomic(path, status)
        pointer = self.store.accept(self.lock, PR, claim, self.intents())
        self.assertEqual(pointer["generation"], 1)

    def test_predecessors_rebuild_and_supersede_unsent_comments(self):
        self.accept()
        for path in (self.root / "outbox").glob("*.json"):
            path.unlink()
        claim = complete_claim(self.store, self.lock, run="new", request="new")
        self.store.accept(self.lock, PR, claim, [{"kind": "comment", "target": PR}])
        self.assertEqual(len(self.store.bundle(PR)["intents"]), 3)
        self.assertEqual(len(list(self.store.chain(PR))), 2)
        old = delivery_id(PR, 1, "comment", PR)
        self.assertEqual(read(self.root / "receipts" / (old + ".json"))["state"], "superseded")
        self.assertEqual(len(list((self.root / "outbox").glob("*.json"))), 5)

    def test_retiring_a_page_supersedes_its_debts_and_frees_the_comment(self):
        self.accept()
        outbox = self.root / "outbox"
        comment = delivery_id(PR, 1, "comment", PR)
        ident = delivery_id(PR, 1, "publish", "page:end/a")
        entry = read(outbox / (ident + ".json"))
        entry.update(state="uncertain", failures=5, error="rsync failed: Unknown module")
        atomic(outbox / (ident + ".json"), entry)
        # the comment waits on the page, and a run-journal republish of the
        # same page is keyed by operation rather than generation
        waiting = read(outbox / (comment + ".json"))
        waiting["dependencies"] = [ident]
        atomic(outbox / (comment + ".json"), waiting)
        op = dict(entry, id=delivery_id(PR, "op", "publish", "page:end/a"), generation="op", gate="page")
        atomic(outbox / (op["id"] + ".json"), op)
        self.lock.close()
        self.assertFalse(self.store.drain(StubAdapter(self.root)) == [])
        self.assertTrue((outbox / (comment + ".json")).exists())
        child = python((BIN / "review-retire.py").read_text(), "--data", self.root,
                       "--reason", "module gone", "page:end/a", PR,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = child.communicate(timeout=30)
        self.assertEqual(child.returncode, 0, err)
        for retired in (ident, op["id"]):
            receipt = read(self.root / "receipts" / (retired + ".json"))
            self.assertEqual((receipt["state"], receipt["reason"]), ("superseded", "module gone"))
            self.assertFalse((outbox / (retired + ".json")).exists())
        rows = read(self.root / "membership" / (digest("page:end/a") + ".json"))
        self.assertTrue(rows[PR]["removed"])
        lock = try_lock(self.store.locks, PR)
        with lock:
            self.store.rebuild(lock, PR)
        self.assertFalse((outbox / (ident + ".json")).exists())
        self.store.drain(StubAdapter(self.root))
        self.assertEqual(read(self.root / "receipts" / (comment + ".json"))["state"], "posted")
        # the next generation inherits page b, a mutable destination, but not
        # the retired page a
        with try_lock(self.store.locks, PR) as lock:
            claim = complete_claim(self.store, lock, run="new", request="new")
            self.store.accept(lock, PR, claim, [{"kind": "comment", "target": PR}])
        targets = [i["target"] for i in self.store.bundle(PR)["intents"] if i["kind"] == "publish"]
        self.assertEqual(targets, ["page:end/b"])

    def test_one_page_publish_settles_every_owed_publish_of_that_page(self):
        # every progress step of every PR on a page journals a publish of it;
        # the page renders whole, so one delivery answers them all
        target = "page:end/shared"
        outbox = self.root / "outbox"
        entries = []
        for n in range(1, 4):
            projection = dict(id=f"proj{n}", pr=f"pr:owner/repo#{n}", generation=f"op{n}", kind="projection",
                              target=target, gate="page", patches={f"pr:owner/repo#{n}": {"ticket": n + 1, "removed": False}},
                              state="owed", failures=0, next_attempt=0)
            publish = dict(id=f"pub{n}", pr=f"pr:owner/repo#{n}", generation=f"op{n}", kind="publish",
                           target=target, gate="page", dependencies=[f"proj{n}"], state="owed", failures=0, next_attempt=0)
            for entry in (projection, publish):
                atomic(outbox / (entry["id"] + ".json"), entry)
            entries.append(publish)
        # a publish whose projection has not merged must wait
        atomic(outbox / "pub9.json", dict(id="pub9", pr="pr:owner/repo#9", generation="op9", kind="publish",
                                          target=target, gate="page", dependencies=["proj9"], state="owed",
                                          failures=0, next_attempt=0))
        self.lock.close()
        adapter = StubAdapter(self.root)
        self.store.drain(adapter)
        for entry in entries:
            self.assertEqual(read(self.root / "receipts" / (entry["id"] + ".json"))["state"], "published")
        delivered = [read(p)["entry"]["id"] for p in adapter.root.glob("*.json") if read(p)["entry"]["kind"] == "publish"]
        self.assertEqual(len(delivered), 1)
        self.assertTrue((outbox / "pub9.json").exists())
        self.assertFalse((self.root / "receipts" / "pub9.json").exists())

    def test_a_bounded_pass_takes_projections_before_the_publishes_that_wait_on_them(self):
        target = "page:end/shared"
        outbox = self.root / "outbox"
        # ids chosen so every publish sorts before every projection by id alone
        for n in range(1, 6):
            atomic(outbox / f"zzz-proj{n}.json", dict(id=f"zzz-proj{n}", pr=f"pr:owner/repo#{n}", generation=f"op{n}",
                                                     kind="projection", target=target, gate="page",
                                                     patches={f"pr:owner/repo#{n}": {"ticket": n, "removed": False}},
                                                     state="owed", failures=0, next_attempt=0))
            atomic(outbox / f"aaa-pub{n}.json", dict(id=f"aaa-pub{n}", pr=f"pr:owner/repo#{n}", generation=f"op{n}",
                                                    kind="publish", target=target, gate="page",
                                                    dependencies=[f"zzz-proj{n}"], state="owed", failures=0, next_attempt=0))
        self.lock.close()
        self.store.drain(StubAdapter(self.root), limit=6)
        self.assertEqual(list((self.root / "outbox").glob("*.json")), [])

    def test_an_entry_removed_by_a_concurrent_drain_does_not_end_this_one(self):
        # read() answers None for a file that vanished between glob and read;
        # a null body is the same thing to the snapshot
        (self.root / "outbox").mkdir(exist_ok=True)
        (self.root / "outbox" / "gone.json").write_text("null")
        self.assertEqual(self.store.delivery_snapshot(), [])
        self.lock.close()
        self.store.drain(StubAdapter(self.root))

    def test_a_journal_is_fanned_out_at_once_and_forgotten_when_delivered(self):
        ident = self.store.journal("run", "discovery", PR, [
            {"kind": "projection", "target": "page:end/q", "gate": "page",
             "patches": {PR: {"ticket": 1, "removed": False}}}])
        projection = delivery_id(PR, ident, "projection", "page:end/q")
        self.assertTrue((self.root / "outbox" / (projection + ".json")).exists())
        self.lock.close()
        self.store.drain(StubAdapter(self.root))
        self.assertTrue((self.root / "receipts" / (projection + ".json")).exists())
        self.store.recover_slice()
        self.assertFalse((self.root / "operations" / (ident + ".json")).exists())

    def test_a_journal_a_dead_controller_never_fanned_out_is_recovered_by_the_drain(self):
        # the controller journalled a page operation and died before the
        # outbox entries existed; a publish elsewhere waits on its projection
        # the page is busy at journal time, so nothing is fanned out then
        holder = python("import sys,time; from review_lock import try_lock; l = try_lock(sys.argv[1], sys.argv[2]); "
                        "print('held', flush=True); time.sleep(30)", self.store.locks, "page:end/p",
                        stdout=subprocess.PIPE, text=True)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        ident = self.store.journal("run", "discovery", PR, [
            {"kind": "projection", "target": "page:end/p", "gate": "page",
             "patches": {PR: {"ticket": 1, "removed": False}}}])
        stop(holder)
        projection = delivery_id(PR, ident, "projection", "page:end/p")
        self.assertFalse((self.root / "outbox" / (projection + ".json")).exists())
        waiting = dict(id="waiting", pr=PR, generation=1, kind="publish", target="page:end/p",
                       dependencies=[projection], state="owed", failures=0, next_attempt=0)
        atomic(self.root / "outbox" / "waiting.json", waiting)
        self.lock.close()
        self.store.drain(StubAdapter(self.root))
        self.assertTrue((self.root / "outbox" / "waiting.json").exists())
        pending = self.store.recover_slice()
        self.assertFalse(pending)
        self.store.drain(StubAdapter(self.root))
        self.assertEqual(read(self.root / "receipts" / (projection + ".json"))["state"], "published")
        self.assertFalse((self.root / "outbox" / "waiting.json").exists())

    def test_a_page_row_keeps_only_what_the_page_needs(self):
        # a row once held the whole discovery candidate, diff and thread
        # included, and every projection rewrote megabytes of it
        page = "page:end/rows"
        self.lock.close()
        self.store.merge_membership(page, {PR: {"ticket": 1, "removed": False, "ci": {"state": "passing"},
                                                "candidate": {"diff": "x" * 100000}}})
        row = read(self.root / "membership" / (digest(page) + ".json"))[PR]
        self.assertNotIn("candidate", row)
        self.assertEqual(row["ci"], {"state": "passing"})

    def finish_with_reconciliation(self, claim):
        from review_schema import FILES, canned
        from review_store import mkdir
        results = {k: read(Path(claim["selected"][k]) / FILES[k]) for k in ("primary", "cold", "validation")}
        path = self.root / "attempt-fixtures" / str(claim["generation"]) / "reconciliation"
        mkdir(path)
        inputs = claim["inputs"]
        job = {k: inputs[k] for k in ("repository", "number", "node_id", "head", "base", "merge_base")}
        job.update(schema=1, run=claim["run"], job="reconciliation", attempt=str(path),
                   generation=claim["generation"], kind="reconciliation", input_digest=digest(inputs),
                   primary_ids=[], finding_ids=[], results=results)
        atomic(path / "job.json", job)
        atomic(path / "final.json", canned(job))
        atomic(path / "status.json", dict(job, state="terminal", exit=0, timed_out=False,
                                          aborted=False, result_status="complete", empty=True))
        claim["attempts"].append(str(path))
        claim["selected"]["reconciliation"] = str(path)
        self.store.save_claim(self.lock, PR, claim)
        return claim

    def test_a_later_claim_over_the_same_inputs_keeps_the_earlier_passes(self):
        from review_fixtures import candidate
        first = complete_claim(self.store, self.lock, run="first")
        first["status"] = "deferred"
        del first["selected"]["reconciliation"]
        self.store.save_claim(self.lock, PR, first)
        # a later run, same review inputs; its observation ticket differs
        second = self.store.allocate(self.lock, PR, "second", "request", dict(candidate(), observation=9))
        self.assertEqual(sorted(second["carried"]), ["cold", "primary", "validation"])
        self.assertEqual(second["selected"]["primary"], first["selected"]["primary"])
        self.assertNotIn("reconciliation", second["selected"])
        # it only needs its reconciliation, and acceptance takes the carried passes
        second = self.finish_with_reconciliation(second)
        self.assertEqual(sorted(self.store.selected(PR, second)), ["cold", "primary", "reconciliation", "validation"])

    def test_nothing_carries_when_the_review_inputs_changed(self):
        from review_fixtures import candidate
        first = complete_claim(self.store, self.lock, run="first")
        first["status"] = "deferred"
        self.store.save_claim(self.lock, PR, first)
        moved = self.store.allocate(self.lock, PR, "second", "request", dict(candidate(), head="d" * 40))
        self.assertEqual(moved["selected"], {})
        self.assertNotIn("carried", moved)

    def test_a_carried_pass_that_no_longer_checks_out_is_refused_at_acceptance(self):
        from review_fixtures import candidate
        first = complete_claim(self.store, self.lock, run="first")
        first["status"] = "deferred"
        self.store.save_claim(self.lock, PR, first)
        second = self.finish_with_reconciliation(
            self.store.allocate(self.lock, PR, "second", "request", candidate()))
        status = Path(second["carried"]["cold"]) / "status.json"
        atomic(status, dict(read(status), exit=1))
        with self.assertRaises(ValueError):
            self.store.selected(PR, second)

    def test_validation_is_not_carried_without_its_primary(self):
        from review_fixtures import candidate
        first = complete_claim(self.store, self.lock, run="first")
        first["status"] = "deferred"
        status = Path(first["selected"]["primary"]) / "status.json"
        atomic(status, dict(read(status), exit=1))
        self.store.save_claim(self.lock, PR, first)
        second = self.store.allocate(self.lock, PR, "second", "request", candidate())
        # cold is independent and carries; validation was made against the
        # primary that failed, so neither comes across
        self.assertEqual(sorted(second.get("carried", {})), ["cold"])

    def test_receipt_wins_at_each_receipt_boundary(self):
        self.accept()
        entry = read(next((self.root / "outbox").glob("*.json")))
        for boundary in ("receipt_file", "receipt_rename", "receipt_fsync", "outbox_removed"):
            with self.subTest(boundary=boundary):
                receipt_path = self.root / "receipts" / (entry["id"] + ".json")
                if receipt_path.exists():
                    receipt_path.unlink()
                atomic(self.root / "outbox" / (entry["id"] + ".json"), entry)
                def crash(point):
                    if point == boundary:
                        raise Crash()
                self.store.crash = crash
                with self.assertRaises(Crash):
                    self.store.receipt(entry, "published")
                self.store.crash = lambda point: None
                self.store.recover_pr(self.lock, PR)
                self.assertEqual((self.root / "outbox" / (entry["id"] + ".json")).exists(), boundary == "receipt_file")

    def test_lost_response_reconciles_instead_of_repeating_effect(self):
        self.accept()
        self.lock.close()
        adapter = StubAdapter(self.root)
        def crash(point):
            if point == "remote_effect":
                raise Crash()
        self.store.crash = crash
        with self.assertRaises(Crash):
            self.store.drain(adapter)
        effects = list(adapter.root.glob("*.json"))
        self.assertEqual(len(effects), 1)
        first = effects[0].stat().st_mtime_ns
        self.store.crash = lambda point: None
        self.store.drain(adapter)
        self.assertEqual(effects[0].stat().st_mtime_ns, first)
        self.assertFalse(list((self.root / "outbox").glob("*.json")))

    def test_tombstones_and_accepted_generation_are_independent(self):
        self.accept()
        page = "page:end/latest"
        self.store.merge_membership(page, {PR: {"ticket": 2, "removed": False, "generation": 1}})
        self.store.merge_membership(page, {PR: {"ticket": 4, "removed": True}})
        rows = self.store.merge_membership(page, {PR: {"ticket": 3, "removed": False, "generation": 1},
                                                        "pr:other/repo#2": {"ticket": 1, "removed": False}})
        self.assertTrue(rows[PR]["removed"])
        self.assertIn("pr:other/repo#2", rows)
        rows = self.store.merge_membership(page, {PR: {"ticket": 5, "removed": False}})
        self.assertEqual(rows[PR]["generation"], 1)
        with self.assertRaises(ValueError):
            self.store.merge_membership(page, {PR: {"generation": 2}})

    def test_delivery_ids_and_receipts_are_retained(self):
        self.assertEqual(delivery_id("pr:OWNER/REPO#01", 2, "comment", PR), delivery_id(PR, 2, "comment", PR))
        self.accept()
        self.lock.close()
        self.store.drain(StubAdapter(self.root))
        self.assertEqual(len(list((self.root / "receipts").glob("*.json"))), 3)
        self.store.recover()
        self.assertFalse(list((self.root / "outbox").glob("*.json")))

    def test_operation_journal_recovers_without_an_acceptance(self):
        ident = self.store.journal("run", "discovery", PR, [{"kind": "publish", "target": "page:end/a", "gate": "page"}])
        # the controller died between the journal and its fan-out
        for path in (self.root / "outbox").glob("*.json"):
            path.unlink()
        self.lock.close()
        self.store.recover()
        self.assertEqual(read(next((self.root / "outbox").glob("*.json")))["generation"], ident)
        self.store.drain(StubAdapter(self.root))
        self.assertEqual(len(list((self.root / "receipts").glob("*.json"))), 1)

    def test_evidence_is_persisted_before_promotion_even_after_a_crash(self):
        claim = complete_claim(self.store, self.lock)
        source = Path(claim["selected"]["primary"])
        result = read(source / "review.json")
        result["findings"] = [{"id": "primary:F1", "kind": "NOTE", "severity": "informational",
                               "claim": "fixture", "location": {"non_line_specific": True},
                               "status": "VERIFIED", "evidence": {"commands": [], "artifacts": ["proof.txt"], "configuration": "fixture"}}]
        atomic(source / "review.json", result)
        (source / "proof.txt").write_text("retained evidence")
        validation = Path(claim["selected"]["validation"])
        validation_job = read(validation / "job.json")
        validation_job.update(primary_result=result, primary_ids=["primary:F1"])
        atomic(validation / "job.json", validation_job)
        validation_result = read(validation / "validate.json")
        validation_result["outcomes"] = [{"id": "primary:F1", "outcome": "CONFIRM", "evidence": {"commands": [], "artifacts": [], "configuration": "fixture"}}]
        atomic(validation / "validate.json", validation_result)
        reconciliation = Path(claim["selected"]["reconciliation"])
        reconciliation_job = read(reconciliation / "job.json")
        reconciliation_job["results"].update(primary=result, validation=validation_result)
        reconciliation_job["finding_ids"] = ["primary:F1"]
        atomic(reconciliation / "job.json", reconciliation_job)
        final = read(reconciliation / "final.json")
        final["outcomes"] = [{"id": "primary:F1", "blocking": False, "actionable": False, "disposition": "retained", "rationale": "", "evidence": {"commands": [], "artifacts": [], "configuration": "fixture"}}]
        atomic(reconciliation / "final.json", final)
        def crash(point):
            if point == "evidence_file":
                raise Crash()
        self.store.crash = crash
        with self.assertRaises(Crash):
            self.store.accept(self.lock, PR, claim, self.intents())
        self.assertIsNone(self.store.current(PR))
        def crash_directory(point):
            if point == "evidence_fsync":
                raise Crash()
        self.store.crash = crash_directory
        with self.assertRaises(Crash):
            self.store.recover_pr(self.lock, PR)
        self.store.crash = lambda point: None
        self.store.recover_pr(self.lock, PR)
        proof = self.store.pr_dir(PR) / "generations/1/evidence/primary/proof.txt"
        self.assertEqual(proof.read_text(), "retained evidence")

    def test_allocation_crash_never_reuses_a_generation(self):
        from review_fixtures import candidate
        def crash(point):
            if point == "allocated":
                raise Crash()
        self.store.crash = crash
        with self.assertRaises(Crash):
            self.store.allocate(self.lock, PR, "run", "request", candidate())
        self.store.crash = lambda point: None
        self.store.recover_pr(self.lock, PR)
        self.assertIsNone(self.store.current(PR))
        claim = complete_claim(self.store, self.lock)
        self.assertEqual(claim["generation"], 2)

    def test_membership_crashes_replay_the_journal_and_preserve_tombstones(self):
        self.lock.close()
        page = "page:end/label"
        for boundary in ("membership_file", "membership_rename", "membership_fsync"):
            with self.subTest(boundary=boundary):
                store = Store(self.root / boundary)
                store.journal("run", "discovery", PR, [{"kind": "projection", "target": page, "gate": "page",
                                                        "patches": {PR: {"ticket": 1, "removed": False}}}])
                store.recover()
                def crash(point):
                    if point == boundary:
                        raise Crash()
                store.crash = crash
                with self.assertRaises(Crash):
                    store.drain(StubAdapter(store.root))
                store.crash = lambda point: None
                store.merge_membership(page, {PR: {"ticket": 2, "removed": True}})
                store.recover()
                store.drain(StubAdapter(store.root))
                rows = store.merge_membership(page, {})
                self.assertTrue(rows[PR]["removed"])
                self.assertFalse(list((store.root / "outbox").glob("*.json")))

    def test_sending_old_comment_is_reconciled_before_newer_comment(self):
        self.accept()
        old_id = delivery_id(PR, 1, "comment", PR)
        path = self.root / "outbox" / (old_id + ".json")
        old = read(path)
        old.update(state="uncertain", payload_digest="fixture")
        atomic(path, old)
        claim = complete_claim(self.store, self.lock, run="new", request="new")
        self.store.accept(self.lock, PR, claim, self.intents())
        self.assertTrue(path.exists())
        self.assertFalse((self.root / "receipts" / (old_id + ".json")).exists())
        self.lock.close()
        adapter = StubAdapter(self.root)
        for _ in range(3):
            self.store.drain(adapter)
        self.assertEqual(read(self.root / "receipts" / (old_id + ".json"))["state"], "superseded")
        self.assertFalse((adapter.root / (old_id + ".json")).exists())

    def test_failed_delivery_has_a_finite_retry_budget_and_contention_costs_none(self):
        self.accept()
        class Failure:
            def deliver(self, entry, deadline):
                raise OSError("offline")
            reconcile = deliver
        adapter = Failure()
        self.store.drain(adapter)
        self.assertTrue(all(read(p)["failures"] == 0 for p in (self.root / "outbox").glob("*.json")))
        self.lock.close()
        for _ in range(6):
            for path in (self.root / "outbox").glob("*.json"):
                entry = read(path)
                entry["next_attempt"] = 0
                atomic(path, entry)
            self.store.drain(adapter)
        self.assertTrue(all(read(p)["failures"] == 5 for p in (self.root / "outbox").glob("*.json")))

    def test_drain_uses_a_finite_snapshot_and_count_budget(self):
        self.accept()
        self.lock.close()
        adapter = StubAdapter(self.root)
        self.store.drain(adapter, limit=1)
        self.assertEqual(len(list(adapter.root.glob("*.json"))), 1)
        self.store.drain(adapter, seconds=0)
        self.assertEqual(len(list(adapter.root.glob("*.json"))), 1)

    def test_blocked_entries_do_not_fill_the_drains_snapshot(self):
        self.accept()
        self.lock.close()
        outbox = self.root / "outbox"
        ready = sorted(read(p)["id"] for p in outbox.glob("*.json")
                       if not read(p).get("dependencies"))
        self.assertTrue(ready)
        for i in range(5):
            blocked = dict(read(outbox / (ready[0] + ".json")), id="blocked%d" % i,
                           next_attempt=0, dependencies=["never-settles"])
            atomic(outbox / ("blocked%d.json" % i), blocked)
        snapshot = self.store.delivery_snapshot(limit=len(ready))
        self.assertEqual(sorted(x["id"] for x in snapshot), ready)

    def test_a_started_delivery_gets_its_own_minute_not_the_budgets_remainder(self):
        self.accept()
        self.lock.close()
        adapter = StubAdapter(self.root)
        seen = []
        real = adapter.deliver
        adapter.deliver = lambda entry, deadline: seen.append(deadline - time.monotonic()) or real(entry, deadline)
        self.store.drain(adapter, limit=1, seconds=0.5)
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], self.store.ENTRY_SECONDS - 5)
        self.assertFalse([x for x in (read(p) for p in (self.root / "outbox").glob("*.json"))
                          if x and x["failures"]])

    def test_changed_selected_pass_cannot_reuse_its_old_validation(self):
        claim = complete_claim(self.store, self.lock)
        path = Path(claim["selected"]["primary"]) / "review.json"
        result = read(path)
        result["clean"] = ["changed after validation"]
        atomic(path, result)
        reconciliation = Path(claim["selected"]["reconciliation"]) / "job.json"
        job = read(reconciliation)
        job["results"]["primary"] = result
        atomic(reconciliation, job)
        with self.assertRaises(ValueError):
            self.store.accept(self.lock, PR, claim, self.intents())

    def test_destination_gate_is_held_through_the_receipt(self):
        claim = complete_claim(self.store, self.lock)
        page = "page:end/a"
        self.store.accept(self.lock, PR, claim, [{"kind": "publish", "target": page}])
        self.lock.close()
        checks = []
        def check():
            child = python("import sys; from review_lock import try_lock; print(try_lock(sys.argv[1],sys.argv[2]) is None)",
                           self.store.locks, page, stdout=subprocess.PIPE, text=True)
            output, _ = child.communicate(timeout=5)
            checks.append(output.strip())
        class Adapter(StubAdapter):
            def deliver(self, entry, deadline):
                check()
                return super().deliver(entry, deadline)
        def receipt_boundary(point):
            if point == "receipt_fsync":
                check()
        self.store.crash = receipt_boundary
        self.store.drain(Adapter(self.root))
        self.assertEqual(checks, ["True", "True"])


if __name__ == "__main__":
    unittest.main()
