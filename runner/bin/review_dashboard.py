"""Supervisor dashboard data: process identity and session-attributed usage."""
from collections import Counter
from html import escape
import json
from pathlib import Path

from review_guardian import alive, proc
from review_store import read


from review_usage import sessions


def identity_readable(record):
    try:
        if not all(k in record for k in ("boot", "pid", "start")):
            return False
        proc(record["pid"])
    except (FileNotFoundError, ProcessLookupError):
        pass  # the recorded process is demonstrably absent
    except (OSError, TypeError, ValueError):
        return False
    return True


def summaries(data):
    out, seen = [], set()
    for path in sorted(Path(data).glob("runs/*/summary.json")):
        summary = read(path)
        if summary.get("schema") != 1:
            continue
        attempts, totals = [], Counter()
        for status_path in sorted(path.parent.glob("attempts/*/status.json")):
            status = read(status_path)
            if status.get("schema") != 1:
                continue
            usage = status.get("sessions") or sessions(status_path.parent / "payload.log")
            if not usage and status.get("session_id") and status.get("usage"):
                usage = {status["session_id"]: status["usage"]}
            for sid, tokens in usage.items():
                identity = (status.get("provider"), status.get("account"), sid)
                if identity not in seen:
                    seen.add(identity)
                    totals.update(tokens)
            try:
                context = read(status_path.parent / "context.json")
            except (OSError, ValueError):
                context = None
            attempts.append(dict(status, liveness="live" if alive(status) else "dead",
                                 sessions=usage, context=context))
            if not identity_readable(status):
                attempts[-1]["liveness"] = "unknown"
        # A summary written before 2026-10-11 carries the phase snapshots, most
        # of its size, and no reader of these rows wants them: over a month of
        # runs they were gigabytes held at once.
        summary.pop("phases", None)
        out.append(dict(summary, name=path.parent.name,
                        liveness="live" if alive(summary) else "dead",
                        counts=dict(Counter(s.get("review", "unknown") for s in summary.get("prs", {}).values())),
                        attempts=attempts, usage=dict(totals)))
        if not identity_readable(summary):
            out[-1]["liveness"] = "unknown"
    return out


def render(rows):
    out = '<h2>Supervisor runs</h2><table><tr><th>Run</th><th>Controller</th><th>PR states</th><th>Delivery debt</th><th>Session usage</th><th>Attempts</th></tr>'
    for row in rows:
        attempts = []
        for attempt in row["attempts"]:
            attempts.append(" / ".join(str(attempt.get(k, "unknown")) for k in
                                       ("attempt", "provider", "state", "liveness", "heartbeat"))
                            + " sessions=" + ",".join(attempt["sessions"]))
        values = [row["name"], row["state"] + ": " + row["liveness"],
                  json.dumps(row["counts"], sort_keys=True), len(row.get("delivery_deferred", [])),
                  json.dumps(row["usage"], sort_keys=True), "\n".join(attempts)]
        out += "<tr>" + "".join("<td><pre>" + escape(str(v)) + "</pre></td>" for v in values) + "</tr>"
    return out + "</table>"
