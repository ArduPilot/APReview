#!/usr/bin/env python3
"""Baseline of what the review system spends, by cost tier, per run.

Reads only local state: the runs' own records, the metrics log written by
each controller and drain, and the outbox. Inference counts come from the
attempts; GitHub, site and local counts from the metrics log."""

import argparse
from collections import Counter, defaultdict
import glob
import json
import os
import time


def load(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def metrics(data, since):
    """Counts per run for controllers, and per day for everything else."""
    per_run, per_day = defaultdict(Counter), defaultdict(Counter)
    seconds = defaultdict(float)
    for path in sorted(glob.glob(os.path.join(data, "metrics", "*.jsonl"))):
        with open(path) as f:
            for line in f:
                try:
                    x = json.loads(line)
                except ValueError:
                    continue
                if x["at"] < since:
                    continue
                key = ("run", x["run"]) if x.get("run") else ("day", time.strftime("%Y-%m-%d", time.localtime(x["at"])) + " " + x["process"])
                for what, (n, s) in x["counts"].items():
                    (per_run if key[0] == "run" else per_day)[key[1]][what] += n
                    seconds[(key[1], what)] += s
    return per_run, per_day, seconds


def runs(data, since):
    out = []
    for directory in sorted(glob.glob(os.path.join(data, "runs", "*"))):
        run = load(os.path.join(directory, "run.json")) or {}
        created = run.get("created")
        if not created or created < since:
            continue
        state = load(os.path.join(directory, "state.json"), {})
        summary = load(os.path.join(directory, "summary.json"), {})
        reasons = Counter((c.get("reason") or "?") for c in run.get("candidates", [])
                          if state.get(c["pr"], {}).get("review") == "accepted")
        passes, minutes, first = Counter(), Counter(), None
        for job_path in glob.glob(os.path.join(directory, "attempts", "*", "job.json")):
            job = load(job_path, {})
            status = load(os.path.join(os.path.dirname(job_path), "status.json"), {})
            registered = job.get("registered")
            if registered:
                first = registered if first is None else min(first, registered)
            if status.get("session_id") or status.get("sessions") or status.get("exit") is not None:
                passes[job.get("kind", "?")] += 1
                end = status.get("heartbeat")
                if registered and end:
                    minutes[job.get("kind", "?")] += (end - registered) / 60
        out.append(dict(
            name=os.path.basename(directory), mode=run.get("mode", "?"), created=created,
            state=summary.get("state"),
            duration_min=((summary.get("heartbeat") or created) - created) / 60,
            launch_latency_min=(first - created) / 60 if first else None,
            outcomes=dict(Counter(v.get("review") for v in state.values())),
            reviewed_because=dict(reasons), passes=dict(passes),
            inference_min={k: round(v) for k, v in minutes.items()},
        ))
    return out


def outbox(data):
    now, kinds, oldest = time.time(), Counter(), None
    for path in glob.glob(os.path.join(data, "outbox", "*.json")):
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue
        entry = load(path, {})
        kinds[entry.get("kind", "?")] += 1
        oldest = max(oldest or 0, age)
    return dict(entries=sum(kinds.values()), by_kind=dict(kinds),
                oldest_min=round(oldest / 60) if oldest else None)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", default=os.environ.get("REVIEW_DATA", os.path.expanduser("~/review/data")))
    p.add_argument("--days", type=float, default=2)
    p.add_argument("--json", action="store_true")
    p.add_argument("--save", action="store_true", help="also write metrics/baseline-<stamp>.json")
    a = p.parse_args()
    since = time.time() - a.days * 86400
    per_run, per_day, seconds = metrics(a.data, since)
    report = dict(generated=time.time(), days=a.days, runs=runs(a.data, since), outbox=outbox(a.data),
                  storage=load(os.path.join(a.data, "gc", "last.json")))
    for r in report["runs"]:
        calls = per_run.get(r["name"], {})
        r["github_calls"] = dict(sorted(((k[7:], v) for k, v in calls.items() if k.startswith("github/")),
                                        key=lambda kv: -kv[1]))
        r["github_seconds"] = round(sum(s for (n, k), s in seconds.items() if n == r["name"] and k.startswith("github/")))
        r["site"] = {k[5:]: v for k, v in calls.items() if k.startswith("site/")}
    report["other_processes"] = {k: dict(v) for k, v in sorted(per_day.items())}
    if a.save:
        os.makedirs(os.path.join(a.data, "metrics"), exist_ok=True)
        with open(os.path.join(a.data, "metrics", time.strftime("baseline-%Y%m%d_%H%M.json")), "w") as f:
            json.dump(report, f, indent=1)
    if a.json:
        print(json.dumps(report, indent=1))
        return
    print("outbox: %(entries)d entries, oldest %(oldest_min)s min, %(by_kind)s" % report["outbox"])
    if report["storage"]:
        print("storage (last GC):", report["storage"].get("summary"))
    print("%-40s %-8s %6s %7s %-28s %s" % ("run", "mode", "min", "launch", "passes", "github calls"))
    for r in report["runs"]:
        print("%-40s %-8s %6.0f %7s %-28s %d %s" % (
            r["name"][:40], r["mode"], r["duration_min"],
            "%.0fm" % r["launch_latency_min"] if r["launch_latency_min"] is not None else "-",
            ",".join("%s:%d" % kv for kv in sorted(r["passes"].items())) or "-",
            sum(r["github_calls"].values()), ", ".join("%s %d" % kv for kv in list(r["github_calls"].items())[:3])))
    for k, v in report["other_processes"].items():
        print(k, dict(v))


if __name__ == "__main__":
    main()
