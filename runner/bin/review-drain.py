#!/usr/bin/env python3
"""One finite outbox drain; no outer board lock."""

import argparse
from review_store import Store, read
from review_github import GitHub
from review_delivery import Delivery

p = argparse.ArgumentParser()
p.add_argument("--data", required=True)
p.add_argument("--config", help="optional defaults; intents retain their frozen delivery settings")
a = p.parse_args()
c = read(a.config) if a.config else {}
s = Store(a.data)
g = GitHub(
    c.get("github_recordings"),
    c.get("github_mode", "live"),
    c.get("github_accounts"),
    writes=c.get("github_writes", False),
)
# A controller that died between journalling a page operation and fanning
# it out leaves entries waiting on it for ever; recover before draining.
s.recover_slice()
debts = s.drain(Delivery(s, g, c))
raise SystemExit(1 if debts else 0)
