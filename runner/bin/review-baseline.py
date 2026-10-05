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
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


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
        summary = load(os.path.join(directory, "summary.json"), {})
        # a run counts if it was active in the window, not only if it began there
        if not created or (summary.get("heartbeat") or created) < since:
            continue
        state = load(os.path.join(directory, "state.json"), {})
        reasons = Counter((c.get("reason") or "?") for c in run.get("candidates", [])
                          if state.get(c["pr"], {}).get("review") == "accepted")
        # A pass counts once its guardian launched (launch.json exists), running
        # or not. Its minutes run from launch to the last heartbeat: attempt
        # time, including permit and account waits, not model time.
        passes, minutes, first, reviewed = Counter(), Counter(), None, set()
        contexts = defaultdict(list)
        for job_path in glob.glob(os.path.join(directory, "attempts", "*", "job.json")):
            job = load(job_path, {})
            attempt = os.path.dirname(job_path)
            # launch.json is written before the guardian starts; a status
            # with a pid is the guardian having actually run
            status = load(os.path.join(attempt, "status.json"), {})
            if not status.get("pid"):
                continue
            try:
                launched = os.path.getmtime(os.path.join(attempt, "launch.json"))
            except OSError:
                continue
            first = launched if first is None else min(first, launched)
            kind = job.get("kind", "?")
            passes[kind] += 1
            if kind == "primary":
                reviewed.add("%s@%s" % (job.get("pr", "?").split(":", 1)[-1], (job.get("head") or "?")[:10]))
            end = status.get("heartbeat")
            if end and end > launched:
                minutes[kind] += (end - launched) / 60
            # over: terminal, or a guardian killed before its final status
            from review_guardian import alive
            try:
                over = status.get("state") == "terminal" or not alive(status)
            except Exception:
                over = False
            if over:
                contexts[kind].append(load(os.path.join(attempt, "context.json")))
        out.append(dict(
            name=os.path.basename(directory), mode=run.get("mode", "?"), created=created,
            state=summary.get("state"),
            duration_min=((summary.get("heartbeat") or created) - created) / 60,
            launch_latency_min=(first - created) / 60 if first else None,
            outcomes=dict(Counter(v.get("review") for v in state.values())),
            reviewed_because=dict(reasons), passes=dict(passes), reviewed=sorted(reviewed),
            attempt_min={k: round(v) for k, v in minutes.items()},
            context={k: context_summary(v) for k, v in contexts.items()},
        ))
    return out


def quantile(values, q):
    values = sorted(values)
    return values[int((len(values) - 1) * q)] if values else None


def context_summary(records):
    """Per pass kind, from each finished pass's context.json. A pass without
    one, or whose record is damaged or failed, counts as unknown, never zero."""
    from review_context import known, readback
    good = [r for r in records if known(r)]
    out = dict(passes=len(records), unknown=len(records) - len(good),
               readback_chars=sum(readback(r) for r in good) if good else None,
               model_time="unknown")    # attempt minutes include permit, build and account waits
    for field in ("requests", "peak_input", "sum_input", "tool_output_chars"):
        for at in (.5, .9, .95):
            out["%s_p%d" % (field, round(at * 100))] = quantile([r[field] for r in good], at)
    return out


def outbox(data):
    """Debt age from each entry's fixed creation time; entries from before
    that field existed fall back to file mtime, which retries rewrite."""
    now, kinds, oldest = time.time(), Counter(), None
    for path in glob.glob(os.path.join(data, "outbox", "*.json")):
        entry = load(path)
        if entry is None:
            continue
        try:
            age = now - (entry.get("created") or os.path.getmtime(path))
        except OSError:
            continue
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
        own = {k: v for k, v in calls.items() if ":" not in k.split("/", 1)[0]}
        r["github_calls"] = dict(sorted(((k[7:], v) for k, v in own.items()
                                         if k.startswith("github/") and k != "github/git fetch"),
                                        key=lambda kv: -kv[1]))
        r["git_fetches"] = own.get("github/git fetch", 0)
        # summed over parallel requests: operation time, not elapsed time
        r["github_op_seconds"] = round(sum(s for (n, k), s in seconds.items()
                                           if n == r["name"] and k.startswith("github/") and k != "github/git fetch"))
        r["github_accounts"] = {k[15:]: v for k, v in own.items() if k.startswith("github-account/")}
        r["site"] = {k[5:]: v for k, v in own.items() if k.startswith("site/")}
        r["local"] = {k[6:]: v for k, v in own.items() if k.startswith("local/")}
        r["inference_launched"] = {k[19:]: v for k, v in own.items() if k.startswith("inference/launched ")}
        # delivery the controller did for the whole store while it ran
        r["executor_drain"] = {k[6:]: v for k, v in calls.items() if k.startswith("drain:")}
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
    print("%-40s %-8s %6s %7s %-28s %s" % ("run", "mode", "min", "1st pass", "passes", "github API calls"))
    for r in report["runs"]:
        print("%-40s %-8s %6.0f %7s %-28s %d %s" % (
            r["name"][:40], r["mode"], r["duration_min"],
            "%.0fm" % r["launch_latency_min"] if r["launch_latency_min"] is not None else "-",
            ",".join("%s:%d" % kv for kv in sorted(r["passes"].items())) or "-",
            sum(r["github_calls"].values()), ", ".join("%s %d" % kv for kv in list(r["github_calls"].items())[:3])))
    print("pass context, per run and kind: passes (unknown) requests p50/p90, peak input p50, "
          "summed input p50/p90, tool output chars p50")
    for r in report["runs"]:
        for kind, c in sorted(r["context"].items()):
            print("  %-40s %-15s %3d (%d) %s/%s %s %s/%s %s" % (
                r["name"][:40], kind, c["passes"], c["unknown"], c["requests_p50"], c["requests_p90"],
                c["peak_input_p50"], c["sum_input_p50"], c["sum_input_p90"], c["tool_output_chars_p50"]))
    for k, v in report["other_processes"].items():
        print(k, dict(v))


if __name__ == "__main__":
    main()
