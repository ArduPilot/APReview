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
import json
import os
from pathlib import Path
import shutil
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_guardian import alive  # noqa: E402
from review_schema import FILES  # noqa: E402

# what the store and its runtime own at its top level
OWNED = {
    "runs", "receipts", "outbox", "operations", "membership", "results", "pages", "landing",
    "owners", "metrics", "gc", "references", "mirror", "locks", "observation.json",
    "drain-recovery.json", "recovery.json", "runs.html", "tmp", "scratch",
    "held", "handoff", "legacy-facts.json", "quota.json", "stub-deliveries",
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
    def __init__(self, data, apply, legacy):
        self.data, self.apply, self.legacy = Path(data), apply, legacy
        self.now = time.time()
        self.removed = {}
        self.skipped = {}

    def remove(self, rule, path):
        files, size = tally(path) if path.is_dir() else (1, path.lstat().st_size)
        r = self.removed.setdefault(rule, [0, 0, 0])
        r[0] += 1
        r[1] += files
        r[2] += size
        if self.apply:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    path.unlink()
                except OSError:
                    pass

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
        return False

    def attempts(self, pins, processes):
        for run in sorted(self.data.glob("runs/*")):
            if not run.is_dir() or run.is_symlink():
                continue
            if not run_finished(run, self.now):
                self.skip("attempt evidence", "run live")
                continue
            created = load(run / "run.json", {}).get("created") or run.stat().st_mtime
            old = self.now - created > OLD_RUN_DAYS * 86400
            if (self.now - created > GONE_RUN_DAYS * 86400 and not used(run, processes)
                    and not any(p.startswith(os.path.realpath(run) + "/") for p in pins)):
                self.remove("expired run", run)
                continue
            for attempt in sorted(run.glob("attempts/*")):
                if not attempt.is_dir() or attempt.is_symlink():
                    continue            # agents write stray files here too
                if os.path.realpath(attempt) in pins:
                    self.skip("attempt evidence", "named by a claim")
                    continue
                if live(attempt / "status.json") or live(attempt / "manager.json"):
                    self.skip("attempt evidence", "guardian alive")
                    continue
                if (attempt / "wt").exists():
                    # a worktree is removed through git, by the guardian's cleanup
                    self.skip("attempt evidence", "worktree present")
                    continue
                if newer_than(attempt, self.now - EVIDENCE_DAYS * 86400) or used(attempt, processes):
                    self.skip("attempt evidence", "recent")
                    continue
                keep = PROOFS if old else KEEP
                for entry in attempt.iterdir():
                    if entry.name not in keep:
                        self.remove("old run" if old else "attempt evidence", entry)

    def litter(self, processes):
        cutoff = self.now - LITTER_DAYS * 86400
        for entry in sorted(self.data.iterdir()):
            name = entry.name
            # store state is JSON at the top; an unknown one is kept, not guessed at
            if name in OWNED or name.endswith(".json"):
                continue
            if name.startswith(LEGACY):
                if self.legacy and not used(entry, processes):
                    self.remove("legacy work dirs", entry)
                else:
                    self.skip("legacy work dirs", "needs --legacy" if not self.legacy else "in use")
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
            if used(entry, processes):
                self.skip("venvs", "in use")
            elif newer_than(entry, cutoff):
                self.skip("venvs", "recent")
            else:
                self.remove("venvs", entry)

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
    a = p.parse_args()
    data = os.path.realpath(a.data)
    gc = GC(data, a.apply, a.legacy)
    started = time.monotonic()
    processes = in_use()
    gc.attempts(pinned_attempts(data), processes)
    if gc.any_live():
        for rule in ("litter", "tmp", "scratch", "venvs", "legacy work dirs"):
            gc.skip(rule, "a run or guardian is live")
    else:
        gc.litter(processes)
        gc.scratch(processes)
        gc.venvs(os.path.realpath(a.cache), processes)
    report = dict(at=gc.now, applied=a.apply,
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
