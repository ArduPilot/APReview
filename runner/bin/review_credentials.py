"""Credential leases for wrapper probes and cleanup, shared with guardians."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import time

from review_guardian import alive, identity
from review_lock import acquire, adopt


@contextmanager
def lease(tool, directory):
    data = os.environ.get("REVIEW_DATA")
    if not data:
        # Library readers on development machines do not participate in a
        # runner. Every deployed shell entry point exports REVIEW_DATA.
        yield
        return
    path = Path(data) / "locks"
    path.parent.mkdir(parents=True, exist_ok=True)
    key = "account:" + tool + "/" + str(Path(directory).resolve())
    inherited = 10 if tool == "claude" else 11
    duplicate = None
    try:
        duplicate = os.dup(inherited)
        lock = adopt(path, key, duplicate)
    except (OSError, ValueError):
        if duplicate is not None:
            os.close(duplicate)
        lock = acquire(path, key, time.monotonic() + 5)
    if lock is None:
        raise TimeoutError("account credential lease busy")
    with lock:
        yield


def cleanup(directory):
    directory = Path(directory).resolve()
    native = directory / ".oauth_refresh.lock"
    if not native.exists():
        return
    with lease("claude", directory):
        # Older clients have no OFD lease. A verified live native user still
        # vetoes cleanup until the pre-canary drain has retired those clients.
        for process in Path("/proc").iterdir():
            if not process.name.isdecimal():
                continue
            try:
                record = identity(int(process.name))
                if (process / "comm").read_text().strip() != "claude":
                    continue
                env = dict(pair.split(b"=", 1) for pair in (process / "environ").read_bytes().split(b"\0") if b"=" in pair)
                home = env.get(b"CLAUDE_CONFIG_DIR") or env.get(b"HOME", b"") + b"/.claude"
                if Path(os.fsdecode(home)).resolve() == directory and alive(record):
                    return
            except (OSError, ValueError):
                continue
        if native.is_symlink() or native.is_file():
            native.unlink(missing_ok=True)
        elif native.exists():
            shutil.rmtree(native)
        print("cleared a stale OAuth refresh lock in " + str(directory))
