#!/usr/bin/env python3
"""Give given-up outbox entries a fresh set of tries, once their cause is fixed.

A comment, note or deprecation that fails five times stops for good, and an
earlier comment left sending or uncertain holds every newer one for its PR.
This clears the failure count under the entry's lock. The drain then treats
it as on any retry: an uncertain comment is reconciled against the thread
before any repost, and a superseded one is cancelled.
"""
import argparse
import re

from review_lock import try_lock
from review_store import Store, atomic, read

p = argparse.ArgumentParser()
p.add_argument("--data", required=True)
p.add_argument("ids", nargs="+", help="outbox entry ids, as the runs page lists them")
a = p.parse_args()
store = Store(a.data)
failed = 0
for ident in a.ids:
    if not re.fullmatch("[0-9a-f]{64}", ident):
        print(ident, "not an entry id")
        failed += 1
        continue
    path = store.root / "outbox" / (ident + ".json")
    entry = read(path)
    if not isinstance(entry, dict):
        print(ident, "not in the outbox")
        failed += 1
        continue
    lock = try_lock(store.locks, entry["target"] if entry.get("gate") == "page" else entry["pr"])
    if lock is None:
        print(ident, "busy, try again")
        failed += 1
        continue
    with lock:
        entry = read(path)
        if not isinstance(entry, dict) or store.has_receipt(ident):
            print(ident, "settled meanwhile")
            continue
        was = entry.get("failures", 0)
        entry.update(failures=0, next_attempt=0, error=None)
        atomic(path, entry)
        print(ident, entry["pr"], entry["kind"], entry.get("state"), "failures %s -> 0" % was)
raise SystemExit(1 if failed else 0)
