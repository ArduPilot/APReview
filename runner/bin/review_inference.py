"""Build a real CLI attempt from frozen configuration; no inference at import."""

import os
from pathlib import Path
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
    job.update(
        worktree=str(worktree),
        reference_clone=str(reference),
        account=provider["account"],
        exclusive_account=provider.get("exclusive_account", True),
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
    env["REVIEW_DATA"] = str(store.root)
    env["REVIEW_HEAVY_SIZE"] = str(config.get("heavy_size", 4))
    env["REVIEW_HEAVY_WAIT"] = str(config.get("permit_timeout", 120))
    env["CLAUDE_CONFIG_DIR" if job["provider"] == "claude" else "CODEX_HOME"] = provider["home"]
    prompt = config.get("prompts", {}).get(job["kind"])
    if prompt is None:
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
        command = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--model",
            provider["model"],
            "-c",
            'model_reasoning_effort="' + provider["effort"] + '"',
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
        subprocess.run(
            ["git", "-C", job["reference_clone"], "worktree", "remove", "--force", job["worktree"]],
            capture_output=True,
            timeout=20,
            check=True,
            pass_fds=(lock.fd,),
        )


def main():
    import json

    job = json.loads((Path(os.environ["REVIEW_JOB_DIR"]) / "job.json").read_text())
    os.chdir(job["worktree"])
    # Prefetch dependencies before any test enters an isolated network namespace.
    if (Path(job["worktree"]) / ".gitmodules").exists():
        command = [
            "git",
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
        if job["repository"] == "ardupilot/ardupilot":
            command += ["--recursive"]
        subprocess.run(command, check=True, timeout=120)
    os.execvpe(job["cli_command"][0], job["cli_command"], os.environ)


if __name__ == "__main__":
    main()
