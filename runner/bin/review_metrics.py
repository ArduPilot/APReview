"""Per-process operation counts by cost tier, appended to the store's
metrics log so a baseline can be read back per run and process.

Tiers, most expensive first: inference, github, site (the publishing host),
local. Nothing is written unless a data directory is known."""

import atexit
import fcntl
import json
import os
import re
import threading
import time

_lock = threading.Lock()
_counts = {}
_context = {}
_local = threading.local()


class scope:
    """Count the enclosed work separately, as "<name>:<tier>/<what>": a
    controller's own delivery pass is executor work, not its run's."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.outer = getattr(_local, "scope", None)
        _local.scope = self.name

    def __exit__(self, *exc):
        _local.scope = self.outer


def context(**fields):
    _context.update(fields)


def count(tier, what, seconds=0.0, n=1):
    scope = getattr(_local, "scope", None)
    with _lock:
        c = _counts.setdefault((scope + ":" if scope else "") + tier + "/" + what, [0, 0.0])
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


def github_class(endpoint, method="GET", query=""):
    """An endpoint with its numbers and hashes folded, so calls group by kind."""
    path = endpoint.split("?", 1)[0]
    if path == "graphql":
        return "graphql " + ("mutation" if query.lstrip().startswith("mutation") else "query")
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
        taken = {k: list(v) for k, v in _counts.items() if v[0]}
        _counts.clear()
    counts = {k: [v[0], round(v[1], 3)] for k, v in taken.items()}
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
            # one writer at a time, so a fragment's repair newline cannot land
            # after another process's whole record and swallow it
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = line.encode()
            written = os.write(fd, data)
            if written != len(data):
                # end the fragment so the next record still parses, and keep
                # these counts for the next flush
                os.write(fd, b"\n")
                raise OSError("short metrics write")
        finally:
            os.close(fd)
    except OSError:
        # metrics never fail the work; keep the counts for the next flush
        with _lock:
            for k, (n, s) in taken.items():
                c = _counts.setdefault(k, [0, 0.0])
                c[0] += n
                c[1] += s


atexit.register(flush)
