"""Per-process operation counts by cost tier, appended to the store's
metrics log so a baseline can be read back per run and process.

Tiers, most expensive first: inference, github, site (the publishing host),
local. Nothing is written unless a data directory is known."""

import atexit
import json
import os
import re
import threading
import time

_lock = threading.Lock()
_counts = {}
_context = {}


def context(**fields):
    _context.update(fields)


def count(tier, what, seconds=0.0, n=1):
    with _lock:
        c = _counts.setdefault(tier + "/" + what, [0, 0.0])
        c[0] += n
        c[1] += seconds


class timed:
    def __init__(self, tier, what):
        self.key = (tier, what)

    def __enter__(self):
        self.start = time.monotonic()
        return self

    def __exit__(self, *exc):
        count(*self.key, seconds=time.monotonic() - self.start)


def github_class(endpoint, method="GET"):
    """An endpoint with its numbers and hashes folded, so calls group by kind."""
    path = endpoint.split("?", 1)[0]
    if path == "graphql":
        return "graphql"
    if path.startswith("search/"):
        return "search"
    match = re.match(r"repos/[^/]+/[^/]+/(.*)", path)
    rest = match[1] if match else path
    rest = re.sub(r"\b[0-9a-f]{40}\b", "SHA", rest)
    rest = re.sub(r"\b\d+\b", "N", rest)
    return method + " " + rest


def flush():
    data = _context.get("data") or os.environ.get("REVIEW_DATA")
    with _lock:
        counts = {k: [v[0], round(v[1], 3)] for k, v in _counts.items() if v[0]}
        _counts.clear()
    if not data or not counts:
        return
    line = json.dumps(dict(at=time.time(), pid=os.getpid(),
                           process=_context.get("process", "other"),
                           run=_context.get("run"), counts=counts), sort_keys=True) + "\n"
    directory = os.path.join(str(data), "metrics")
    try:
        os.makedirs(directory, exist_ok=True)
        # one O_APPEND write per line keeps concurrent writers' lines whole
        fd = os.open(os.path.join(directory, time.strftime("%Y-%m-%d") + ".jsonl"),
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except OSError:
        pass                            # metrics never fail the work


atexit.register(flush)
