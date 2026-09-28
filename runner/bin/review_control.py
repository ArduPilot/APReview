"""Local lifecycle operations. No path-based process discovery or GitHub writes."""
import os
from pathlib import Path
import signal
import time

from review_guardian import alive, cleanup_attempt
from review_store import atomic, read


def signal_identity(record, sig):
    if not alive(record):
        return False
    try:
        fd = os.pidfd_open(record["pid"])
        try:
            # Check again after opening: reuse between the first check and open
            # must not signal the new occupant of that PID.
            if not alive(record):
                return False
            signal.pidfd_send_signal(fd, sig)
            return True
        finally:
            os.close(fd)
    except ProcessLookupError:
        return False


def abort(directory, grace=5):
    directory = Path(directory).resolve()
    if read(directory / "run.json", {}).get("schema") != 1:
        raise ValueError("not a supervisor run")
    request = directory / "abort.json"
    if not request.exists():
        atomic(request, {"schema": 1, "requested": time.time(), "run": str(directory)})
    records = [directory / "controller.json", directory / "summary.json", *directory.glob("attempts/*/status.json")]
    for path in records:
        signal_identity(read(path, {}), signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(alive(read(p, {})) for p in records):
        time.sleep(0.05)
    # Only recorded identities from this run may be escalated. Killing a
    # systemd guardian triggers its unit's payload cleanup; plain managers are
    # reconciled through the same recorded attempt registry as the scheduler.
    for path in records:
        signal_identity(read(path, {}), signal.SIGKILL)
    return reap(directory, seconds=5)


def reap(root, seconds=30):
    deadline = time.monotonic() + seconds
    paths = sorted(Path(root).glob("attempts/*/launch.json"))
    paths += sorted(Path(root).glob("runs/*/attempts/*/launch.json"))
    blocked = []
    for record in paths:
        if time.monotonic() >= deadline:
            blocked.append(str(record.parent))
            break
        status = read(record.parent / "status.json", {})
        # Absence of a heartbeat proves nothing. The launch record protects
        # the small interval before the guardian writes its first status.
        witness = status or read(record)
        if not witness.get("boot") or alive(witness):
            continue
        if not cleanup_attempt(record.parent, min(deadline, time.monotonic() + 5)):
            blocked.append(str(record.parent))
    return blocked
