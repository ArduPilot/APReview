#!/usr/bin/env python3
"""One finite outbox drain; no outer board lock."""

import argparse
import os
import time
from review_store import Store, read
from review_github import GitHub
from review_delivery import Delivery
import review_metrics

p = argparse.ArgumentParser()
p.add_argument("--data", required=True)
p.add_argument("--config", help="optional defaults; intents retain their frozen delivery settings")
p.add_argument("--budget", type=float, default=240, help="seconds of passes")
a = p.parse_args()
BUDGET = a.budget
c = read(a.config) if a.config else {}
# one drainer at a time: a cron start that finds another running leaves it
# the work rather than contending for every lock it holds
import fcntl
_single = open(os.path.join(a.data, "drain.lock"), "a")
try:
    fcntl.flock(_single, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    raise SystemExit(0)
s = Store(a.data)
review_metrics.context(process="drain", data=a.data)
g = GitHub(
    c.get("github_recordings"),
    c.get("github_mode", "live"),
    c.get("github_accounts"),
    writes=c.get("github_writes", False),
)
# A controller that died between journalling a page operation and fanning
# it out leaves entries waiting on it for ever; recover before draining.
# One pass takes at most a hundred entries, a few seconds' work; an all run
# queues some two thousand, so a single pass per cron slot left comments
# hours behind. Keep passing while entries settle, inside the five minutes.
# Settled, not a shrinking outbox: a running controller adds entries faster
# than one pass removes them, and that stopped the drain after one pass.
adapter = Delivery(s, g, c)
deadline = time.monotonic() + BUDGET
while True:
    if time.monotonic() < deadline - 30:
        # a journal fans out only where its page was free; the rest wait for
        # recovery, which one slice per cron slot reached hours late
        s.recover_slice()
    before = {p.name for p in (s.root / "outbox").glob("*.json")}
    debts = s.drain(adapter, seconds=min(60, max(0, deadline - time.monotonic())))
    if not debts or time.monotonic() >= deadline:
        break
    if before <= {p.name for p in (s.root / "outbox").glob("*.json")}:
        break
raise SystemExit(1 if debts else 0)
