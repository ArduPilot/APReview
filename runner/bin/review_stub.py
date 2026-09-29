#!/usr/bin/env python3
"""Exercise orchestration without invoking an inference provider."""
import os
from pathlib import Path
import sys
import time

from review_schema import FILES, canned
from review_store import atomic, read


def main():
    if os.environ.get("REVIEW_AI_STUB") != "1":
        raise RuntimeError("stub jobs require REVIEW_AI_STUB=1")
    path = Path(os.environ["REVIEW_JOB_DIR"])
    job = read(path / "job.json")
    behavior = job.get("stub", {})
    time.sleep(behavior.get("sleep", 0.03))
    if behavior.get("refuse"):
        # what codex prints when OpenAI's content filter declines the prompt
        print('{"type":"error","message":"This content was flagged for possible cybersecurity risk."}', flush=True)
        return 1
    result = canned(job)
    if behavior.get("invalid"):
        result["unexpected"] = True
    if behavior.get("incomplete"):
        result["status"] = "incomplete"
        result["gaps"] = ["UNCONFIRMED: the stub read only part of the diff"]
    atomic(path / FILES[job["kind"]], result)
    return behavior.get("exit", 0)


if __name__ == "__main__":
    sys.exit(main())
