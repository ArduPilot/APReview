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
    if behavior.get("findings") and job["kind"] in ("primary", "cold"):
        result["findings"] = [dict(f, id="%s:%s" % (job["kind"], f["id"])) for f in behavior["findings"]]
    if job["kind"] == "validation" and job.get("primary_ids"):
        result["outcomes"] = [{"id": i, "outcome": "CONFIRM",
                               "evidence": {"commands": [], "artifacts": [], "configuration": "stub"}}
                              for i in job["primary_ids"]]
    if behavior.get("retain") and job["kind"] == "reconciliation":
        for outcome in result["outcomes"]:
            outcome.update(disposition="retained", actionable=True, rationale="")
        if result["outcomes"]:
            result["verdict"] = "COMMENT"
    if behavior.get("invalid"):
        result["unexpected"] = True
    if behavior.get("incomplete"):
        result["status"] = "incomplete"
        result["gaps"] = ["UNCONFIRMED: the stub read only part of the diff"]
    atomic(path / FILES[job["kind"]], result)
    return behavior.get("exit", 0)


if __name__ == "__main__":
    sys.exit(main())
