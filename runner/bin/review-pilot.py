#!/usr/bin/env python3
"""A paired quality pilot (step 6 of docs/context-efficiency-plan.md;
procedure and bound in docs/quality-pilot.md).

freeze:    read each PR once, as discovery would, into candidates.json, and
           freeze one configuration for every arm; a PR discovery could not
           read completely is refused, not reviewed.
run:       review the frozen candidates under one presentation, in a store
           of its own, with frozen_inputs: nothing is read from GitHub
           again, and nothing is published, posted or synced. Meant to run
           while production review runs are paused (overnight), so the
           stores share nothing but accounts; production's quota pause
           still applies (quota.json is production's).
overnight: check no production run is going, pause production review runs,
           run every arm together, end the pause, collect.
collect:   check every frozen PR was reviewed to acceptance at its frozen
           head in every arm; pool each arm's kept findings into a blinded
           pack, with the arm hidden behind a key kept apart.
score:     unblind complete, typed verdicts and apply the bound fixed before
           the pilot ran."""

import argparse
import glob
import hashlib
import json
import os
from pathlib import Path
import random
import re
import secrets
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_store import Store, atomic, read  # noqa: E402

BIN = Path(__file__).resolve().parent
ARMS = {"legacy": "legacy", "paging": "v4-paging"}
# the bound fixed before the pilot ran (docs/quality-pilot.md)
BOUND = dict(missed_blockers=0, missed_other=2, false_blockers=2)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def production():
    """The production store, from the environment, never from a command
    line: the hourly reaper ends processes whose arguments name it."""
    data = os.environ.get("REVIEW_DATA")
    if not data:
        raise SystemExit("REVIEW_DATA must name the production store")
    return os.path.realpath(data)


def outside_production(directory):
    """A pilot directory must lie outside the production store: the hourly
    reaper ends any process working inside it, the pause holder included."""
    path, data = os.path.realpath(directory), production()
    # by plain prefix, as the reaper matches: ~/review/data-pilot would be reaped too
    if path.startswith(data):
        raise SystemExit("%s is inside the production store %s, or shares its path prefix" % (path, data))
    return Path(path)


def production_configuration(data):
    """The newest production run's configuration, to freeze for the pilot."""
    runs = sorted(glob.glob(os.path.join(data, "runs", "*", "configuration.json")), key=os.path.getmtime)
    if not runs:
        raise SystemExit("no production run configuration to copy")
    return read(runs[-1])


def freeze(args):
    from review_discovery import Discovery
    from review_github import GitHub
    import review_metrics
    directory = outside_production(args.out)
    # a refused freeze leaves only its metrics: that directory may be retried
    if directory.exists() and any(p.name != "metrics" for p in directory.iterdir()):
        raise SystemExit("%s is not empty: a pilot directory is never reused" % directory)
    directory.mkdir(parents=True, exist_ok=True)
    # discovery's metrics go to the pilot, never production's log
    review_metrics.context(process="pilot-freeze", data=str(directory.resolve()))
    config = production_configuration(production())
    github = GitHub(None, "live", config.get("github_accounts"), writes=False)
    discovery = Discovery(github, config, Store(production()))
    out, refused = [], []
    for pr in args.prs:
        candidate = discovery.candidate(pr, "pr")
        # discovery marks a PR it could not read completely (no diff, a
        # failed fetch) as something other than a review: never bless it
        if candidate.get("classification") != "REVIEW" or not isinstance(candidate.get("diff"), str):
            refused.append((pr, candidate.get("classification"), candidate.get("reason")))
            continue
        candidate.update(reason="pilot", post=False, held=True, live={}, destinations=[], membership_removed={})
        candidate.pop("configuration", None)
        out.append(candidate)
        print("froze %s at %s (%d diff lines)" % (candidate["pr"], candidate["head"][:10],
                                                  len(candidate["diff"].splitlines())))
    if refused:
        raise SystemExit("not frozen, discovery could not read them completely: %s" % refused)
    directory.mkdir(parents=True, exist_ok=True)
    for key in ("prompts", "presentation", "date", "stamp", "routing", "routing_root"):
        config.pop(key, None)
    atomic(directory / "configuration-base.json", config)
    atomic(directory / "candidates.json", out)
    atomic(directory / "frozen.json", dict(at=time.time(), prs=[c["pr"] for c in out],
                                          heads={c["pr"]: c["head"] for c in out},
                                          configuration=digest(config), candidates=digest(out)))


def arm_configuration(directory, presentation, admission, pool):
    import review_presentation
    base = read(directory / "configuration-base.json")
    frozen = read(directory / "frozen.json")
    if digest(base) != frozen["configuration"]:
        raise SystemExit("the frozen configuration has changed since freeze")
    if digest(read(directory / "candidates.json")) != frozen["candidates"]:
        raise SystemExit("the frozen candidates have changed since freeze")
    config = dict(base)
    config.update(presentation=review_presentation.normalise(presentation), frozen_inputs=True,
                  github_writes=False, comment_accounts={}, project_id="pilot-board", controller_delivers=False,
                  rereview_hours=0, admission=admission, pool_size=pool)
    config["endpoints"] = {name: dict(e, publish="") for name, e in (base.get("endpoints") or {}).items()}
    # a pass may write only the pilot, the shared caches and the reference
    # clones its worktree lives in, never the rest of production
    root = Path(os.environ.get("REVIEW_ROOT", os.path.expanduser("~/review")))
    grants = [str(directory.resolve()), str(root / "cache"), str(root / "ccache"),
              *sorted(set((base.get("reference_clones") or {}).values()))]
    config["providers"] = {name: dict(p, granted_directories=grants)
                           for name, p in (base.get("providers") or {}).items()}
    return config


def run(args):
    directory = outside_production(args.dir)
    arm = directory / args.arm
    data = arm / "data"
    data.mkdir(parents=True, exist_ok=True)
    # production's quota state, live, so a quota pause stops the pilot too
    quota = data / "quota.json"
    target = Path(production()) / "quota.json"
    if not quota.is_symlink() and not quota.exists():
        quota.symlink_to(target)
    elif not quota.is_symlink() or Path(os.readlink(quota)) != target:
        raise SystemExit("%s is not production's quota state" % quota)
    config = arm_configuration(directory, ARMS[args.arm], args.admission, args.pool)
    run_dir = data / "runs" / ("pilot-" + args.arm)
    if (run_dir / "run.json").exists():
        # a resume must be this experiment: every setting the arm froze
        import review_presentation
        persisted = read(run_dir / "run.json") or {}
        frozen_run = {k: v for k, v in (persisted.get("configuration") or {}).items() if k not in ("date", "stamp")}
        expected = dict(config, presentation=review_presentation.normalise(config["presentation"]),
                        prompts=review_presentation.prompts(review_presentation.normalise(config["presentation"])["prompts"]))
        changed = sorted(k for k in set(frozen_run) | set(expected) if frozen_run.get(k) != expected.get(k))
        frozen_prs = sorted((c["pr"], c["head"]) for c in read(directory / "candidates.json"))
        if sorted((c.get("pr"), c.get("head")) for c in persisted.get("candidates", [])) != frozen_prs:
            changed.append("candidates")
        if changed:
            raise SystemExit("%s was started with other settings: %s" % (run_dir, changed))
        command = ["--resume", str(run_dir)]
    else:
        atomic(arm / "configuration.json", config)
        command = ["--run", str(run_dir), "--candidates", str(directory / "candidates.json"),
                   "--config", str(arm / "configuration.json")]
    command = [sys.executable, str(BIN / "review-supervisor.py"), "candidates", "--data", str(data), *command,
               "--pool-size", str(args.pool), "--admission", str(args.admission)]
    print(" ".join(command), flush=True)
    return subprocess.call(command)


def live_work(data, clean=False):
    """A controller, guardian or payload that may still be running in a
    store, or None. As review_control's reaper judges it: a guardian's
    status, or before its first status its launch record, is the witness;
    an identity not yet complete (a launch record has a boot id but no pid
    until the guardian writes its status), or a live process whatever its
    record says, is live. A dead guardian has finished only when its record
    proves its payload empty; otherwise its recorded payload may live on.
    With clean, such an attempt is cleaned up as the reaper would
    (cleanup_attempt) and counts as finished only if that succeeds. A
    record that cannot be read counts as live."""
    from review_guardian import alive, cleanup_attempt
    from review_lock import boot_id
    complete = lambda r: isinstance(r, dict) and all(r.get(k) for k in ("boot", "pid", "start"))
    this_boot = boot_id()
    for record in glob.glob(os.path.join(data, "runs", "*", "summary.json")):
        try:
            value = read(record, {})
            if not isinstance(value, dict) or alive(value):
                return record
        except Exception:
            return record
    attempts = {Path(p).parent for pattern in ("launch.json", "status.json")
                for p in glob.glob(os.path.join(data, "runs", "*", "attempts", "*", pattern))}
    for attempt in sorted(attempts):
        try:
            status = read(attempt / "status.json", None)
            witness = status if isinstance(status, dict) else read(attempt / "launch.json", {}) or {}
            if isinstance(witness, dict) and witness.get("boot") and witness["boot"] != this_boot:
                continue                    # from before a reboot: nothing of it can be running
            if status is None:
                launch = attempt / "launch.json"
                # a launch an hour old with no status: its guardian never started
                if time.time() - launch.stat().st_mtime < 3600:
                    return str(attempt)
                continue
            if not complete(status) or alive(status):
                return str(attempt)
            if status.get("empty") is True:
                continue                    # the guardian proved its payload gone
            payload = status.get("payload")
            if isinstance(payload, dict) and alive(payload):
                return str(attempt)
            if not (clean and cleanup_attempt(attempt, time.monotonic() + 30)):
                return str(attempt)
        except Exception:
            return str(attempt)
    return None


def production_running(data):
    return live_work(data)


def pause_tool(*args):
    root = os.environ.get("REVIEW_ROOT", os.path.expanduser("~/review"))
    return subprocess.run([os.path.join(root, "bin", "pause-runs.sh"), *args], capture_output=True, text=True)


def overnight(args):
    """Pause production, run every arm together within a deadline, and on
    the way out, however it ends, stop every arm's controller and passes,
    wait until none is alive, and only then end the pause. If that cannot
    be shown, or the pause cannot be shown to have ended, it says so loudly
    and exits 3, leaving the pause to expire on its own."""
    import signal
    directory = outside_production(args.dir)
    # the pause holder inherits this working directory: keep it outside the
    # production store, where the reaper would end it and release the pause
    os.chdir(directory)
    deadline = time.monotonic() + args.hours * 3600
    arms, outcome, error, pause_attempted = [], 0, None, False
    stop_requested = []
    def stop(signum, frame):
        stop_requested.append(signum)       # acted on below; never raised across cleanup
    stopping = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    for signum in stopping:
        signal.signal(signum, stop)
    def check():
        if stop_requested:
            raise SystemExit("stopped by signal %d" % stop_requested[0])
    try:
        check()
        status = pause_tool("status")
        if status.returncode != 0 or status.stdout.startswith("PAUSED"):
            raise SystemExit("production is already paused, or its pause state cannot be read: not taking it over")
        pause_attempted = True          # from here a pause may be held, whatever the call returns
        if pause_tool(str(int(args.hours * 60) + 30)).returncode != 0:
            raise SystemExit("could not pause production")
        check()
        busy = production_running(production())
        if busy:
            raise SystemExit("production work is still running (%s): start the pilot when it has finished" % busy)
        for arm in ARMS:
            arms.append((arm, subprocess.Popen(
                [sys.executable, os.path.realpath(__file__), "run", "--dir", str(directory), "--arm", arm,
                 "--pool", str(args.pool), "--admission", str(args.admission)],
                stdout=open(directory / (arm + ".log"), "a"), stderr=subprocess.STDOUT)))
            check()
        while any(p.poll() is None for _, p in arms) and time.monotonic() < deadline - 1800:
            check()
            time.sleep(5)
        if any(p.poll() is None for _, p in arms):
            outcome = 4             # out of time
    except BaseException as caught:     # signals included: cleanup still runs
        error = caught
    # cleanup, with no signal able to cut it short, and any failure in it loud
    signal.pthread_sigmask(signal.SIG_BLOCK, stopping)
    clean = True
    try:
        if arms or any((directory / arm / "data").exists() for arm in ARMS):
            clean = stop_arms(directory, arms)
        if pause_attempted and clean:
            pause_tool("resume")
            status = pause_tool("status")
            clean = status.returncode == 0 and status.stdout.startswith("not paused")
    except BaseException as caught:
        print("cleanup failed: %r" % caught, file=sys.stderr)
        clean = False
    if pause_attempted and not clean:
        print("PRODUCTION IS STILL PAUSED (pilot passes may still be running, or the pause could not be "
              "ended): check %s, then run pause-runs.sh resume" % directory, file=sys.stderr)
        return 3
    if error is not None:
        print("pilot stopped: %s" % error, file=sys.stderr)
        return 1
    if outcome:
        print("pilot ran out of time; production resumed", file=sys.stderr)
        return outcome
    print("arms finished:", {arm: p.returncode for arm, p in arms})
    collect(argparse.Namespace(dir=str(directory)))
    return 0


def stop_arms(directory, arms, wait=1500):
    """Ask every arm to stop (abort.json, which its controller and guardians
    honour), and wait until no controller or pass of any arm is alive.
    True when that could be shown."""
    def abort_all():
        # every arm, even one whose run has not appeared yet: a supervisor
        # starting later finds the request and admits nothing
        for arm in ARMS:
            run_dir = directory / arm / "data" / "runs" / ("pilot-" + arm)
            if not (read(run_dir / "summary.json", {}) or {}).get("state") == "complete":
                atomic(run_dir / "abort.json", {"requested": True})
    end = time.monotonic() + wait
    while True:
        abort_all()
        if all(p.poll() is not None for _, p in arms) and not any(
                live_work(str(directory / arm / "data"), clean=True)
                for arm in ARMS if (directory / arm / "data").exists()):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(10)


def arm_results(directory, arm, frozen):
    """{pr: (bundle results, previous findings)} for every frozen PR, or the
    problems that stop this arm being compared."""
    import review_presentation
    store, out, problems = Store(directory / arm / "data"), {}, []
    config = read(directory / arm / "configuration.json") or {}
    if config.get("presentation") != review_presentation.normalise(ARMS[arm]):
        problems.append("%s ran presentation %r" % (arm, config.get("presentation")))
    candidates = {c["pr"]: c for c in read(directory / "candidates.json")}
    expected = review_presentation.recorded(ARMS[arm])
    for pr in frozen["prs"]:
        bundle = store.bundle(pr)
        if not bundle:
            problems.append("%s: %s never accepted" % (arm, pr))
            continue
        inputs = bundle.get("inputs") or {}
        # the review was of exactly the frozen inputs, under this arm's presentation
        differ = [k for k in ("head", "base", "merge_base", "diff", "thread", "title", "rules", "previous_comment")
                  if inputs.get(k) != candidates[pr].get(k)]
        if differ or inputs.get("presentation") != expected:
            problems.append("%s: %s accepted with other inputs: %s" % (arm, pr, differ or ["presentation"]))
            continue
        previous = ((bundle.get("inputs") or {}).get("previous_comment") or {}).get("findings") or []
        out[pr] = (bundle.get("results") or {}, {f["id"]: f for f in previous if isinstance(f, dict)})
    return out, problems


def kept_findings(results, previous):
    """The findings an arm's review kept: each upstream finding and each
    previous finding its reconciliation did not refute or merge away, with
    reconciliation's word on it when it adjusted one. A finding merged into
    another is folded into the one that survives, with its location, so a
    problem only it described is still there to be judged. Text is kept as
    written: the pack stays with the key, and packets blinds it."""
    final = results.get("reconciliation") or {}
    outcomes = {o["id"]: o for o in final.get("outcomes") or []}
    every = {}
    for kind in ("primary", "cold", "validation"):
        r = results.get(kind) or {}
        for f in (r.get("findings") or []) + (r.get("new") or []):
            every[f["id"]] = dict(f, origin=kind)
    for ident, f in previous.items():
        every[ident] = dict(f, id=ident, origin="previous")
    def survivor(ident, seen=()):
        o = outcomes.get(ident, {})
        if o.get("disposition") == "merged" and o.get("target") in every and o["target"] not in seen:
            return survivor(o["target"], seen + (ident,))
        return ident
    found, kept = [], {}
    for ident, f in every.items():
        o = outcomes.get(ident, {})
        if o.get("disposition") in ("refuted", "merged") or (f["origin"] == "previous" and not o.get("disposition")):
            continue
        entry = dict(claim=f.get("claim"), kind="PREVIOUS" if f["origin"] == "previous" else f.get("kind"),
                     location={"non_line_specific": True} if f["origin"] == "previous"
                     else f.get("location"),
                     severity=f.get("severity") if f["origin"] != "previous" else "",
                     status=f.get("status") if f["origin"] != "previous" else "",
                     final=o.get("disposition"), final_rationale=o.get("rationale"),
                     claimed_blocking=bool(o.get("blocking")))
        kept[ident] = entry
        found.append(entry)
    for ident, f in every.items():
        o = outcomes.get(ident, {})
        target = survivor(ident) if o.get("disposition") == "merged" else None
        if target in kept and target != ident:
            where = f.get("location") if f["origin"] != "previous" else None
            kept[target]["claim"] += "\n\nMerged into this finding%s: %s" % (
                " (at %s)" % json.dumps(where, sort_keys=True) if where else "", f.get("claim") or "")
            if o.get("rationale"):
                kept[target]["final_rationale"] = ((kept[target]["final_rationale"] or "") +
                                                   "\n\nOn the merged finding: " + o["rationale"]).strip()
    return found


def contexts(data):
    """Every attempt's context, retries included, with whether it was selected."""
    out = {}
    selected = set()
    for claim in glob.glob(os.path.join(data, "results", "*", "*", "*", "claim.json")):
        selected |= set((read(claim, {}) or {}).get("selected", {}).values())
    for path in glob.glob(os.path.join(data, "runs", "*", "attempts", "*", "job.json")):
        attempt = str(Path(path).parent)
        job = read(path) or {}
        out.setdefault(job.get("pr"), {}).setdefault(job.get("kind"), []).append(
            dict(attempt=attempt, selected=attempt in selected, context=read(Path(attempt) / "context.json")))
    return out


def collect(args):
    directory = Path(args.dir)
    frozen = read(directory / "frozen.json")
    arms = [arm for arm in ARMS if (directory / arm / "data").is_dir()]
    every, problems = {}, []
    for arm in arms:
        every[arm], trouble = arm_results(directory, arm, frozen)
        problems += trouble
    if len(arms) != len(ARMS):
        problems.append("arms missing: %s" % sorted(set(ARMS) - set(arms)))
    for arm in arms:
        # each finished pass's context, in the pilot store itself
        subprocess.run([sys.executable, str(BIN / "review-context.py"), "--data", str(directory / arm / "data"),
                        "--days", "30"], capture_output=True)
    measured = {arm: contexts(directory / arm / "data") for arm in arms}
    unmeasured = {arm: sum(1 for kinds in v.values() for attempts in kinds.values() for a in attempts
                           if not a["context"] or a["context"].get("error")) for arm, v in measured.items()}
    atomic(directory / "contexts.json", dict(arms=measured, unmeasured=unmeasured))
    if problems:
        atomic(directory / "collect-problems.json", problems)
        raise SystemExit("not comparable yet: %s" % problems)
    pooled = []
    for arm in arms:
        for pr in frozen["prs"]:
            results, previous = every[arm][pr]
            pooled += [dict(f, pr=pr, arm=arm) for f in kept_findings(results, previous)]
    random.SystemRandom().shuffle(pooled)
    pack, key = [], {}
    for f in pooled:
        ident = secrets.token_hex(4)
        key[ident] = dict(arm=f.pop("arm"), pr=f["pr"])
        pack.append(dict(id=ident, **f))
    atomic(directory / "adjudication-pack.json", sorted(pack, key=lambda f: (f["pr"], f["id"])))
    atomic(directory / "adjudication-key.json", key)
    print("%d findings over %d PRs from %s; arm key kept apart in adjudication-key.json"
          % (len(pack), len(frozen["prs"]), ", ".join(arms)))


def score(args):
    """Unblind the verdicts and apply the bound. verdicts.json holds, for
    every pack id: real (true/false), a reason, and for a real one issues:
    {name: whether it should block}, each name shared by every finding, in
    either arm, that describes the same real problem."""
    directory = Path(args.dir)
    key, verdicts = read(directory / "adjudication-key.json"), read(Path(args.verdicts))
    pack = {f["id"]: f for f in read(directory / "adjudication-pack.json")}
    missing = sorted(set(key) - set(verdicts))
    if missing:
        raise SystemExit("verdicts missing for %d findings, e.g. %s" % (len(missing), missing[:3]))
    issues, false_blockers, errors = {arm: {} for arm in ARMS}, {arm: 0 for arm in ARMS}, []
    blocking_of = {}
    for ident, verdict in verdicts.items():
        if ident not in key:
            errors.append("%s is not in the pack" % ident)
            continue
        problem = check_verdict(verdict)
        if problem:
            errors.append("%s: %s" % (ident, problem))
            continue
        arm, pr = key[ident]["arm"], key[ident]["pr"]
        named = verdict.get("issues") or {}
        # a finding may describe several real problems: each counts as found
        for issue, blocking in named.items():
            if blocking_of.setdefault((pr, issue), blocking) != blocking:
                errors.append("%s: issue %r judged both blocking and not" % (ident, issue))
            issues[arm][(pr, issue)] = blocking
        # a false blocker: raised as blocking, but describing no real problem that should block
        if pack[ident].get("claimed_blocking") and not any(named.values()):
            false_blockers[arm] += 1
    if errors:
        raise SystemExit("verdicts not usable: %s" % errors[:10])
    base, treatment = "legacy", "paging"
    missed = {name: b for name, b in issues[base].items() if name not in issues[treatment]}
    gained = [name for name in issues[treatment] if name not in issues[base]]
    result = dict(
        baseline=base, treatment=treatment, bound=BOUND,
        real={arm: len(v) for arm, v in issues.items()},
        real_blockers={arm: sum(v.values()) for arm, v in issues.items()},
        missed_blockers=sum(1 for b in missed.values() if b), missed_other=sum(1 for b in missed.values() if not b),
        gained=len(gained), false_blockers=false_blockers,
        missed=[dict(pr=pr, issue=issue, blocking=b) for (pr, issue), b in sorted(missed.items())])
    result["passed"] = (result["missed_blockers"] <= BOUND["missed_blockers"]
                        and result["missed_other"] <= BOUND["missed_other"]
                        and false_blockers[treatment] - false_blockers[base] <= BOUND["false_blockers"])
    atomic(directory / "score.json", result)
    print(json.dumps(result, indent=1))


def slug(pr):
    return pr.split(":", 1)[-1].replace("/", "-").replace("#", "-")


def ours(entry):
    """One of our own review comments, the deprecated ones included: left
    out of what the adjudicator reads, so earlier verdicts do not anchor
    it. Judged by unquoted lines, so an author quoting us is kept."""
    body = (entry or {}).get("body") or ""
    unquoted = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith(">"))
    return "Automated review note" in unquoted or bool(re.search(r"<!--\s*apreview:", unquoted, re.I))


# what in a reviewer's text could tell the adjudicator its arm or someone's
# view of severity; the adjudicator decides severity itself
BLIND = [
    # an attempt's own paths; a repository's src/inputs/x.c is left alone
    (r"\S*(?:/attempts/|REVIEW_JOB_DIR|REVIEW_SCRATCH)\S*|(?<![\w/])(?:\./)?inputs/\S*", "[path]"),
    (r"(?<![\w/.-])(?:job|manifest|index|result-skeleton|skeleton|diffstat|facts|fresh_snapshot)\.(?:json|md|txt)\b",
     "[input]"),
    (r"\b(?:v4-paging|v3-files|v2-schema)\b", "[reviewer]"),
    (r"\b(?:primary|cold|validation|reconciliation|previous|upstream):[A-Za-z]*\d+\b", "[finding]"),
    # unmistakable review severity only: words like "block" and "priority"
    # are often the code's own sense, and are left for the audit below
    (r"\b(?:non-?blocking|blocking)\s+(?:finding|review finding)s?\b", "[severity] finding"),
    (r"\b(?:merge[- ])?blockers?\b", "[severity]"),
    (r"\bblocks? (?:the )?merg(?:e|ing)\b", "[severity]"),
    (r"\b(?:retained|kept|adjusted|marked)[,;:]?\s+(?:as\s+)?(?:non-?)?blocking\b", "[severity]"),
    (r"\bblocking\s*[:=]\s*(?:true|false|yes|no)\b", "[severity]"),
    (r"\b(?:must[- ]fix|should[- ]fix)\b", "[severity]"),
    (r"\b(?:critical|high|medium|low)[- ]severity\b", "[severity]"),
    (r"\bseverity\s*[:=]\s*(?:critical|high|medium|low|major|minor)\b", "[severity]"),
]
SUSPECT = re.compile(r"(?i)\b(?:block(?:s|ing|er)?|severity|priority|legacy|paging|skeleton|schema|nit|F\d+)\b")


def blind(text):
    if not isinstance(text, str):
        return text
    for pattern, replacement in BLIND:
        text = re.sub(pattern, replacement, text, flags=re.I)
    return text


def packets(args):
    """One blinded work directory per PR for the adjudicator: the findings
    without arm, kind, severity or anyone's blocking view, and the frozen
    inputs. The key stays in the pilot directory: adjudicate on a machine
    that does not hold it."""
    directory = Path(args.dir).resolve()
    out = Path(args.out).resolve()
    if out == directory or directory in out.parents or out in directory.parents:
        raise SystemExit("packets go outside the pilot directory, away from its key")
    pack = read(directory / "adjudication-pack.json")
    if pack is None:
        raise SystemExit("no adjudication-pack.json: collect first")
    candidates = {c["pr"]: c for c in read(directory / "candidates.json")}
    prompt = (BIN.parent.parent / "commands" / "pilot-adjudicate.md").read_text()
    suspects = []
    for pr, c in candidates.items():
        work = out / slug(pr)
        if (work / "verdicts.json").exists():
            raise SystemExit("%s already has verdicts: not overwriting" % work)
        work.mkdir(parents=True, exist_ok=True)
        findings = []
        for f in (f for f in pack if f["pr"] == pr):
            location = f["location"]
            if isinstance(location, dict):
                location = {k: blind(v) for k, v in location.items()}
            findings.append(dict(id=f["id"], claim=blind(f["claim"]), location=location,
                                 previous=f.get("kind") == "PREVIOUS", adjusted=f.get("final") == "adjusted",
                                 reviewer_notes=blind(f.get("final_rationale") or "")))
            for field, original, blinded in (("claim", f["claim"], findings[-1]["claim"]),
                                             ("reviewer_notes", f.get("final_rationale") or "",
                                              findings[-1]["reviewer_notes"]),
                                             ("location", f["location"], location)):
                if blinded != original or SUSPECT.search(json.dumps(blinded)):
                    suspects.append(dict(pr=pr, id=f["id"], field=field, original=original, blinded=blinded))
        atomic(work / "packet.json", dict(pr=pr, title=c.get("title"), head=c["head"], base=c.get("base"),
                                          merge_base=c.get("merge_base"), findings=findings))
        (work / "diff.patch").write_text(c.get("diff") or "")
        atomic(work / "thread.json", [t for t in c.get("thread") or [] if not ours(t)])
        (work / "rules.md").write_text(c.get("rules") or "")
        (work / "PROMPT.md").write_text(prompt)
        print("%s: %d findings -> %s (check out %s at %s into %s)"
              % (pr, len(findings), work, c.get("repository"), c["head"], work / "code"))
    # read before any packet is judged, by Claude and never by tridge, whose
    # spot-check must stay blind: what blinding changed, and what may still tell
    atomic(Path(args.dir) / "suspects.json", suspects)
    print("%d passages to read (original and blinded) before judging: %s"
          % (len(suspects), Path(args.dir) / "suspects.json"))


def check_verdict(verdict):
    """What is wrong with one finding's verdict, or None."""
    if not isinstance(verdict, dict) or not isinstance(verdict.get("real"), bool):
        return "real must be true or false"
    if not isinstance(verdict.get("reason"), str) or not verdict["reason"].strip():
        return "a reason is needed"
    issues = verdict.get("issues")
    if verdict["real"]:
        if not isinstance(issues, dict) or not issues or not all(
                isinstance(k, str) and k and isinstance(v, bool) for k, v in issues.items()):
            return "a real finding needs issues: {name: blocking (true/false)}"
    elif issues:
        return "a finding that is not real names no issues"
    return None


def inconsistent(verdicts, pr_of):
    """Issues judged both blocking and not, within a PR."""
    blocking, errors = {}, []
    for ident, v in verdicts.items():
        for name, b in ((v or {}).get("issues") or {}).items():
            if blocking.setdefault((pr_of(ident), name), b) != b:
                errors.append("%s: issue %r judged both blocking and not" % (pr_of(ident), name))
    return errors


def verdicts(args):
    """Merge the adjudicator's per-PR verdicts, checking they cover exactly
    the pack and are complete and consistent, into one file for score."""
    directory, out, merged, errors = Path(args.dir), Path(args.out), {}, []
    pack = read(directory / "adjudication-pack.json")
    for pr in sorted({f["pr"] for f in pack} | set(read(directory / "frozen.json")["prs"])):
        ids = {f["id"] for f in pack if f["pr"] == pr}
        got = read(out / slug(pr) / "verdicts.json")
        if not isinstance(got, dict):
            if ids:
                errors.append("%s: no verdicts.json" % pr)
            continue
        if set(got) != ids:
            errors.append("%s: verdicts for %s, findings %s" % (pr, sorted(set(got) - ids), sorted(ids - set(got))))
        for ident, v in got.items():
            problem = check_verdict(v)
            if problem:
                errors.append("%s %s: %s" % (pr, ident, problem))
        if not errors:
            errors += inconsistent(got, lambda ident: pr)
        merged.update(got)
    if errors:
        raise SystemExit("verdicts not complete: %s" % errors)
    atomic(directory / "verdicts.json", merged)
    print("%d verdicts merged into %s" % (len(merged), directory / "verdicts.json"))


def spotcheck(args):
    """tridge's sample, still blind: ten findings at random, and every
    finding of a real blocker that only one arm found."""
    directory = Path(args.dir)
    key, verdict = read(directory / "adjudication-key.json"), read(directory / "verdicts.json")
    # the audited packets, exactly as the adjudicator saw them: never the pack
    shown = {}
    for packet in Path(args.packets).glob("*/packet.json"):
        p = read(packet)
        shown.update({f["id"]: dict(f, pr=p["pr"]) for f in p["findings"]})
    if set(shown) != set(key):
        raise SystemExit("the packets do not cover the pack: %d of %d findings" % (len(set(shown) & set(key)), len(key)))
    if not isinstance(verdict, dict) or set(verdict) != set(key) or any(check_verdict(v) for v in verdict.values()) \
            or inconsistent(verdict, lambda ident: key[ident]["pr"]):
        raise SystemExit("run verdicts first: verdicts.json is not complete and checked")
    arms_of = {}
    for ident, v in verdict.items():
        for name, b in (v.get("issues") or {}).items():
            if b is True:
                arms_of.setdefault((key[ident]["pr"], name), set()).add(key[ident]["arm"])
    lone = sorted(i for i, v in verdict.items()
                  if any(b is True and len(arms_of[(key[i]["pr"], name)]) == 1
                         for name, b in (v.get("issues") or {}).items()))
    rest = [i for i in sorted(verdict) if i not in lone]
    sample = random.SystemRandom().sample(rest, min(10, len(rest)))
    lines = ["# Adjudication spot-check", "",
             "For each: is the verdict right? Arms stay hidden.", ""]
    for title, ids in (("Real blockers only one arm found", lone), ("Random sample", sample)):
        lines += ["## %s (%d)" % (title, len(ids)), ""]
        for ident in ids:
            f, v = shown[ident], verdict[ident]
            lines += ["### %s %s" % (f["pr"], ident), "",
                      "- claim: %s" % f["claim"],
                      "- location: %s" % json.dumps(f["location"]),
                      "- previous: %s; adjusted by the reviewers' last pass: %s"
                      % ("yes" if f["previous"] else "no", "yes" if f["adjusted"] else "no"),
                      "- reviewers' notes: %s" % f["reviewer_notes"],
                      "- verdict: real=%s issues=%s" % (v["real"], json.dumps(v.get("issues") or {})),
                      "- reason: %s" % v["reason"], ""]
    (directory / "spotcheck.md").write_text("\n".join(lines))
    atomic(directory / "spotcheck.json", dict(lone=lone, sample=sample))
    print("%d lone blockers and %d sampled findings in %s" % (len(lone), len(sample), directory / "spotcheck.md"))


def spotcheck_html(args):
    """The spot-check as a page: per PR, the two arms' kept findings side by
    side as Review A and Review B, assigned at random per PR (the assignment
    is kept beside the key), each with the adjudicator's verdict; issues are
    coloured alike across columns and the findings to check are marked."""
    import html as H
    directory = Path(args.dir)
    key, verdict = read(directory / "adjudication-key.json"), read(directory / "verdicts.json")
    chosen = read(directory / "spotcheck.json")
    if chosen is None:
        # a spot-check written before spotcheck.json: its ids are in the headings
        text = (directory / "spotcheck.md").read_text()
        part = text.split("## Random sample")
        ids = lambda t: re.findall(r"^### \S+ ([0-9a-f]{8})$", t, re.M)
        chosen = dict(lone=ids(part[0]), sample=ids(part[1]) if len(part) > 1 else [])
    marked = {i: "only one review raised this real blocker" for i in chosen["lone"]}
    marked.update({i: "random sample" for i in chosen["sample"]})
    packets = {}
    for path in Path(args.packets).glob("*/packet.json"):
        p = read(path)
        packets[p["pr"]] = p
    labels = read(directory / "spotcheck-labels.json") or {}
    rng = random.SystemRandom()
    for pr in packets:
        if pr not in labels:
            arms = list(ARMS)
            rng.shuffle(arms)
            labels[pr] = dict(zip("AB", arms))
    atomic(directory / "spotcheck-labels.json", labels)
    issues = sorted({n for v in verdict.values() for n in (v.get("issues") or {})})
    colour = {n: "hsl(%d 60%% 85%%)" % (i * 137 % 360) for i, n in enumerate(issues)}
    order = []
    for ident in chosen["lone"] + chosen["sample"]:
        if key[ident]["pr"] not in order:
            order.append(key[ident]["pr"])
    def card(f):
        v = verdict[f["id"]]
        chips = "".join('<span class="chip" style="background:%s">%s%s</span>'
                        % (colour[n], H.escape(n), " &middot; <b>blocking</b>" if b else "")
                        for n, b in (v.get("issues") or {}).items()) or '<span class="chip notreal">not real</span>'
        mark = marked.get(f["id"])
        loc = f["location"] if isinstance(f["location"], dict) else {}
        where = "%s:%s" % (loc.get("file"), loc.get("line")) if loc.get("file") else "not line-specific"
        flags = " ".join(x for x, on in (("previous", f["previous"]), ("adjusted", f["adjusted"])) if on)
        return ('<div class="card%s" id="f%s"><div class="head"><code>%s</code> <span class="where">%s</span> %s%s</div>'
                '<div class="claim">%s</div>%s<div class="verdict">%s</div><div class="reason">%s</div>%s</div>') % (
            " check" if mark else "", f["id"], f["id"], H.escape(where),
            '<span class="flag">%s</span>' % flags if flags else "",
            '<span class="tag">check: %s</span>' % mark if mark else "",
            H.escape(f["claim"]),
            '<details><summary>reviewers\' notes</summary><div class="notes">%s</div></details>' % H.escape(f["reviewer_notes"])
            if f["reviewer_notes"] else "",
            chips, H.escape(v.get("reason", "")),
            ('<div class="answer"><label><input type="radio" name="a%s" value="right"> verdict right</label> '
             '<label><input type="radio" name="a%s" value="wrong"> wrong</label> '
             '<input type="text" name="n%s" placeholder="what is wrong"></div>') % (f["id"], f["id"], f["id"])
            if mark else "")
    sections = []
    for pr in order + sorted(set(packets) - set(order)):
        p = packets[pr]
        columns = []
        for label in "AB":
            arm = labels[pr][label]
            found = [f for f in p["findings"] if key[f["id"]]["arm"] == arm]
            found.sort(key=lambda f: (f["id"] not in marked, f["id"]))
            columns.append('<div class="col"><h3>Review %s <span class="sub">%d findings</span></h3>%s</div>'
                           % (label, len(found), "".join(card(f) for f in found)))
        owner, rest = pr[3:].split("/", 1)
        repo, number = rest.split("#")
        sections.append('<section%s><h2><a href="https://github.com/%s/%s/pull/%s">%s</a> %s</h2>'
                        '<div class="sub">frozen head %s</div><div class="cols">%s</div></section>' % (
            "" if pr in order else ' class="other"', owner, repo, number, H.escape(pr[3:]),
            H.escape(p.get("title") or ""), p["head"][:10], "".join(columns)))
    page = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Pilot spot-check</title><style>
:root{--bg:#fff;--fg:#111;--sub:#666;--card:#f7f7f7;--line:#ddd;--check:#c60}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--sub:#999;--card:#1c1c1c;--line:#333;--check:#f90}
 .chip{color:#111}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0 16px 80px}
h1{font-size:20px}h2{font-size:16px;margin:28px 0 2px}h3{font-size:14px;margin:8px 0}
.sub{color:var(--sub);font-weight:normal;font-size:12px}a{color:inherit}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media (max-width:800px){.cols{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 10px;margin:0 0 10px}
.card.check{border:2px solid var(--check)}.tag{color:var(--check);font-weight:600;font-size:12px}
.head{font-size:12px;color:var(--sub);margin-bottom:4px}.flag{font-size:11px;border:1px solid var(--line);padding:0 4px;border-radius:3px}
.claim{white-space:pre-wrap}.notes{white-space:pre-wrap;color:var(--sub);font-size:13px}
.verdict{margin:6px 0 2px}.chip{display:inline-block;border-radius:10px;padding:1px 8px;margin:2px 4px 2px 0;font-size:12px}
.chip.notreal{background:var(--line);color:var(--fg)}.reason{font-size:13px;color:var(--sub)}
.answer{margin-top:6px;font-size:13px}.answer input[type=text]{width:60%%}
section.other{opacity:.85}#bar{position:fixed;bottom:0;left:0;right:0;background:var(--card);border-top:1px solid var(--line);padding:8px 16px}
textarea{width:100%%;height:5em}
</style></head><body>
<h1>Quality pilot spot-check</h1>
<p class="sub">Per PR, the two reviews' kept findings side by side. Which review is which arm is hidden, and assigned
at random per PR. Each finding shows the adjudicator's verdict: issue names (same colour = same problem; <b>blocking</b>
if it should block) or <i>not real</i>, and its reason. Findings outlined in orange are the %d to check; mark each, then
copy your answers below. PRs without a finding to check follow, for context.</p>
%s
<div id="bar"><button onclick="collect()">Copy my answers</button> <span id="done" class="sub"></span>
<textarea id="out" readonly placeholder="answers appear here"></textarea></div>
<script>
function collect(){var out=[],n=0;document.querySelectorAll('.card.check').forEach(function(c){var id=c.id.slice(1);
 var r=document.querySelector('input[name=a'+id+']:checked');var t=document.querySelector('input[name=n'+id+']').value;
 if(r)n++;out.push(id+': '+(r?r.value:'unanswered')+(t?' - '+t:''));});
 var o=document.getElementById('out');o.value=out.join('\\n');o.select();
 try{navigator.clipboard.writeText(o.value)}catch(e){}
 document.getElementById('done').textContent=n+' of '+out.length+' answered';}
</script></body></html>""" % (len(marked), "".join(sections))
    (directory / "spotcheck.html").write_text(page)
    print("%s: %d PRs, %d findings to check" % (directory / "spotcheck.html", len(order), len(marked)))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--out", required=True)
    f.add_argument("prs", nargs="+")
    for name in ("run", "overnight"):
        r = sub.add_parser(name)
        r.add_argument("--dir", required=True)
        r.add_argument("--pool", type=int, default=2)
        r.add_argument("--admission", type=float, default=12 * 3600)
        if name == "run":
            r.add_argument("--arm", required=True, choices=sorted(ARMS))
        else:
            r.add_argument("--hours", type=float, default=10)
    c = sub.add_parser("collect")
    c.add_argument("--dir", required=True)
    k = sub.add_parser("packets")
    k.add_argument("--dir", required=True)
    k.add_argument("--out", required=True)
    v = sub.add_parser("verdicts")
    v.add_argument("--dir", required=True)
    v.add_argument("--out", required=True)
    sp = sub.add_parser("spotcheck")
    sp.add_argument("--dir", required=True)
    sp.add_argument("--packets", required=True)
    sh = sub.add_parser("spotcheck-html")
    sh.add_argument("--dir", required=True)
    sh.add_argument("--packets", required=True)
    s = sub.add_parser("score")
    s.add_argument("--dir", required=True)
    s.add_argument("--verdicts", required=True)
    args = p.parse_args()
    return {"freeze": freeze, "run": run, "overnight": overnight, "collect": collect, "packets": packets,
            "verdicts": verdicts, "spotcheck": spotcheck, "spotcheck-html": spotcheck_html, "score": score}[args.command](args) or 0


if __name__ == "__main__":
    sys.exit(main())
