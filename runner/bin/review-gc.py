#!/usr/bin/env python3
"""Garbage collector for the review store: reachability first, age second.

An attempt directory is pinned while any PR's claim names it (carried passes
are copied from it at promotion), or while its run is still live. Unpinned
attempts of finished runs lose their evidence and build trees but keep the
records the dashboard and audits read. Runs older than OLD_RUN_DAYS keep only
their summaries. Anything at the top of the store that the store does not
own is litter from passes told to work "under REVIEW_DATA"; it goes once it
is quiet, unless a live process is using it.

Receipts, operations, membership and generations are never touched here.

Reports by default; --apply deletes. Statistics go to gc/last.json."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_guardian import alive  # noqa: E402
from review_lock import boot_id  # noqa: E402
from review_lock import acquire  # noqa: E402
from review_schema import FILES  # noqa: E402

# what the store and its runtime own at its top level
OWNED = {
    "runs", "receipts", "outbox", "operations", "membership", "results", "pages", "landing",
    "owners", "metrics", "gc", "references", "mirror", "locks", "observation.json",
    "drain-recovery.json", "recovery.json", "runs.html", "tmp", "scratch",
    "held", "handoff", "legacy-facts.json", "quota.json", "stub-deliveries",
    "drain.lock", "drain-last.json", "http-cache", "receipts.db", "receipts.db-wal", "receipts.db-shm",
    "recovered.json", "followup-coverage.json",
}
# Caches agents made inside the store before passes were given shared ones
# under $REVIEW_ROOT/cache are litter like the rest once they go quiet.
# the retired prompt-driven path's work directories; removed only with --legacy
LEGACY = ("fu_", "allrun", "aireview-", "aireview_", "followup-", "allruns-")
# records the dashboard reads, and the guardian's proofs that cleanup needs
PROOFS = {"job.json", "status.json", "launch.json", "manager.json", "empty.json"}
KEEP = PROOFS | {"payload.log", *FILES.values()}
EVIDENCE_DAYS = 2
LITTER_DAYS = 2
SCRATCH_DAYS = 7
OLD_RUN_DAYS = 30
GONE_RUN_DAYS = 90
VENV_DAYS = 14
# a week past the followup window, which reads posting times from receipt files
RECEIPT_DAYS = 21
HTTP_CACHE_DAYS = 14


def load(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def tally(path):
    """Files and bytes under path, without following links."""
    files = size = 0
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            files += 1
                            size += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return files, size


def newer_than(path, cutoff):
    """True if anything under path was modified after cutoff (stops early)."""
    stack = [str(path)]
    try:
        if os.lstat(path).st_mtime > cutoff:
            return True
    except OSError:
        return False
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if st.st_mtime > cutoff:
                        return True
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
        except OSError:
            pass
    return False


def in_use():
    """Working directories and open files of every process we can see."""
    paths = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            paths.add(os.readlink("/proc/%s/cwd" % pid))
        except OSError:
            continue
        try:
            for fd in os.listdir("/proc/%s/fd" % pid):
                try:
                    paths.add(os.readlink("/proc/%s/fd/%s" % (pid, fd)))
                except OSError:
                    pass
        except OSError:
            pass
    return paths


def used(path, paths):
    path = os.path.realpath(path)
    prefix = path.rstrip("/") + "/"
    return any(p == path or p.startswith(prefix) for p in paths)


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)


def pinned_attempts(data):
    """Every attempt any PR's current claim or any owner record names. An
    unreadable record stops the collector: a lost pin is a lost pass."""
    pins = set()
    runs = os.path.realpath(os.path.join(data, "runs")) + "/"
    for record in [*Path(data, "results").glob("*/*/*/claim.json"), *Path(data, "owners").glob("*.json")]:
        value = load(record)
        if value is None:
            if record.exists():
                raise SystemExit("unreadable %s: not collecting" % record)
            continue
        for text in strings(value):
            if text.startswith("/"):
                real = os.path.realpath(text)
                if real.startswith(runs) and "/attempts/" in real:
                    pins.add(real.split("/attempts/")[0] + "/attempts/" + real.split("/attempts/")[1].split("/")[0])
    return pins


def cleaned(attempt):
    """Proof the attempt's processes are gone: a terminal status, written only
    once cleanup found the payload empty, or a launch in an earlier boot,
    since nothing survives a reboot. A missing status alone proves nothing."""
    status = load(attempt / "status.json")
    if status and status.get("state") == "terminal":
        return True
    launch = load(attempt / "launch.json")
    if launch and launch.get("boot") and launch["boot"] != boot_id():
        return True
    return False


def live(record_path):
    record = load(record_path, {})
    return bool(record) and alive(record)


def run_finished(run, now):
    if live(run / "controller.json"):
        return False
    summary = load(run / "summary.json", {})
    if summary.get("state") == "complete":
        return True
    beat = summary.get("heartbeat") or load(run / "run.json", {}).get("created") or 0
    return now - beat > 86400


class GC:
    def __init__(self, data, apply, legacy, cache, budget=600):
        self.data, self.apply, self.legacy = Path(data), apply, legacy
        self.roots = (os.path.realpath(data) + "/", os.path.realpath(cache) + "/")
        self.root_fds = {}
        self.now = time.time()
        self.deadline = time.monotonic() + budget
        self.removed = {}
        self.skipped = {}

    def spent(self):
        return self.apply and time.monotonic() > self.deadline

    @staticmethod
    def walk_open(start_fd, parts):
        """Open each component below start_fd refusing links (no trailing
        slash, so O_NOFOLLOW really refuses a directory link)."""
        fd = os.dup(start_fd)
        try:
            for part in filter(None, parts):
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
            return fd
        except OSError:
            os.close(fd)
            raise

    def root_fd(self, root):
        """The store or cache root, opened once from / one component at a
        time without following links, and kept for the whole collection."""
        if root not in self.root_fds:
            top = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
            try:
                self.root_fds[root] = self.walk_open(top, root.strip("/").split("/"))
            finally:
                os.close(top)
        return self.root_fds[root]

    def parent_fd(self, path):
        """A descriptor for path's parent, opened one component at a time from
        a verified root without following any link, so no ancestor swapped
        for a symlink can carry the deletion elsewhere."""
        for root in self.roots:
            if (str(path.parent) + "/").startswith(root):
                parts = str(path.parent)[len(root):].split("/") if str(path.parent) + "/" != root else []
                return self.walk_open(self.root_fd(root), parts)
        raise OSError("outside the store")

    def remove(self, rule, path):
        if self.spent():
            self.skip(rule, "time budget spent")
            return
        try:
            parent = self.parent_fd(path)
        except OSError:
            self.skip(rule, "outside the store, or reached through a link")
            return
        try:
            st = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            directory = stat.S_ISDIR(st.st_mode)
            files, size = tally(path) if directory else (1, st.st_size)
            if self.spent():
                self.skip(rule, "time budget spent")
                return
            r = self.removed.setdefault(rule, [0, 0, 0])
            r[0] += 1
            r[1] += files
            r[2] += size
            if self.apply:
                if directory:
                    shutil.rmtree(path.name, dir_fd=parent, ignore_errors=True)
                else:
                    os.unlink(path.name, dir_fd=parent)
        except OSError:
            pass
        finally:
            os.close(parent)

    def skip(self, rule, why):
        self.skipped.setdefault(rule, {}).setdefault(why, 0)
        self.skipped[rule][why] += 1

    def any_live(self):
        """A run or a guardian is live: shared caches and litter may be in use
        by something that has not opened them yet, so their rules wait."""
        for run in self.data.glob("runs/*"):
            if live(run / "controller.json"):
                return True
            for attempt in run.glob("attempts/*"):
                if live(attempt / "status.json") or live(attempt / "manager.json"):
                    return True
                # launched but never proved empty: its payload may still run,
                # or start, and open shared caches, whatever the run's age
                if (attempt / "launch.json").exists() and not cleaned(attempt):
                    self.unresolved = getattr(self, "unresolved", []) + [str(attempt)]
        return bool(getattr(self, "unresolved", None))

    def settled_attempt(self, attempt, pins, processes):
        """Why an attempt must stay, or None. Its guardian must have finished
        cleanup (a terminal status): a dead guardian alone does not prove its
        payload's processes are gone."""
        if os.path.realpath(attempt) in pins:
            return "named by a claim"
        if live(attempt / "status.json") or live(attempt / "manager.json"):
            return "guardian alive"
        if (attempt / "wt").exists():
            return "worktree present"
        if not cleaned(attempt):
            return "no cleanup proof"
        if newer_than(attempt, self.now - EVIDENCE_DAYS * 86400) or used(attempt, processes):
            return "recent"
        return None

    def attempts(self, pins, processes):
        if (self.data / "runs").is_symlink():
            self.skip("attempt evidence", "runs is a symlink")
            return
        for run in sorted(self.data.glob("runs/*")):
            if self.spent():
                return
            if not run.is_dir() or run.is_symlink() or (run / "attempts").is_symlink():
                continue
            if not run_finished(run, self.now):
                self.skip("attempt evidence", "run live")
                continue
            created = load(run / "run.json", {}).get("created") or run.stat().st_mtime
            old = self.now - created > OLD_RUN_DAYS * 86400
            attempts = [a for a in sorted(run.glob("attempts/*")) if a.is_dir() and not a.is_symlink()]
            if self.now - created > GONE_RUN_DAYS * 86400 and not used(run, processes):
                # the whole run only when every attempt in it could go
                held = [why for why in (self.settled_attempt(a, pins, processes) for a in attempts) if why]
                if not held:
                    self.remove("expired run", run)
                    continue
            for attempt in attempts:
                why = self.settled_attempt(attempt, pins, processes)
                if why:
                    self.skip("attempt evidence", why)
                    continue
                keep = PROOFS if old else KEEP
                for entry in attempt.iterdir():
                    if entry.name not in keep:
                        self.remove("old run" if old else "attempt evidence", entry)

    def litter(self, processes):
        cutoff = self.now - LITTER_DAYS * 86400
        for entry in sorted(self.data.iterdir()):
            if self.spent():
                return
            name = entry.name
            # store state is JSON at the top; an unknown one is kept, not guessed at
            if name in OWNED or name.endswith(".json"):
                continue
            if name.startswith(LEGACY):
                if not self.legacy:
                    self.skip("legacy work dirs", "needs --legacy")
                elif used(entry, processes) or newer_than(entry, cutoff):
                    self.skip("legacy work dirs", "in use or recent")
                else:
                    self.remove("legacy work dirs", entry)
                continue
            if used(entry, processes):
                self.skip("litter", "in use")
            elif newer_than(entry, cutoff):
                self.skip("litter", "recent")
            elif used(entry, in_use()):          # look again just before
                self.skip("litter", "in use")
            else:
                self.remove("litter", entry)

    def scratch(self, processes):
        cutoff = self.now - SCRATCH_DAYS * 86400
        for area in ("tmp", "scratch"):
            if (self.data / area).is_symlink():
                self.skip(area, "symlink")
                continue
            for entry in sorted((self.data / area).glob("*")):
                if self.spent():
                    return
                if used(entry, processes):
                    self.skip(area, "in use")
                elif newer_than(entry, cutoff):
                    self.skip(area, "recent")
                else:
                    self.remove(area, entry)

    def venvs(self, cache, processes):
        """Shared virtualenvs nobody has touched in VENV_DAYS."""
        cutoff = self.now - VENV_DAYS * 86400
        if Path(cache, "venvs").is_symlink():
            self.skip("venvs", "symlink")
            return
        for entry in sorted(Path(cache, "venvs").glob("*")):
            if self.spent():
                return
            if used(entry, processes):
                self.skip("venvs", "in use")
            elif newer_than(entry, cutoff):
                self.skip("venvs", "recent")
            else:
                self.remove("venvs", entry)

    def http_cache(self, processes):
        """Conditional-request cache entries not used for HTTP_CACHE_DAYS: a
        lost entry only costs one full answer from GitHub."""
        cache = self.data / "http-cache"
        if cache.is_symlink():
            return
        cutoff = self.now - HTTP_CACHE_DAYS * 86400
        for shard in sorted(cache.glob("*")):
            if shard.is_symlink() or not shard.is_dir():
                continue
            for entry in shard.glob("*.json"):
                if self.spent():
                    return
                try:
                    if entry.stat().st_mtime < cutoff:
                        self.remove("http cache", entry)
                except OSError:
                    pass

    def receipts(self):
        """Move old receipt files into the store's ledger: a quarter of a
        million small files become rows, each found by the same lookup."""
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from review_store import Store
        cutoff = self.now - RECEIPT_DAYS * 86400
        if not self.apply:
            n = sum(1 for e in os.scandir(self.data / "receipts")
                    if e.name.endswith(".json") and e.stat().st_mtime < cutoff)
            self.removed["receipts to ledger"] = [n, n, 0]
            return
        moved = Store(self.data).compact_receipts(cutoff, limit=100000, deadline=self.deadline)
        self.removed["receipts to ledger"] = [moved, moved, 0]

    def stats(self):
        areas = {}
        for entry in self.data.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                key = entry.name if (entry.name in OWNED) else (
                    "legacy" if entry.name.startswith(LEGACY) else "litter")
                files, size = tally(entry)
                a = areas.setdefault(key, [0, 0])
                a[0] += files
                a[1] += size
        return {k: dict(files=v[0], bytes=v[1]) for k, v in sorted(areas.items(), key=lambda kv: -kv[1][0])}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default=os.environ.get("REVIEW_DATA", os.path.expanduser("~/review/data")))
    p.add_argument("--apply", action="store_true", help="delete; without it, only report")
    p.add_argument("--legacy", action="store_true", help="also remove the retired path's work directories")
    p.add_argument("--stats", action="store_true", help="count files and bytes per area afterwards")
    p.add_argument("--cache", default=os.path.join(os.environ.get("REVIEW_ROOT", os.path.expanduser("~/review")), "cache"))
    p.add_argument("--budget", type=float, default=600, help="seconds of deletion while admission is held")
    p.add_argument("--root", default=os.environ.get("REVIEW_ROOT", os.path.expanduser("~/review")))
    a = p.parse_args()
    data = os.path.realpath(a.data)
    gc = GC(data, a.apply, a.legacy, a.cache, a.budget)
    started = time.monotonic()
    # Deleting holds the admission fence: with "pause" held exclusively no
    # run can start (run-reviewprs.sh defers it to its next slot), so nothing
    # can begin using what is being removed. Collection happens only when no
    # run or guardian is live, with pins read under the fence.
    # Every controller holds "maintenance" shared for its life (taken right
    # after its run lock), so holding it exclusively means no run is going and
    # none can start, resume or recover until it is released.
    fence = acquire(os.path.join(data, "locks"), "maintenance", time.monotonic() + 5) if a.apply else None
    legacy = None
    try:
        if a.apply:
            # The retired path's run lock, where its runner takes it: while a
            # retired runner holds it, it may use any shared area, so nothing
            # is collected at all.
            try:
                legacy = open(os.path.join(a.root, "etc", "reviewprs.lock"), "a")
                fcntl.flock(legacy, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if legacy:
                    legacy.close()
                legacy = None
        if a.apply and fence is None:
            gc.skip("all rules", "a controller holds the maintenance lock")
        elif a.apply and legacy is None:
            gc.skip("all rules", "the retired path's run lock is held")
        elif gc.any_live():
            gc.skip("all rules", "a run or guardian is live, or a launched attempt is unresolved")
        else:
            processes = in_use()
            gc.attempts(pinned_attempts(data), processes)
            gc.litter(processes)
            gc.scratch(processes)
            gc.venvs(os.path.realpath(a.cache), processes)
            gc.http_cache(processes)
            gc.receipts()
    finally:
        for fd in gc.root_fds.values():
            os.close(fd)
        if legacy:
            legacy.close()
        if fence:
            fence.close()
    report = dict(at=gc.now, applied=a.apply, unresolved=getattr(gc, "unresolved", [])[:20],
                  removed={k: dict(entries=v[0], files=v[1], bytes=v[2]) for k, v in gc.removed.items()},
                  skipped=gc.skipped)
    if a.stats:
        report["areas"] = gc.stats()
        report["summary"] = "%d files, %.1f GB" % (sum(x["files"] for x in report["areas"].values()),
                                                   sum(x["bytes"] for x in report["areas"].values()) / 1e9)
    report["seconds"] = round(time.monotonic() - started)
    a.data = data
    os.makedirs(os.path.join(a.data, "gc"), exist_ok=True)
    with open(os.path.join(a.data, "gc", "last.json.tmp"), "w") as f:
        json.dump(report, f, indent=1)
    os.replace(os.path.join(a.data, "gc", "last.json.tmp"), os.path.join(a.data, "gc", "last.json"))
    verb = "removed" if a.apply else "would remove"
    for rule, r in report["removed"].items():
        print("%s %-18s %6d entries %9d files %8.2f GB" % (verb, rule, r["entries"], r["files"], r["bytes"] / 1e9))
    for rule, why in report["skipped"].items():
        print("kept   %-18s %s" % (rule, why))
    if a.stats:
        print("store now:", report["summary"])


if __name__ == "__main__":
    main()
