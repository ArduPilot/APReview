#!/usr/bin/env python3
"""Take a PR off a page it can never reach again.

A page path that stopped existing (a renamed prefix, a missing rsync module)
leaves its publish entries retrying until they exhaust their attempts, and the
comment behind them waits forever because verification repairs the same
intent. Delivery already treats a PR removed from a page as superseded; this
writes that membership row and the superseded receipts delivery would have
written, so rebuild never materializes the intents again and later
generations do not inherit the page.
"""
import argparse
import time

from review_lock import canonical, try_lock
from review_store import Store, read

p = argparse.ArgumentParser()
p.add_argument("--data", required=True)
p.add_argument("--reason", required=True)
p.add_argument("page", help="page:<endpoint>/<path> the PR is leaving")
p.add_argument("prs", nargs="+", help="pr:<owner>/<repo>#<n>")
a = p.parse_args()
store = Store(a.data)
page = canonical(a.page)
busy = 0
for pr in map(canonical, a.prs):
    # The counter first: the drain orders PR before page, and the ticket
    # only has to be newer than the row it replaces.
    ticket = store.ticket()
    pr_lock = try_lock(store.locks, pr)
    if pr_lock is None:
        print(pr, "busy")
        busy += 1
        continue
    with pr_lock:
        page_lock = try_lock(store.locks, page)
        if page_lock is None:
            print(page, "busy")
            busy += 1
            continue
        with page_lock:
            store._merge_membership(
                page_lock, page, {pr: {"ticket": ticket, "removed": True, "unreachable": a.reason}}
            )
            entries = [read(f) for f in (store.root / "outbox").glob("*.json")]
            entries += [
                dict(intent, pr=pr, generation=bundle["generation"])
                for bundle in store.chain(pr)
                for intent in bundle["intents"]
            ]
            done = set()
            for entry in entries:
                if (
                    not entry
                    or entry["pr"] != pr
                    or entry["kind"] not in ("publish", "projection", "annotation")
                    or canonical(entry["target"]) != page
                    or entry["id"] in done
                    or (store.root / "receipts" / (entry["id"] + ".json")).exists()
                ):
                    continue
                store.receipt(
                    entry, "superseded", reason=a.reason, retired_at=time.time(),
                    last_error=entry.get("error"),
                )
                done.add(entry["id"])
                print(pr, entry["kind"], entry["generation"], "superseded")
    print(pr, "removed from", page)
raise SystemExit(1 if busy else 0)
