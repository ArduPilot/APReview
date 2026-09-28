"""Small real on-disk fixtures shared by the deterministic-core tests."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
from review_lock import try_lock
from review_schema import canned
from review_store import Store, atomic, digest, mkdir, read

PR = "pr:owner/repo#1"


def workspace(case):
    Path("/data/review/supervisor-tests").mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="core-", dir="/data/review/supervisor-tests"))
    case.addCleanup(shutil.rmtree, root, True)
    return root


def until(case, predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    case.fail("bounded wait expired")


def stop(child):
    if child.poll() is None:
        child.kill()
    child.wait(timeout=5)


def candidate(number=1, **changes):
    return dict(repository="owner/repo", number=number, node_id="node-%d" % number,
                head="a" * 40, base="b" * 40, merge_base="c" * 40,
                created_at="2020-01-01", **changes)


def complete_claim(store, lock, run="run", request="request", inputs=None):
    from review_schema import FILES
    inputs = inputs or candidate()
    claim = store.allocate(lock, PR, run, request, inputs)
    results = {}
    for kind, filename in FILES.items():
        path = store.root / "attempt-fixtures" / str(claim["generation"]) / kind
        mkdir(path)
        job = {k: inputs[k] for k in ("repository", "number", "node_id", "head", "base", "merge_base")}
        job.update(schema=1, run=run, job=kind, attempt=str(path), generation=claim["generation"],
                   kind=kind, input_digest=digest(inputs), primary_ids=[], finding_ids=[])
        if kind == "validation":
            job["primary_result"] = results["primary"]
        elif kind == "reconciliation":
            job["results"] = dict(results)
        atomic(path / "job.json", job)
        results[kind] = canned(job)
        atomic(path / filename, results[kind])
        atomic(path / "status.json", dict(job, state="terminal", exit=0, timed_out=False,
                                          aborted=False, result_status="complete", empty=True))
        claim["attempts"].append(str(path))
        claim["selected"][kind] = str(path)
    store.save_claim(lock, PR, claim)
    return claim


def python(code, *args, **kwargs):
    env = dict(os.environ, PYTHONPATH=str(BIN), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.Popen([sys.executable, "-c", code, *map(str, args)], env=env, **kwargs)
