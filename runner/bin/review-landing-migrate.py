#!/usr/bin/env python3
"""Backfill the landing index from the receipts once, then mark the cutover.

Every date a dated label publish receipt names gets an entry per label in
landing/<endpoint>/<date>/, at the newest revision any receipt or existing
entry holds, and is marked complete. Then landing/<endpoint>/.migrated is
written for each endpoint and landing/.migrated last: from then on every label
upload writes its own entry, so the landing render reads only the index and
receipts need no anchors.

It holds the collector lock (compaction and slimming cannot move a receipt
mid-scan), the maintenance fence exclusively (no controller is running, so
none delivers, nor review-retire.py) and drain.lock (no cron drain): every
writer of label publish receipts and index entries is excluded. (Handoff
writes only "imported" receipts, which this ignores.) Each write is atomic and the markers come last, so it is safe to
rerun after a crash. --dry-run takes no locks and writes nothing.
"""
import argparse
import fcntl
import json
import os
import re
import sys
import time

from review_lock import acquire, canonical
from review_store import Store, atomic, read

LABEL = re.compile(r"page:([^/]+)/DevCallReviews/(\d{4}[-_]\d{2}[-_]\d{2})/([^/]+)/devcall_pr_reviews.html")
SHOWN = ("published", "superseded")

p = argparse.ArgumentParser()
p.add_argument("--data", required=True)
p.add_argument("--dry-run", action="store_true")
p.add_argument("--wait", type=float, default=2400, help="seconds to wait for each lock")
a = p.parse_args()
store = Store(a.data)
landing = store.root / "landing"


def flock(path, what):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + a.wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                sys.exit("%s busy for %.0fs" % (what, a.wait))
            time.sleep(1)


held = []
if not a.dry_run:
    # the collector's order (collector, then maintenance); the drain takes
    # nothing else first, so drain.lock can come last
    os.makedirs(store.root / "gc", exist_ok=True)
    held.append(flock(store.root / "gc" / "collector.lock", "the collector lock"))
    fence = acquire(store.locks, "maintenance", time.monotonic() + a.wait)
    if fence is None:
        sys.exit("a controller held the maintenance lock for %.0fs; pause runs first" % a.wait)
    held.append(fence)
    held.append(flock(store.root / "drain.lock", "drain.lock"))


def receipts():
    """Every receipt that may be a dated label publish: files first, then
    ledger rows no file shadows. A file that does not parse ends the run, as
    it may shadow a ledger row."""
    seen = set()
    directory = store.root / "receipts"
    for entry in os.scandir(directory) if directory.is_dir() else ():
        if not entry.name.endswith(".json"):
            continue
        try:
            with open(entry.path, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            continue                    # compacted since (a dry run holds no lock): in the ledger
        seen.add(entry.name[:-5])
        yield entry.name[:-5], raw
    if (store.root / "receipts.db").exists():
        db = store.ledger()
        last = ""
        while True:
            rows = db.execute("SELECT id, body FROM receipt WHERE id > ? ORDER BY id LIMIT 1000",
                              (last,)).fetchall()
            if not rows:
                break
            last = rows[-1][0]
            for ident, body in rows:
                if ident not in seen and "DevCallReviews" in body:
                    yield ident, body


def strings(value):
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def authoritative(entry, target):
    """An index entry as an upload writes it, for this label."""
    return (isinstance(entry, dict) and entry.get("target") == target and entry.get("state") in SHOWN
            and isinstance(entry.get("revision", 0), int) and strings(entry.get("anchors")))


started = time.monotonic()
winners = {}                    # (endpoint, date, label) -> newest shown publish receipt
blocked = {}                    # (endpoint, date) -> why it cannot be completed
scanned = 0
for ident, raw in receipts():
    scanned += 1
    try:
        record = json.loads(raw)
    except ValueError:
        sys.exit("unreadable receipt %s" % ident)
    if not (isinstance(record, dict) and all(isinstance(record.get(k), str) for k in ("id", "kind", "target", "state"))):
        sys.exit("malformed receipt %s" % ident)    # it may shadow a ledger row
    target = record["target"]
    if not target.startswith("page:"):
        continue
    try:
        where = canonical(target)
    except ValueError:
        if "DevCallReviews/" in target:
            sys.exit("damaged target in receipt %s" % ident)
        continue
    match = LABEL.fullmatch(where)
    if not match:
        continue
    endpoint, date, label = match.groups()
    revision = record.get("revision", 0)
    if not (isinstance(revision, int) and ("anchors" not in record or strings(record["anchors"]))):
        blocked[endpoint, date] = "damaged receipt %s for %s" % (ident, label)
        continue
    # projections share the page's target; only an upload (a publish or an
    # annotation, both through Publication.deliver) puts a label up
    if record["kind"] not in ("publish", "annotation") or record["state"] not in SHOWN:
        continue
    key = (endpoint, date, label)
    if key not in winners or revision >= winners[key]["revision"]:
        # review-retire.py's receipt for a page never uploaded has no anchors;
        # an upload's receipt without them cannot say what the page held
        anchors = record.get("anchors", [] if "retired_at" in record else None)
        winners[key] = dict(target=where, state=record["state"], revision=revision, anchors=anchors)

dates = {}
for (endpoint, date, label), page in winners.items():
    dates.setdefault((endpoint, date), {})[label] = page
for key in blocked:
    dates.setdefault(key, {})

writes = marks = fresh = 0
for (endpoint, date), labels in sorted(dates.items()):
    index = landing / endpoint / date
    fresh += not index.is_dir()
    for label, page in sorted(labels.items()):
        try:
            entry = read(index / (label + ".json"))
        except (OSError, ValueError):
            entry = None                # damaged: replaced from the receipt
        if authoritative(entry, page["target"]) and entry.get("revision", 0) >= page["revision"]:
            continue                    # the upload's own entry is as new
        if page["anchors"] is None:
            blocked.setdefault((endpoint, date), "receipt for %s has no anchors" % label)
            continue
        writes += 1
        if not a.dry_run:
            atomic(index / (label + ".json"), page)
    if (endpoint, date) in blocked:
        print("not completing %s/%s: %s" % (endpoint, date, blocked[endpoint, date]), file=sys.stderr)
        continue
    if not (index / ".complete").exists():
        marks += 1
        if not a.dry_run:
            atomic(index / ".complete", True)

endpoints = sorted({endpoint for endpoint, _ in dates})
print("candidates %d label dates %d (%d with no index) entries written %d dates marked %d endpoints %d "
      "blocked %d in %.0fs%s" % (scanned, len(dates), fresh, writes, marks, len(endpoints), len(blocked),
                                 time.monotonic() - started, " (dry run)" if a.dry_run else ""))
if blocked:
    sys.exit("not migrated: %d dates blocked" % len(blocked))
if not a.dry_run:
    # a rerun keeps the first cutover time; the root marker, last, also covers
    # endpoints that had no dated label upload before the cutover
    for endpoint in endpoints:
        if not (landing / endpoint / ".migrated").exists():
            atomic(landing / endpoint / ".migrated", dict(at=time.time()))
    if not (landing / ".migrated").exists():
        atomic(landing / ".migrated", dict(at=time.time()))
    print("migrated")
