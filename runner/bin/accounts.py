#!/usr/bin/env python3
"""Which account each role runs on, given what the accounts have left.

This decides; it does not act. Nothing here repoints a role link or starts
anything - run-reviewprs.sh asks with --select once it holds the run lock, and
applies the answer after re-checking the directory for itself.

    accounts.py                  what every role would select
    accounts.py --role rsync     one role
    accounts.py --json           the same as records
    accounts.py --record         append the decisions to $REVIEW_LOGS/select.jsonl
    accounts.py --live           read quota now rather than using a recent recording
    accounts.py --select --role R  one role, for a caller: "tool<TAB>name<TAB>dir"
                                   per line on stdout, the walk on stderr, and
                                   exit 3 if any tool has nothing usable

The policy is $REVIEW_AUTH/policy.json - an ordered list of account directory
names per tool and role. Order is priority; the list is also the whole of what
that role may use, which is the part that matters: a role that must not spend
the project's subscription cannot reach it by running out of its own.

    {
      "min_free_pct": 5,
      "fresh_minutes": 15,
      "roles": {
        "default": {"claude": ["claude-ardupilot", "claude-personal"],
                    "codex":  ["codex-work", "codex-personal"]},
        "rsync":   {"claude": ["claude-personal"],
                    "codex":  ["codex-personal"]}
      }
    }

A missing or unreadable policy is refused rather than defaulted: guessing which
account may pay is the one thing this must never do.

Nothing here spends money. An account whose included allowance is gone is passed
over even where the tool would happily carry on against credits: an unattended
run may stop, but it may not start billing.
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quota                                                      # noqa: E402

HOME = quota.HOME
AUTH = quota.AUTH
POLICY = os.path.join(AUTH, "policy.json")
DEFAULT_MIN_FREE = 5.0
DEFAULT_FRESH_MINUTES = 15


NOTHING_USABLE = 3      # a run may defer on this; a policy error it may not


class PolicyError(Exception):
    """The policy cannot be used, so no choice may be made from it."""


def load_policy(path=None):
    path = path or POLICY
    try:
        with open(path) as f:
            p = json.load(f)
    except OSError as e:
        raise PolicyError("cannot read %s: %s" % (path, e))
    except ValueError as e:
        raise PolicyError("%s is not valid JSON: %s" % (path, e))
    if not isinstance(p, dict):
        raise PolicyError("%s must be an object" % path)
    roles = p.get("roles")
    if not isinstance(roles, dict) or not roles:
        raise PolicyError("%s has no roles" % path)
    for role, tools in roles.items():
        if not isinstance(tools, dict) or not tools:
            raise PolicyError("role %s lists no tools" % role)
        for tool, names in tools.items():
            if tool not in quota.TOOLS:
                raise PolicyError("role %s names an unknown tool: %s" % (role, tool))
            if not isinstance(names, list) or not names or \
                    not all(isinstance(n, str) and n for n in names):
                raise PolicyError("role %s, tool %s: expected a list of account names"
                                  % (role, tool))
            if len(set(names)) != len(names):
                raise PolicyError("role %s, tool %s: repeats an account" % (role, tool))
    for key, kind in (("min_free_pct", (int, float)), ("fresh_minutes", int)):
        if key in p and not isinstance(p[key], kind):
            raise PolicyError("%s must be a number" % key)
    return p


def _account_dir(name):
    """The directory an account name means, or None if it is not usable.

    Names are directory names under the auth root, not paths: a policy that
    could name somewhere else would be a way round the containment the runner
    relies on.
    """
    if os.sep in name or name in (os.curdir, os.pardir):
        return None
    path = os.path.join(AUTH, name)
    if not os.path.isdir(path):
        return None
    real = os.path.realpath(path)
    root = os.path.realpath(AUTH)
    own = [os.path.realpath(os.path.join(HOME, "." + t)) for t in quota.TOOLS]
    if not real.startswith(root + os.sep) and real not in own:
        return None
    return real


def _reading(tool, directory, live, fresh_minutes, cache):
    """What this account has left, from a recent recording or by asking."""
    key = (tool, directory)
    if key in cache:
        return cache[key]
    if not live:
        rec = quota.recorded().get(key)
        if rec:
            at = rec.get("at")
            try:
                age = (datetime.datetime.now().astimezone()
                       - datetime.datetime.fromisoformat(at)).total_seconds() / 60.0
            except Exception:
                age = None
            if age is not None and 0 <= age <= fresh_minutes:
                rec = dict(rec, source="recorded %dm ago" % age)
                cache[key] = rec
                return rec
    rec = quota.read(tool, directory)
    rec["source"] = "read now"
    cache[key] = rec
    return rec


def choose(tool, role, policy, live=False, cache=None):
    """The first account in the role's list with quota to spare.

    Returns the decision and the whole walk that produced it: an account
    skipped for the wrong reason is the failure worth being able to see.
    """
    cache = {} if cache is None else cache
    min_free = float(policy.get("min_free_pct", DEFAULT_MIN_FREE))
    fresh = int(policy.get("fresh_minutes", DEFAULT_FRESH_MINUTES))
    names = (policy.get("roles", {}).get(role) or {}).get(tool)
    decision = {"at": datetime.datetime.now().astimezone().isoformat(),
                "tool": tool, "role": role, "min_free_pct": min_free,
                "considered": [], "chosen": None, "reason": None}
    if not names:
        decision["reason"] = "no accounts listed for %s/%s" % (role, tool)
        return decision
    for name in names:
        step = {"account": name}
        directory = _account_dir(name)
        if directory is None:
            step["skipped"] = "no usable directory of that name"
            decision["considered"].append(step)
            continue
        rec = _reading(tool, directory, live, fresh, cache)
        step["source"] = rec.get("source")
        step["free_pct"] = rec.get("free_pct")
        step["identity"] = rec.get("account")
        if rec.get("error"):
            # unknown is not spare: it cannot establish that there is quota,
            # and an unattended run guessing wrong spends the wrong account
            step["skipped"] = "quota unknown: %s" % str(rec["error"])[:80]
        elif rec.get("free_pct") is None:
            step["skipped"] = "quota unknown"
        elif rec.get("ordinary_usage_allowed") is False:
            # the included allowance is gone. Whatever the window says, work
            # from here is paid overage, and these runs may not spend money.
            # Only an explicit False refuses: the field is a Codex one, and a
            # tool that does not report it has no overage to refuse.
            step["skipped"] = "included allowance exhausted, would spend credits"
        elif rec["free_pct"] <= min_free:
            step["skipped"] = "%.1f%% left, at or below %.1f%%" % (rec["free_pct"], min_free)
        else:
            step["chosen"] = True
            decision["considered"].append(step)
            decision["chosen"] = name
            decision["dir"] = directory
            decision["free_pct"] = rec["free_pct"]
            decision["identity"] = rec.get("account")
            decision["reason"] = "%.1f%% left" % rec["free_pct"]
            return decision
        decision["considered"].append(step)
    decision["reason"] = "every account listed for %s/%s is spent or unknown" % (role, tool)
    return decision


def _write(record, decisions):
    if not record:
        return
    logs = os.environ.get("REVIEW_LOGS") or os.path.join(HOME, "review", "logs")
    os.makedirs(logs, exist_ok=True)
    with open(os.path.join(logs, "select.jsonl"), "a") as f:
        for d in decisions:
            f.write(json.dumps(d) + "\n")


def main(argv):
    roles = [a for i, a in enumerate(argv) if argv[i - 1] == "--role"] or None
    as_json, record, live = ("--json" in argv), ("--record" in argv), ("--live" in argv)
    select = "--select" in argv
    if select and (not roles or len(roles) != 1):
        print("--select needs exactly one --role", file=sys.stderr)
        return 2
    path = None
    if "--policy" in argv:
        i = argv.index("--policy")
        path = argv[i + 1] if i + 1 < len(argv) else None
        if not path:
            print("--policy needs a path", file=sys.stderr)
            return 2
    try:
        policy = load_policy(path)
    except PolicyError as e:
        print("FATAL: %s" % e, file=sys.stderr)
        print("       no account may be chosen without one", file=sys.stderr)
        return 1
    wanted = roles or sorted(policy.get("roles", {}))
    unknown = [r for r in wanted if r not in policy.get("roles", {})]
    if unknown:
        print("FATAL: policy has no role %s" % ", ".join(unknown), file=sys.stderr)
        return 1
    cache, decisions = {}, []
    for role in wanted:
        for tool in sorted(policy["roles"][role]):
            decisions.append(choose(tool, role, policy, live=live, cache=cache))
    if select:
        # stdout is for the caller and carries only what it must act on; the
        # walk goes to stderr, where a run log shows why an account was passed
        # over without the caller having to parse it.
        for d in decisions:
            for step in d["considered"]:
                print("  %-7s %-20s %s" % (
                    d["tool"], step["account"],
                    "CHOSEN" if step.get("chosen") else step.get("skipped")),
                    file=sys.stderr)
        short = [d for d in decisions if not d["chosen"]]
        if short:
            for d in short:
                print("no %s account usable for role %s: %s"
                      % (d["tool"], d["role"], d["reason"]), file=sys.stderr)
            _write(record, decisions)
            return NOTHING_USABLE
        for d in decisions:
            print("%s\t%s\t%s" % (d["tool"], d["chosen"], d["dir"]))
        _write(record, decisions)
        return 0
    if as_json:
        print(json.dumps(decisions, indent=2))
    else:
        for d in decisions:
            print("%-8s %-7s -> %s" % (
                d["role"], d["tool"],
                "%s  (%s)" % (d["chosen"], d["reason"]) if d["chosen"]
                else "NOTHING USABLE  (%s)" % d["reason"]))
            for step in d["considered"]:
                if step.get("chosen"):
                    continue
                print("             %-20s %s" % (step["account"], step.get("skipped")))
    _write(record, decisions)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
