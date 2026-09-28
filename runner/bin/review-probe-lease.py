#!/usr/bin/env python3
"""Run the usage shell with a proved credential lease, including its cleanup."""
import os
import subprocess
import sys
from review_credentials import lease

if sys.argv[1:] == ["--verify"]:
    from pathlib import Path
    from review_lock import adopt
    try:
        home = Path(os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))).resolve()
        lock = adopt(Path(os.environ["REVIEW_DATA"]) / "locks", "account:claude/" + str(home), os.dup(10))
        lock.close()
    except (OSError, ValueError):
        raise SystemExit(1)
    raise SystemExit(0)

with lease("claude", os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))):
    # lease may have adopted the run's fd or opened another. Pass the owned
    # description at the fixed shell descriptor so nested cleanup can prove it.
    from review_lock import _registry
    lock = _registry()[-1]
    if lock.fd != 10:
        os.dup2(lock.fd, 10)
    try:
        result = subprocess.run(["bash", *sys.argv[1:]], env=dict(os.environ, REVIEW_PROBE_LEASED="1"),
                                pass_fds=(10,), timeout=120)
    finally:
        if lock.fd != 10:
            os.close(10)
    raise SystemExit(result.returncode)
