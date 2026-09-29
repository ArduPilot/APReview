#!/usr/bin/env python3
"""Real OFD ownership, including exec and colliding stripes."""
import hashlib
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest

from review_fixtures import BIN, PR, python, stop, until, workspace
from review_lock import (LAYOUT, MAGIC, TABLE, WIDTH, canonical, pages, permit,
                         region, try_lock)
import time


class ReviewLocks(unittest.TestCase):
    def setUp(self):
        self.root = workspace(self)
        self.path = self.root / "locks"

    def take(self, key):
        lock = try_lock(self.path, key)
        if lock:
            self.addCleanup(lock.close)
        return lock

    def test_mapping_and_normalisation(self):
        self.assertEqual(canonical("pr:Owner/RePo#001"), PR)
        self.assertEqual(canonical("pr:old/repo#1", {"old/repo": "owner/repo"}), PR)
        self.assertEqual(canonical("page:end/a//./B"), "page:end/a/B")
        self.assertEqual(canonical("account:codex/A"), "account:codex/A")
        for key in ("page:end/a/../B", "page:end/a?b", "pr:a/b#0"):
            with self.assertRaises(ValueError):
                canonical(key)
        self.assertEqual(region(PR), 4096 + int.from_bytes(hashlib.sha256(PR.encode()).digest()[:8], "big") % TABLE)
        self.assertEqual(region("permit:codex:255"), 767)
        self.assertGreaterEqual(region("page:end/a"), 4096 + TABLE)
        self.assertGreaterEqual(region("run:/data/review/a"), 4096 + 2 * TABLE)
        self.assertGreaterEqual(region("account:codex/A"), 4096 + 3 * TABLE)

    def test_independent_regions_and_advisory_header(self):
        first = self.take(PR)
        self.assertIsNotNone(first)
        second = self.take("pr:owner/repo#2")
        self.assertIsNotNone(second)
        self.assertIsNone(try_lock(self.path, PR))
        self.assertEqual(os.pread(first.fd, 4, first.region * WIDTH), MAGIC)
        self.assertFalse(os.get_inheritable(first.fd))
        os.pwrite(first.fd, bytes(32), first.region * WIDTH)
        self.assertIsNone(try_lock(self.path, PR))
        first.close()
        self.assertIsNotNone(self.take(PR))

    def test_pool_initializer_cannot_make_an_unrelated_run_busy(self):
        self.take("pause").close()  # establish the immutable layout marker
        with open(self.path, "r+b") as initializer:
            fcntl.flock(initializer, fcntl.LOCK_EX)
            self.assertIsNotNone(self.take("run:" + str(self.root / "independent")))
            self.assertIsNotNone(self.take(PR))
            # Permit claims still share the gate with pool resize.
            self.assertIsNone(try_lock(self.path, "permit:claude:0"))

    def test_unknown_layout_refused(self):
        self.path.write_bytes(b"\0" * LAYOUT + b"RVW\x09")
        with self.assertRaises(ValueError):
            self.take(PR)

    def test_same_stripe_is_not_a_reentrant_lock(self):
        seen = {}
        pair = None
        for n in range(1, 15000):
            key = "pr:collision/repo#%d" % n
            stripe = region(key)
            if stripe in seen:
                pair = (seen[stripe], key)
                break
            seen[stripe] = key
        self.assertIsNotNone(pair)
        first = self.take(pair[0])
        self.assertIsNone(try_lock(self.path, pair[1]))
        first.close()
        self.assertIsNotNone(self.take(pair[1]))

    def test_order_refuses_earlier_kind_and_second_run(self):
        run = self.take("run:" + str(self.root))
        with self.assertRaises(RuntimeError):
            self.take("run:" + str(self.root / "other"))
        self.take(PR)
        page = self.take("page:end/a")
        with self.assertRaises(RuntimeError):
            self.take("pr:owner/repo#2")
        with self.assertRaises(RuntimeError):
            permit(self.path, "codex")
        page.close()
        self.assertIsNotNone(self.take("pr:owner/repo#2"))

    def test_pages_are_sorted_and_deduplicated(self):
        with pages(self.path, ["page:end/a", "page:end/b", "page:end/a"], time.monotonic() + 1):
            child = python("from review_lock import try_lock; import sys; print(try_lock(sys.argv[1], 'page:end/a') is None)", self.path, stdout=subprocess.PIPE, text=True)
            output, _ = child.communicate(timeout=5)
            self.assertEqual(output.strip(), "True")

    def test_exec_inheritance_and_last_holder_death(self):
        lock = self.take(PR)
        ready = self.root / "ready"
        child = python("import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text('ready'); time.sleep(5)", ready,
                       pass_fds=(lock.fd,))
        self.addCleanup(stop, child)
        until(self, ready.exists)
        lock.close()
        self.assertIsNone(try_lock(self.path, PR))
        child.kill()
        child.wait(timeout=5)
        self.assertIsNotNone(self.take(PR))

    def test_permit_pool_is_shared_across_processes(self):
        lock = permit(self.path, "codex", 2)
        self.addCleanup(lock.close)
        self.assertEqual(lock.key, "permit:codex:1")
        code = "from review_lock import permit; import sys; a=permit(sys.argv[1],'codex',2); print(a.key, permit(sys.argv[1],'codex',2), flush=True); sys.stdin.read(1)"
        child = python(code, self.path, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(stop, child)
        import selectors
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(5))
        self.assertEqual(child.stdout.readline().strip(), "permit:codex:0 None")
        self.assertIsNone(permit(self.path, "codex", 2))
        child.communicate("x", timeout=5)
        other = permit(self.path, "codex", 2)
        self.assertIsNotNone(other)
        other.close()

    def test_reserved_slot_is_only_for_finishing_jobs(self):
        primary = permit(self.path, "claude", 2, skip_finishing_slot=True)
        self.addCleanup(primary.close)
        self.assertEqual(primary.key, "permit:claude:1")
        self.assertIsNone(permit(self.path, "claude", 2, skip_finishing_slot=True))
        final = permit(self.path, "claude", 2)
        self.addCleanup(final.close)
        self.assertEqual(final.key, "permit:claude:0")

    def test_runs_with_different_pool_sizes_share_one_physical_pool(self):
        # a run frozen at 2 and a run at 3 coexist; neither waits for a drain
        small = permit(self.path, "heavy", 2)
        self.addCleanup(small.close)
        large = permit(self.path, "heavy", 3)
        self.assertIsNotNone(large)
        self.addCleanup(large.close)
        self.assertEqual({small.key, large.key}, {"permit:heavy:1", "permit:heavy:2"})
        # the small run still has its own slot 0; its cap is its own
        again = permit(self.path, "heavy", 2)
        self.assertEqual(again.key, "permit:heavy:0")
        again.close()
        # the pool never shrinks below what a run has declared
        smaller = permit(self.path, "heavy", 1)
        self.assertEqual(smaller.key, "permit:heavy:0")
        smaller.close()
        with self.assertRaises(ValueError):
            self.take("permit:heavy:3")

    def test_colliding_pages_share_one_sorted_operation_lifetime(self):
        seen = {}
        pair = None
        for n in range(15000):
            key = "page:end/%d" % n
            stripe = region(key)
            if stripe in seen:
                pair = [seen[stripe], key]
                break
            seen[stripe] = key
        self.assertIsNotNone(pair)
        with pages(self.path, pair, time.monotonic() + 1):
            child = python("import sys; from review_lock import try_lock; print(try_lock(sys.argv[1],sys.argv[2]) is None)", self.path, pair[1], stdout=subprocess.PIPE, text=True)
            output, _ = child.communicate(timeout=5)
            self.assertEqual(output.strip(), "True")
        self.assertIsNotNone(self.take(pair[1]))

    def test_an_account_has_a_capped_number_of_session_slots(self):
        # each session is its own guardian process holding one slot
        from review_lock import account_slot
        key = "account:claude//home/one/.claude"
        code = ("import sys,time; from review_lock import account_slot; "
                "l = account_slot(sys.argv[1], sys.argv[2], 4); print(l.key if l else 'none', flush=True); time.sleep(30)")
        holders = []
        for _ in range(4):
            child = python(code, self.path, key, stdout=subprocess.PIPE, text=True)
            self.addCleanup(stop, child)
            holders.append((child, child.stdout.readline().strip()))
        self.assertEqual(len({k for _, k in holders}), 4)
        self.assertNotIn("none", [k for _, k in holders])
        self.assertIsNone(account_slot(self.path, key, 4))
        # the account's own key stays free for probes, admission and cleanup
        probe = try_lock(self.path, key)
        self.assertIsNotNone(probe)
        probe.close()
        stop(holders[0][0])
        again = account_slot(self.path, key, 4)
        self.assertEqual(again.key, holders[0][1])
        again.close()
        # highest first: a larger cap leaves slot 0 to a run capped at one
        for child, _ in holders[1:]:
            stop(child)
        wide = python(code.replace("4)", "8)"), self.path, key, stdout=subprocess.PIPE, text=True)
        self.addCleanup(stop, wide)
        self.assertEqual(wide.stdout.readline().strip(), key + "#7")
        narrow = account_slot(self.path, key, 1)
        self.assertEqual(narrow.key, key + "#0")
        narrow.close()

    def test_shared_leases_leave_the_exclusive_header_unchanged(self):
        key = "account:codex/account"
        exclusive = self.take(key)
        header = os.pread(exclusive.fd, 32, region(key) * WIDTH)
        exclusive.close()
        shared = try_lock(self.path, key, shared=True)
        self.addCleanup(shared.close)
        child = python("import sys; from review_lock import try_lock; s=try_lock(sys.argv[1],sys.argv[2],shared=True); print(s is not None); s.close(); print(try_lock(sys.argv[1],sys.argv[2]) is None)", self.path, key, stdout=subprocess.PIPE, text=True)
        output, _ = child.communicate(timeout=5)
        self.assertEqual(output.splitlines(), ["True", "True"])
        self.assertEqual(os.pread(shared.fd, 32, region(key) * WIDTH), header)


if __name__ == "__main__":
    unittest.main()
