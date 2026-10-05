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
    prompt = config.get("prompts", {}).get(job["kind"])
    if prompt is None:
        import review_presentation
        # only a legacy run may fall back to the live prompt files
        if not review_presentation.legacy(config.get("presentation")):
            raise KeyError("prompt for %s not frozen in the run" % job["kind"])
        prompt = (COMMANDS / ("review-" + PROMPTS[job["kind"]] + ".md")).read_text()
    prompt += (
        "\nRead "
        + str(path / "job.json")
        + ". Write "
        + str(path / __import__("review_schema").FILES[job["kind"]])
        + ".\n"
    )
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

    job = json.loads((Path(os.environ["REVIEW_JOB_DIR"]) / "job.json").read_text())
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
