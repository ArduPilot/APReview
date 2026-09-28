#!/usr/bin/env python3
"""One finite outbox drain; no outer board lock."""

import argparse
from review_store import Store, read
from review_github import GitHub
from review_delivery import Delivery

p = argparse.ArgumentParser()
p.add_argument("--data", required=True)
p.add_argument("--config", required=True)
a = p.parse_args()
c = read(a.config)
s = Store(a.data)
g = GitHub(
    c.get("github_recordings"),
    c.get("github_mode", "live"),
    c.get("github_accounts"),
    writes=c.get("github_writes", False),
)
debts = s.drain(Delivery(s, g, c))
raise SystemExit(1 if debts else 0)
