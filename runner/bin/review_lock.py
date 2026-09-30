#!/usr/bin/env python3
"""One inode and one open description per lifetime keep ownership in the kernel."""
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import re
import struct
import threading
import time

WIDTH = 32
TABLE = 1 << 20
MAGIC = b"RVW\x01"
FIXED = {"board": 0, "refresh": 1, "pause": 2, "observation": 3, "quota": 4}
POOLS = {"claude": 256, "codex": 512, "heavy": 768}
KINDS = {"pr": 0, "page": 1, "run": 2, "account": 3}
HEADER_KINDS = {name: index for index, name in enumerate(
    ("board", "refresh", "pause", "observation", "quota", "permit:claude",
     "permit:codex", "permit:heavy", "pr", "page", "run", "account"))}
# The first reserved region holds the file-wide layout marker.
LAYOUT = 5 * WIDTH
_owned = []
_mutex = threading.RLock()
_pid = os.getpid()


def canonical(key, aliases=None):
    aliases = aliases or {}
    if key in FIXED:
        return key
    kind, sep, value = key.partition(":")
    if not sep or not value or any(ord(c) < 32 for c in value):
        raise ValueError("invalid lock key")
    if kind == "pr":
        m = re.fullmatch(r"([^/#]+)/([^/#]+)#([0-9]+)", value)
        if not m or int(m[3]) < 1:
            raise ValueError("invalid PR key")
        repo = (m[1] + "/" + m[2]).lower()
        repo = aliases.get(repo, repo).lower()
        if not re.fullmatch(r"[^/#]+/[^/#]+", repo) or any(p in (".", "..") for p in repo.split("/")):
            raise ValueError("invalid repository")
        return "pr:%s#%d" % (repo, int(m[3]))
    if kind == "run":
        return "run:" + os.path.realpath(value)
    if kind == "page":
        endpoint, sep, path = value.partition("/")
        if not sep or not endpoint or not path or path.startswith("/") or any(c in value for c in "?#\\"):
            raise ValueError("invalid page key")
        if ".." in path.split("/"):
            raise ValueError("parent path in page key")
        path = "/".join(p for p in path.split("/") if p and p != ".")
        if not path:
            raise ValueError("empty page path")
        return "page:" + aliases.get(endpoint, endpoint) + "/" + path
    if kind == "account":
        provider, sep, ident = value.partition("/")
        if not sep or provider not in ("claude", "codex", "github") or not ident:
            raise ValueError("invalid account key")
        return "account:" + aliases.get(value, value)
    if kind == "permit":
        provider, sep, slot = value.partition(":")
        if not sep or provider not in POOLS or not slot.isdecimal() or not 0 <= int(slot) < 256:
            raise ValueError("invalid permit key")
        return "permit:%s:%d" % (provider, int(slot))
    raise ValueError("unknown lock kind")


def region(key):
    key = canonical(key)
    if key in FIXED:
        return FIXED[key]
    kind = key.split(":", 1)[0]
    if kind == "permit":
        _, provider, slot = key.split(":")
        return POOLS[provider] + int(slot)
    stripe = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") % TABLE
    return 4096 + KINDS[kind] * TABLE + stripe


def rank(key):
    kind = key.split(":", 1)[0]
    if kind == "permit":
        return 7 if key.startswith("permit:heavy:") else 4
    return {"run": 0, "pr": 1, "pause": 2, "observation": 2,
            "refresh": 3, "account": 5, "quota": 6, "page": 8, "board": 9}[kind]


def start_time(pid):
    return int(Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()[19])


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _flock(fd, number, shared=False):
    # Linux's native struct flock includes tail padding on 64-bit machines.
    data = struct.pack("hhqqi", fcntl.F_RDLCK if shared else fcntl.F_WRLCK,
                       os.SEEK_SET, number * WIDTH, WIDTH, 0)
    fcntl.fcntl(fd, fcntl.F_OFD_SETLK, data)


def _registry():
    global _pid, _owned
    if _pid != os.getpid():
        _pid = os.getpid()
        _owned = [h for h in _owned if not h.closed]
    return _owned


class Lock:
    def __init__(self, path, key, fd, shared=False):
        self.path, self.key, self.fd = str(path), key, fd
        self.region, self.shared, self.closed = region(key), shared, False

    def close(self):
        with _mutex:
            if not self.closed:
                os.close(self.fd)
                self.closed = True
                if self in _registry():
                    _owned.remove(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _order(key):
    owned = [h for h in _registry() if not h.closed]
    r = rank(key)
    if owned and (r == 0 or any(rank(h.key) > r for h in owned)):
        raise RuntimeError("lock order: " + key)
    if r in (5, 8) and any(rank(h.key) == r and h.region >= region(key) for h in owned):
        raise RuntimeError("region order: " + key)


def _layout(fd):
    magic = os.pread(fd, 4, LAYOUT)
    if magic in (b"", b"\0" * 4):
        os.pwrite(fd, MAGIC, LAYOUT)
        os.fsync(fd)
    elif magic != MAGIC:
        raise ValueError("unknown lock layout")


def _pool_offset(provider):
    return LAYOUT + 4 + list(POOLS).index(provider) * 4


def try_lock(path, key, shared=False, _layout_held=False):
    key = canonical(key)
    with _mutex:
        _order(key)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            # Pool resize/claim operations share the initializer gate. Once
            # the layout exists, unrelated run/PR/page regions must not appear
            # busy just because another process is resizing a permit pool.
            # That false contention made an independent supervisor exit 75.
            needs_gate = key.startswith("permit:") or os.pread(fd, 4, LAYOUT) != MAGIC
            if not _layout_held and needs_gate:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                _layout(fd)
                if key.startswith("permit:"):
                    _, provider, slot = key.split(":")
                    size = int.from_bytes(os.pread(fd, 4, _pool_offset(provider)), "big") or 4
                    if int(slot) >= size:
                        raise ValueError("slot outside configured pool")
                _flock(fd, region(key), shared)
            finally:
                if not _layout_held and needs_gate:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            if not shared:
                digest = hashlib.sha256(key.encode()).digest()
                header = struct.pack(">4s4sIQI8s", MAGIC, digest[:4], os.getpid(),
                                     start_time(os.getpid()), HEADER_KINDS[":".join(key.split(":")[:2]) if key.startswith("permit:") else key.split(":")[0]],
                                     bytes.fromhex(boot_id().replace("-", ""))[:8])
                os.pwrite(fd, header, region(key) * WIDTH)
            result = Lock(path, key, fd, shared)
            _owned.append(result)
            return result
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EAGAIN, errno.EACCES):
                return None
            raise
        except BaseException:
            os.close(fd)
            raise


def adopt(path, key, fd):
    """Register a received OFD; probing a duplicate must conflict with its lock."""
    key = canonical(key)
    with _mutex:
        _order(key)
        if os.fstat(fd).st_ino != os.stat(path).st_ino or os.fstat(fd).st_dev != os.stat(path).st_dev:
            raise ValueError("descriptor is not the lock file")
        probe = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        try:
            data = struct.pack("hhqqi", fcntl.F_WRLCK, os.SEEK_SET, region(key) * WIDTH, WIDTH, 0)
            answer = fcntl.fcntl(fd, fcntl.F_OFD_GETLK, data)
            other = fcntl.fcntl(probe, fcntl.F_OFD_GETLK, data)
            if struct.unpack("hhqqi", answer)[0] != fcntl.F_UNLCK or struct.unpack("hhqqi", other)[0] != fcntl.F_WRLCK:
                raise ValueError("descriptor does not own PR region")
        finally:
            os.close(probe)
        os.set_inheritable(fd, False)
        lock = Lock(path, key, fd)
        _owned.append(lock)
        return lock


def account_slot(path, key, cap, skip_finishing_slot=False):
    """One of `cap` session slots on an account (REVIEW_ACCOUNT_SLOTS). Sessions no longer hold the
    account's own key: that is left for the brief holders (quota probes, the
    wrapper's admission, stale refresh-lock cleanup). Several CLI sessions on
    one login refresh their token safely; this only bounds how many."""
    key = canonical(key)
    # highest first: a run capped at one session always finds slot 0
    # slot 0 is kept for the passes that finish a PR, as in the permit pools
    for slot in reversed(range(1 if skip_finishing_slot and int(cap) > 1 else 0, max(1, int(cap)))):
        lock = try_lock(path, "%s#%d" % (key, slot))
        if lock is not None:
            return lock
    return None


def permit(path, provider, size=4, skip_finishing_slot=False):
    """A slot below `size` in the provider's pool. Size is each run's own
    cap, frozen with the run, over one physical pool: the file records the
    largest size declared, which only grows. Growing needs no drain because
    no slot above the old size can be held; runs with different sizes then
    share the pool instead of each waiting for it to empty to resize it.
    Slots are taken highest first, leaving the low ones to smaller runs."""
    if provider not in POOLS or not 1 <= size <= 256:
        raise ValueError("invalid permit pool")
    with _mutex:
        _order("permit:%s:0" % provider)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _layout(fd)
            offset = _pool_offset(provider)
            recorded = int.from_bytes(os.pread(fd, 4, offset), "big")
            if size > recorded:
                os.pwrite(fd, size.to_bytes(4, "big"), offset)
                os.fsync(fd)
            for slot in reversed(range(1 if skip_finishing_slot else 0, size)):
                lock = try_lock(path, "permit:%s:%d" % (provider, slot), _layout_held=True)
                if lock is not None:
                    return lock
            return None
        except OSError as error:
            if error.errno in (errno.EAGAIN, errno.EACCES):
                return None
            raise
        finally:
            os.close(fd)


def acquire(path, key, deadline, shared=False):
    if canonical(key).startswith("pr:"):
        return try_lock(path, key, shared)
    while time.monotonic() < deadline:
        lock = try_lock(path, key, shared)
        if lock is not None:
            return lock
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    return None


def pages(path, keys, deadline):
    """A collision is one page lifetime, unlike two independent PR claims."""
    from contextlib import ExitStack
    stack = ExitStack()
    try:
        unique = {region(key): canonical(key) for key in keys}
        for offset in sorted(unique):
            lock = acquire(path, unique[offset], deadline)
            if lock is None:
                raise TimeoutError("page busy")
            stack.enter_context(lock)
        return stack
    except BaseException:
        stack.close()
        raise
