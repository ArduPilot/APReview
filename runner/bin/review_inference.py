"""Build a real CLI attempt from frozen configuration; no inference at import."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from review_lock import acquire
from review_store import mkdir

BIN = Path(__file__).resolve().parent
COMMANDS = BIN.parents[1] / "commands"
PROMPTS = dict(primary="primary", cold="cold", validation="validate", reconciliation="reconcile")


def prepare(store, path, job, config):
    try:
        return _prepare(store, path, job, config)
    except (subprocess.SubprocessError, KeyError) as error:
        raise OSError("attempt preparation failed: " + str(error)) from error


def _prepare(store, path, job, config):
    provider = config["providers"][job["provider"]]
    reference = Path(config["reference_clones"][job["repository"]]).resolve()
    worktree = path / "wt"
    logs = path / "buildlogs"
    deadline = time.monotonic() + config.get("permit_timeout", 120)
    lock = acquire(store.locks, "refresh", deadline, shared=True)
    if lock is None:
        raise TimeoutError("worktree refresh region busy")
    with lock:
        check = subprocess.run(
            ["git", "-C", str(reference), "config", "--get-regexp", r"remote\..*\.promisor"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if any(line.split()[-1].lower() == "true" for line in check.stdout.splitlines()):
            raise OSError("partial reference clones are forbidden")
        subprocess.run(
            [
                "git",
                "-C",
                str(reference),
                "worktree",
                "add",
                "--detach",
                str(worktree),
                job["head"],
            ],
            check=True,
            capture_output=True,
            timeout=max(1, deadline - time.monotonic()),
            pass_fds=(lock.fd,),
        )
    mkdir(logs)
    scratch = path / "scratch"
    mkdir(scratch)
    job.update(
        worktree=str(worktree),
        reference_clone=str(reference),
        account=provider["account"],
        exclusive_account=provider.get("exclusive_account", True),
        account_slots=provider.get("account_slots", 8),
    )
    env = job["env"]
    # Resolve symlinks too; an alias of the base Tools/autotest is still the base.
    base_tools = reference / "Tools" / "autotest"
    paths = [
        p
        for p in config.get("path", os.environ["PATH"]).split(os.pathsep)
        if p and Path(p).resolve() != base_tools
    ]
    env["PATH"] = os.pathsep.join([str(BIN), str(worktree / "Tools" / "autotest"), *paths])
    env["BUILDLOGS"] = str(logs)
    # Scratch work stays inside the attempt, where GC finds it; caches are
    # shared across attempts, outside the store.
    cache = Path(env.get("REVIEW_ROOT") or os.environ.get("REVIEW_ROOT") or store.root.parent) / "cache"
    env["REVIEW_SCRATCH"] = env["TMPDIR"] = str(scratch)
    env["REVIEW_VENVS"] = str(cache / "venvs")
    env["UV_CACHE_DIR"] = str(cache / "uv")
    env["PIP_CACHE_DIR"] = str(cache / "pip")
    env["npm_config_cache"] = str(cache / "npm")
    # the wrapper exports it, but a systemd guardian need not inherit that
    env.setdefault("CCACHE_DIR", str(cache.parent / "ccache"))
    for name in ("venvs", "uv", "pip", "npm"):
        mkdir(cache / name)
    env["REVIEW_DATA"] = str(store.root)
    env["REVIEW_HEAVY_SIZE"] = str(config.get("heavy_size", 4))
    env["REVIEW_HEAVY_WAIT"] = str(config.get("permit_timeout", 120))
    env["CLAUDE_CONFIG_DIR" if job["provider"] == "claude" else "CODEX_HOME"] = provider["home"]
    import review_presentation
    presentation = review_presentation.normalise(config.get("presentation"))
    result_file = path / __import__("review_schema").FILES[job["kind"]]
    if presentation["renderer"]:
        mkdir(path / "evidence")
        render_inputs(path, job, result_file, presentation["renderer"])
    prompt = config.get("prompts", {}).get(job["kind"])
    if prompt is None:
        # only a legacy run may fall back to the live prompt files
        if presentation != review_presentation.LEGACY:
            raise KeyError("prompt for %s not frozen in the run" % job["kind"])
        prompt = (COMMANDS / ("review-" + PROMPTS[job["kind"]] + ".md")).read_text()
    if presentation["inputs"] == "files":
        prompt += "\nRead " + str(path / "inputs" / "README.md") + ". Write " + str(result_file) + ".\n"
    else:
        prompt += "\nRead " + str(path / "job.json") + ". Write " + str(result_file) + ".\n"
    if presentation["renderer"]:
        prompt += "Check your result: " + check_command(result_file) + "\n"
    else:
        prompt += "Schema validator: " + str(BIN / "review_schema.py") + "\n"
    prompt += "Repository house rules:\n" + job.get("rules", "")
    grants = list(dict.fromkeys([str(path), *provider.get("granted_directories", [])]))
    if job["provider"] == "claude":
        command = [
            "claude",
            "-p",
            prompt,
            "--model",
            provider["model"],
            "--effort",
            provider["effort"],
            "--permission-mode",
            provider["permission_mode"],
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        for directory in grants:
            command += ["--add-dir", directory]
    else:
        model, effort = provider["model"], provider["effort"]
        if job.get("refused_before"):
            # the content filter declined this pass once; ask another model
            model = provider.get("fallback_model") or model
            effort = provider.get("fallback_effort") or effort
            job["fallback"] = dict(model=model, effort=effort)
        command = [
            "codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--model",
            model,
            "-c",
            'model_reasoning_effort="' + effort + '"',
            "--sandbox",
            provider["permission_mode"],
        ]
        for directory in grants:
            command += ["--add-dir", directory]
        command += [prompt]
    job["cli_command"] = command
    job["command"] = [sys.executable, str(BIN / "review_inference.py")]


def check_command(result_file):
    return "python3 %s check %s" % (BIN / "review_schema.py", result_file)


# what the main review inputs in job.json hold
JOB_INPUTS = {"title": "the PR title", "diff": "the PR diff at the pinned head",
              "thread": "the PR conversation", "rules": "the repository's house rules",
              "previous_comment": "our previous comment and its findings",
              "previous_section": "our previous report section",
              "previous_ids": "our previous findings (what each pass owes them: see its prompt)",
              "primary_result": "the primary review to challenge", "primary_ids": "its finding ids",
              "results": "the primary, cold and validation results", "finding_ids": "every id to settle",
              "fresh_snapshot": "the PR as it is now: title, head, thread", "ci": "CI state"}
# fields that only run the pass; every other field is a fact about the review
RUNNER_FIELDS = {"schema", "run", "job", "attempt", "generation", "kind", "provider", "account",
                 "exclusive_account", "account_slots", "input_digest", "abort_path", "registered", "env",
                 "wall_timeout", "pool_size", "permit_timeout", "command", "cli_command", "stub",
                 "configuration", "worktree", "reference_clone", "refused_before", "fallback", "presentation",
                 "result_sources"}


def size(value):
    """A field as the README describes it: a short plain value itself, else
    its size as JSON text, with lines too for text."""
    import json
    if isinstance(value, (int, str)) and not isinstance(value, bool) and len(str(value)) <= 80 and "\n" not in str(value):
        return json.dumps(value)
    chars = len(json.dumps(value))
    out = "%d %s" % (chars, "char" if chars == 1 else "chars")
    if isinstance(value, str) and "\n" in value:
        lines = len(value.splitlines())
        out += ", %d %s" % (lines, "line" if lines == 1 else "lines")
    return out


def schema_inputs(path, job, result_file):
    """Renderer 1: a README of where everything is, the result format and a
    result skeleton, beside job.json."""
    import json
    import review_schema
    kind = job["kind"]
    lines = ["# Inputs for this %s pass" % kind, "",
             "Paths:", "",
             "- job directory (REVIEW_JOB_DIR): %s" % path,
             "- the PR at its pinned head %s: %s" % (job.get("head", "?")[:12], job.get("worktree", path / "wt")),
             "- scratch for builds and temporary files (REVIEW_SCRATCH): %s" % (path / "scratch"),
             "- your retained evidence: %s; cite it as evidence/<name>, relative to the job directory"
             % (path / "evidence"),
             "- the result to write: %s" % result_file, "",
             "Review inputs, all in %s (sizes as the JSON text):" % (path / "job.json"), ""]
    facts = [k for k in job if k not in RUNNER_FIELDS and k not in JOB_INPUTS]
    for key in [k for k in JOB_INPUTS if k in job] + facts:
        what = JOB_INPUTS.get(key)
        lines.append("- `%s`: %s%s" % (key, (what + ", ") if what else "", size(job[key])))
    lines += ["", "Its other fields (%s) only run this pass." % ", ".join(sorted(RUNNER_FIELDS)),
              "", "This directory, inputs/, describes your result and repeats nothing from job.json:", ""]
    files = {"schema.md": review_schema.describe(kind),
             "result-skeleton.json": json.dumps(review_schema.skeleton(job), indent=1) + "\n"}
    lines += ["- inputs/schema.md (%d chars): the result format" % len(files["schema.md"]),
              "- inputs/result-skeleton.json (%d chars): your result, to copy and fill in"
              % len(files["result-skeleton.json"]),
              "- inputs/manifest.json: digests of these files, for the runner",
              "", "Check your result: " + check_command(result_file), ""]
    files["README.md"] = "\n".join(lines)
    return files


def render_inputs(path, job, result_file, renderer):
    """inputs/, whole: written into a staging directory with a manifest of
    every file's digest, each file and directory fsynced, then renamed into
    place, all before the supervisor publishes job.json."""
    import hashlib
    import json
    from review_store import fsync_dir
    if renderer == 1:
        files = schema_inputs(path, job, result_file)
    else:
        import review_inputs
        import review_schema
        files = review_inputs.render(path, job, result_file, RUNNER_FIELDS, check_command,
                                     review_schema.describe, review_schema.skeleton)
    files["manifest.json"] = json.dumps(dict(renderer=renderer, files={
        name: hashlib.sha256(text.encode()).hexdigest() for name, text in sorted(files.items())}), indent=1) + "\n"
    staging = path / ("inputs.tmp-%d" % os.getpid())
    shutil.rmtree(staging, ignore_errors=True)
    mkdir(staging)
    directories = {staging}
    for name, text in files.items():
        target = staging / name
        if target.parent != staging:
            mkdir(target.parent)
            directories.add(target.parent)
        with open(target, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    for directory in sorted(directories, key=lambda d: -len(d.parts)):
        fsync_dir(directory)
    os.rename(staging, path / "inputs")
    fsync_dir(path)


# files every inputs/ of a renderer must hold, whatever the pass
REQUIRED = {1: {"README.md", "schema.md", "result-skeleton.json"},
            2: {"README.md", "schema.md", "result-skeleton.json", "facts.md", "previous.md"}}


def verify_inputs(path, renderer=None):
    """Whether inputs/ is exactly what its manifest says, made by this
    renderer, and holds what that renderer always writes: every listed file
    present with its digest, no other file. None when there is no inputs/
    and none was expected."""
    import hashlib
    import json
    inputs = path / "inputs"
    if not inputs.exists():
        return None if not renderer else False
    try:
        manifest = json.loads((inputs / "manifest.json").read_text())
        listed = manifest["files"]
        if renderer and manifest.get("renderer") != renderer:
            return False
        if renderer and not REQUIRED.get(renderer, set()) <= set(listed):
            return False
        present = {str(p.relative_to(inputs)) for p in inputs.rglob("*") if p.is_file()} - {"manifest.json"}
        return present == set(listed) and all(
            hashlib.sha256((inputs / name).read_bytes()).hexdigest() == digest for name, digest in listed.items())
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def cleanup(store, job):
    if not job.get("worktree"):
        return
    lock = acquire(store.locks, "refresh", time.monotonic() + 5, shared=True)
    if lock is None:
        return
    with lock:
        # An ardupilot worktree holds every submodule checkout; on a busy box
        # removing it took longer than the twenty seconds it was given, and
        # the failure was fatal to an attempt whose review had succeeded.
        try:
            subprocess.run(
                ["git", "-C", job["reference_clone"], "worktree", "remove", "--force", job["worktree"]],
                capture_output=True,
                timeout=600,
                check=True,
                pass_fds=(lock.fd,),
            )
        except (subprocess.SubprocessError, OSError):
            shutil.rmtree(job["worktree"], ignore_errors=True)
            subprocess.run(["git", "-C", job["reference_clone"], "worktree", "prune"],
                           capture_output=True, timeout=120, pass_fds=(lock.fd,))


def main():
    import json

    directory = Path(os.environ["REVIEW_JOB_DIR"])
    job = json.loads((directory / "job.json").read_text())
    # a pass never runs on inputs that are not exactly what was rendered,
    # nor without the inputs its presentation needs
    import review_presentation
    try:
        renderer = review_presentation.normalise(job.get("presentation"))["renderer"]
    except ValueError:
        renderer = -1                   # a presentation this code cannot run
    if verify_inputs(directory, renderer) is False or renderer == -1:
        print("inputs/ does not match its manifest", file=sys.stderr)
        sys.exit(2)
    os.chdir(job["worktree"])
    # Prefetch dependencies before any test enters an isolated network namespace.
    init_submodules(job["worktree"], job["repository"] == "ardupilot/ardupilot", job.get("reference_clone"))
    os.execvpe(job["cli_command"][0], job["cli_command"], os.environ)


def local_submodules(reference):
    """Every submodule the reference clone has checked out, nested ones
    included, as git -c url rewrites from its upstream URL to the local copy."""
    if not reference:
        return []
    out = subprocess.run(
        ["git", "submodule", "foreach", "--quiet", "--recursive",
         'echo "$toplevel/$sm_path $(git config --get remote.origin.url)"'],
        cwd=reference, capture_output=True, text=True, timeout=60)
    rewrites = []
    for line in out.stdout.splitlines():
        local, _, url = line.rpartition(" ")
        if url and local and Path(local).is_dir():
            rewrites += ["-c", "url.%s.insteadOf=%s" % (local, url)]
    return rewrites


def init_submodules(worktree, recursive, reference=None):
    if not (Path(worktree) / ".gitmodules").exists():
        return
    # Worktrees share the reference clone's config. A reviewer once pointed
    # every submodule URL at its own worktree; when that worktree went, every
    # later attempt failed here. Put the URLs back from .gitmodules first.
    subprocess.run(["git", "submodule", "sync", "--quiet"], cwd=worktree, check=True, timeout=60)
    # Clone them from the local reference, not GitHub: eight passes cloning
    # ChibiOS from the network at once ran past the time limit.
    command = [
        "git",
        *local_submodules(reference),
        "-c",
        "protocol.file.allow=always",
        "-c",
        "submodule.alternateLocation=superproject",
        "-c",
        "submodule.alternateErrorStrategy=info",
        "submodule",
        "update",
        "--init",
        "--jobs",
        "8",
    ]
    if recursive:
        command += ["--recursive"]
    subprocess.run(command, cwd=worktree, check=True, timeout=600)


if __name__ == "__main__":
    main()
