#!/usr/bin/env python3
"""What quota each account has left, as the tools themselves report it.

Observation only. Nothing here selects an account or changes a run: this exists
so the readings can be checked against reality before anything depends on them.

    quota.py                 a table of every account
    quota.py --json          the same as records
    quota.py --record        append them to $REVIEW_LOGS/quota.jsonl
    quota.py claude          one tool only

Both CLIs publish structured figures, which is worth saying because the runner
still reads Claude's by regex over English prose:

  claude -p /usage --output-format stream-json      usage_report.rate_limits
  codex app-server, account/rateLimits/read          rateLimits.primary/secondary

Neither costs inference - the Claude reply reports total_cost_usd 0 and no model
usage - but both start the CLI, which can trigger an OAuth token refresh. See
the note on the refresh lock in docs/review-box.md.
"""
import datetime
import glob
import json
import os
import subprocess
import sys

HOME = os.path.expanduser("~")
AUTH = os.environ.get("REVIEW_AUTH") or os.path.join(HOME, "review.auth")
TOOLS = ("claude", "codex")
TIMEOUT = 120

# How long each named window is. The CLIs report a reset time but not always a
# duration, and the duration is what says whether a window is the short one.
WINDOW_MINUTES = {"session": 300, "weekly_all": 10080, "weekly_scoped": 10080}


def account_dirs(tool):
    """Every directory a role could select for this tool, the tool's own first.

    By real path, so the role links resolve onto the accounts they point at
    rather than counting them twice, and so does a ~/.claude that is itself a
    symlink into the auth root.
    """
    dirs, seen = [], set()
    for d in [os.path.join(HOME, "." + tool)] + \
             sorted(glob.glob(os.path.join(glob.escape(AUTH), tool + "-*"))):
        if not os.path.isdir(d):
            continue
        real = os.path.realpath(d)
        if real in seen:
            continue
        seen.add(real)
        dirs.append(real)
    return dirs


def _run(cmd, env):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT,
                          env=dict(os.environ, **env))


def _env_for(tool, directory):
    """The variable that points the CLI at this account, and nothing inherited.

    The same rule the runner applies: setting CLAUDE_CONFIG_DIR to the tool's
    own directory is not a no-op - the CLI then reports no address at all - so
    that case leaves it unset.
    """
    var = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}[tool]
    own = os.path.join(HOME, "." + tool)
    env = {}
    if os.path.realpath(directory) == os.path.realpath(own) and not os.path.islink(own):
        env[var] = ""          # cleared below
    else:
        env[var] = directory
    return var, env


def read_claude(directory):
    """usage_report on the stream, rather than the prose /usage prints."""
    var, env = _env_for("claude", directory)
    if not env[var]:
        env.pop(var)
    cmd = ["claude", "-p", "/usage", "--output-format", "stream-json", "--verbose"]
    out = _run(cmd, env)
    if out.returncode != 0:
        return {"error": "claude exited %d" % out.returncode}
    for line in out.stdout.splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        report = d.get("usage_report")
        if not isinstance(report, dict):
            continue
        limits = (report.get("rate_limits") or {}).get("limits")
        if not isinstance(limits, list):
            continue
        windows = []
        for lim in limits:
            if not isinstance(lim, dict) or not isinstance(lim.get("percent"), int):
                continue
            kind = lim.get("kind") or "unknown"
            windows.append({
                "kind": kind,
                "minutes": WINDOW_MINUTES.get(kind),
                "used_pct": lim["percent"],
                "resets_at": lim.get("resets_at"),
                # a per-model window is a real limit but not the one that says
                # whether ordinary work can start
                "scoped": bool(lim.get("scope")),
            })
        if windows:
            return {"windows": windows, "source": "usage_report"}
    return {"error": "no usage_report in the reply"}


def read_codex(directory):
    """account/rateLimits/read over the app-server, rather than rollout files."""
    var, env = _env_for("codex", directory)
    if not env[var]:
        env.pop(var)
    req = "\n".join(json.dumps(m) for m in (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"clientInfo": {"name": "review-quota", "title": "review-quota",
                                   "version": "0"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}},
    )) + "\n"
    # The app-server shuts down on stdin EOF, so it has to stay open until the
    # answer arrives - writing the requests and closing loses the reply.
    proc = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1, env=dict(os.environ, **env))
    lines = []
    try:
        proc.stdin.write(req)
        proc.stdin.flush()
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=TIMEOUT)
        while datetime.datetime.now() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
            if '"id":2' in line or '"id": 2' in line:
                break
    finally:
        proc.kill()
        proc.wait(timeout=10)
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("id") != 2:
            continue
        if "error" in d:
            return {"error": str(d["error"])[:200]}
        result = d.get("result") or {}
        rl = result.get("rateLimits") or {}
        windows = []
        for slot in ("primary", "secondary"):
            s = rl.get(slot)
            if not isinstance(s, dict) or not isinstance(s.get("usedPercent"), (int, float)):
                continue
            mins = s.get("windowDurationMins")
            resets = s.get("resetsAt")
            windows.append({
                "kind": slot,
                "minutes": mins,
                "used_pct": float(s["usedPercent"]),
                "resets_at": (datetime.datetime.fromtimestamp(
                    resets, datetime.timezone.utc).isoformat() if resets else None),
                "scoped": False,
            })
        rec = {"windows": windows, "source": "app-server"}
        # what the account may do beyond the included allowance, which decides
        # whether "out of quota" means "stops" or "starts costing money"
        credits = rl.get("credits") or {}
        rec["ordinary_usage_allowed"] = result.get("ordinaryUsageAllowed")
        rec["has_credits"] = credits.get("hasCredits")
        rec["plan"] = rl.get("planType")
        if not windows:
            rec["error"] = "no rate limit windows reported"
        return rec
    return {"error": "no answer from the app-server"}


READERS = {"claude": read_claude, "codex": read_codex}


def identity(tool, directory):
    """Which account this directory is, so a reading can be attributed."""
    try:
        if tool == "claude":
            acct = json.load(open(os.path.join(directory, ".claude.json"))) \
                .get("oauthAccount") or {}
            return acct.get("emailAddress")
        tok = (json.load(open(os.path.join(directory, "auth.json"))) or {}).get("tokens") or {}
        return tok.get("account_id")
    except Exception:
        return None


def free_pct(windows):
    """Least remaining across the windows that gate ordinary work.

    Per-model windows are recorded but not counted: they say a particular model
    is spent, not that the account cannot start.
    """
    usable = [w for w in windows if not w.get("scoped") and w.get("used_pct") is not None]
    if not usable:
        return None
    return round(100.0 - max(w["used_pct"] for w in usable), 1)


def read(tool, directory):
    rec = {"at": datetime.datetime.now().astimezone().isoformat(),
           "tool": tool, "dir": directory, "account": identity(tool, directory)}
    try:
        rec.update(READERS[tool](directory))
    except subprocess.TimeoutExpired:
        rec["error"] = "timed out after %ds" % TIMEOUT
    except Exception as e:                       # observation must not raise
        rec["error"] = "%s: %s" % (type(e).__name__, e)
    rec.setdefault("windows", [])
    rec["free_pct"] = free_pct(rec["windows"])
    return rec


def recorded(logs=None):
    """The newest recorded reading for each account, by (tool, real path).

    From the file quota.py --record writes. Callers that need a figure without
    starting a CLI read this: the dashboard is rebuilt every ten minutes and a
    run starts on a schedule, and asking an account directly is what makes two
    processes contend for its OAuth refresh.
    """
    path = os.path.join(logs or os.environ.get("REVIEW_LOGS") or
                        os.path.join(HOME, "review", "logs"), "quota.jsonl")
    try:
        with open(path, errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return {}
    newest = {}
    for line in reversed(lines):              # newest first
        try:
            rec = json.loads(line)
        except Exception:
            continue
        key = (rec.get("tool"), os.path.realpath(rec.get("dir") or ""))
        if key[0] and key not in newest:
            newest[key] = rec
    return newest


def main(argv):
    want = [a for a in argv if not a.startswith("-")] or list(TOOLS)
    as_json = "--json" in argv
    record = "--record" in argv
    bad = [t for t in want if t not in TOOLS]
    if bad:
        print("unknown tool: %s" % ", ".join(bad), file=sys.stderr)
        return 2
    records = [read(t, d) for t in want for d in account_dirs(t)]
    if as_json:
        print(json.dumps(records, indent=2))
    else:
        for r in records:
            windows = " ".join(
                "%s=%s%%%s" % (w["kind"], w["used_pct"], "*" if w.get("scoped") else "")
                for w in r["windows"]) or "-"
            print("%-7s %-34s %-26s free=%-6s %s%s" % (
                r["tool"], os.path.basename(r["dir"]), r["account"] or "-",
                "-" if r["free_pct"] is None else "%.1f%%" % r["free_pct"],
                windows, "  ERROR: " + r["error"] if r.get("error") else ""))
    if record:
        logs = os.environ.get("REVIEW_LOGS") or os.path.join(HOME, "review", "logs")
        os.makedirs(logs, exist_ok=True)
        with open(os.path.join(logs, "quota.jsonl"), "a") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
