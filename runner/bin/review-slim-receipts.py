#!/usr/bin/env python3
"""Drop the page copies (Store.PAGE_COPY) from receipts written before
Store.receipt stopped keeping them, in files and the ledger, keeping each
receipt's write time. Safe to rerun, and to run beside the drain: it holds
the collector lock, so compaction cannot move a receipt from under its
rewrite, and the drain never rewrites a receipt.

--vacuum then gives the freed ledger space back to the disk; it needs free
space about the size of the slimmed ledger.
"""
import argparse
import fcntl
import json
import os
import subprocess
import sys
import time

from review_store import Store, encoded, fsync_dir

p = argparse.ArgumentParser()
p.add_argument("--data", required=True)
p.add_argument("--vacuum", action="store_true")
p.add_argument("--dry-run", action="store_true")
p.add_argument("--batch", type=int, default=100, help="ledger rows per transaction")
a = p.parse_args()
if a.batch < 1:
    p.error("--batch must be at least 1")
store = Store(a.data)
drop = Store.PAGE_COPY

os.makedirs(store.root / "gc", exist_ok=True)
collector = open(store.root / "gc" / "collector.lock", "a")
fcntl.flock(collector, fcntl.LOCK_EX)


# The vacuumed pages land in the WAL; only a checkpoint no reader blocks
# shrinks the file, so retry it until one completes.
VACUUM = """
import sqlite3, sys, time
db = sqlite3.connect(sys.argv[1], timeout=600)
db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
db.execute("VACUUM")
deadline = time.monotonic() + 600
while db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
    if time.monotonic() > deadline:
        sys.exit("vacuumed, but readers kept the checkpoint from shrinking the ledger")
    time.sleep(1)
"""


def slim(body):
    record = json.loads(body)
    if not isinstance(record, dict) or not any(k in record for k in drop):
        return None
    return encoded({k: v for k, v in record.items() if k not in drop})


files = saved = 0
directory = store.root / "receipts"
for entry in os.scandir(directory):
    if not entry.name.endswith(".json"):
        continue
    try:
        with open(entry.path, "rb") as f:
            lean = slim(f.read())
        stat = entry.stat()
    except (OSError, ValueError):
        continue                        # not ours to judge
    if lean is None:
        continue
    files += 1
    saved += stat.st_size - len(lean)
    if a.dry_run:
        continue
    temp = directory / ("." + entry.name + ".slim")
    with open(temp, "wb") as stream:
        stream.write(lean)
        stream.flush()
        os.utime(stream.fileno(), ns=(stat.st_mtime_ns, stat.st_mtime_ns))
        os.fsync(stream.fileno())
    os.replace(temp, entry.path)
if files and not a.dry_run:
    fsync_dir(directory)

rows = 0
if (store.root / "receipts.db").exists():
    db = store.ledger()
    last = 0
    while True:
        updates = []
        cursor = db.execute("SELECT rowid, body FROM receipt WHERE rowid > ? ORDER BY rowid LIMIT ?",
                            (last, a.batch))
        seen = 0
        for rowid, body in cursor:
            seen += 1
            last = rowid
            lean = slim(body)
            if lean is not None:
                updates.append((lean.decode("utf-8"), rowid))
                saved += len(body.encode("utf-8")) - len(lean)
        if not seen:
            break
        rows += len(updates)
        if updates and not a.dry_run:
            with db:
                db.executemany("UPDATE receipt SET body = ? WHERE rowid = ?", updates)
    db.close()
    if a.vacuum and not a.dry_run:
        # SQLite reads SQLITE_TMPDIR once, at start: a child set up with it
        # puts the temporary copy beside the ledger, not on a tmpfs /tmp
        start = time.monotonic()
        subprocess.run([sys.executable, "-c", VACUUM, str(store.root / "receipts.db")],
                       env=dict(os.environ, SQLITE_TMPDIR=str(store.root)), check=True)
        print("vacuumed in %.0fs, ledger now %.1f MB" % (time.monotonic() - start,
                                                       (store.root / "receipts.db").stat().st_size / 1e6))
print("ledger %d files %d saved %.1f MB%s" % (rows, files, saved / 1e6, " (dry run)" if a.dry_run else ""))
