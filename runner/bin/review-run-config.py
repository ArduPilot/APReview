#!/usr/bin/env python3
"""Freeze the wrapper's selected settings, never credential contents."""
import argparse
import json
import os
from pathlib import Path
import shlex
import uuid
import tomllib

import repos
import review_presentation
from review_routing import load, route
from review_store import atomic


def clone_dir(entry):
    return entry.get("clone_dir") or entry["repo"].split("/")[1]


def reference_clones(references, repositories):
    """Every swept repository's reference clone: the listed ones, plus the
    main repository's ArduPilot-owned submodules, which discovery sweeps from
    .gitmodules and which repos.json does not list. Read from the main
    reference clone's .gitmodules, so no network is needed here. A listed
    entry wins over a submodule of the same name, as it does in discovery."""
    main = next(r for r in repositories["repos"] if r.get("discovery") == "main")
    modules = references / clone_dir(main) / ".gitmodules"
    clones = {}
    if modules.is_file():
        for repo in repos.submodules(modules.read_text(), repositories):
            clones[repo.lower()] = str(references / repo.split("/")[1])
    clones.update({r["repo"].lower(): str(references / clone_dir(r)) for r in repositories["repos"]})
    # A swept repository without a clone has every changed PR deferred, one
    # by one, with nothing that says why: refuse to start instead.
    missing = sorted(name for name, path in clones.items() if not Path(path).is_dir())
    if missing:
        raise ValueError("no reference clone under %s for %s" % (references, ", ".join(missing)))
    return clones


def freeze(args):
    env = os.environ
    root = Path(env["REVIEW_ROOT"])
    routing, repositories = load(root), repos.load()
    if route(args.mode, routing, repositories) != "new":
        raise ValueError("target is old-owned")
    codex_path = Path(args.codex_home) / "config.toml"
    codex = tomllib.loads(codex_path.read_text()) if codex_path.exists() else {}
    codex_model = env.get("REVIEW_CODEX_MODEL") or codex.get("model")
    if not codex_model:
        raise ValueError("pin model in the selected Codex config.toml or REVIEW_CODEX_MODEL")
    providers = {}
    for tool in ("claude", "codex"):
        home = str(Path(getattr(args, tool + "_home")).resolve())
        providers[tool] = dict(
            # concurrent sessions on one login were tested safe on 2026-09-30,
            # token-refresh race included; the cap is the only bound
            account=home, home=home, exclusive_account=False,
            account_slots=int(env.get("REVIEW_ACCOUNT_SLOTS", "8")),
            # a Codex pass the content filter declined is retried on these
            **(dict(fallback_model=env.get("REVIEW_CODEX_FALLBACK_MODEL", "gpt-6-sol"),
                    fallback_effort=env.get("REVIEW_CODEX_FALLBACK_EFFORT", "medium"))
               if tool == "codex" else {}),
            model=args.model if tool == "claude" else codex_model,
            effort="high" if tool == "claude" else env.get("REVIEW_CODEX_EFFORT", codex.get("model_reasoning_effort", "high")),
            permission_mode="auto" if tool == "claude" else codex.get("sandbox_mode", "workspace-write"),
            granted_directories=[str(root), *(codex.get("sandbox_workspace_write", {}).get("writable_roots", [])
                                              if tool == "codex" else [])],
        )
    # Separate clones are a migration invariant: old refresh/worktree users do
    # not participate in the new refresh region.
    references = Path(env.get("REVIEW_NEW_REPOS", str(Path(env["REVIEW_DATA"]) / "references")))
    # review-env adds the legacy base clone's Tools/autotest. The adapter only
    # knows its separate new reference clone, so remove both trees here before
    # it prepends the pinned attempt's Tools/autotest.
    base_tools = {(base / clone_dir(r) / "Tools/autotest").resolve()
                  for base in (Path(env["REVIEW_REPOS"]), references)
                  for r in repositories["repos"]}
    path = os.pathsep.join(p for p in env["PATH"].split(os.pathsep)
                           if p and Path(p).resolve() not in base_tools)
    config = dict(
        schema=1, routing=routing, routing_root=str(root), providers=providers, repos=repositories,
        reference_clones=reference_clones(references, repositories),
        comment_accounts=json.loads(args.comments), endpoint="review",
        # Where a review's retained page lives under the endpoint. The default
        # needs a PRReviews tree on the publishing host; a site without one
        # can put it under a tree it already serves.
        retained_prefix=env.get("REVIEW_RETAINED_PREFIX", "PRReviews").strip("/"),
        endpoints={"review": dict(url=env.get("REVIEW_PUBLIC_URL", ""),
                                  publish=env.get("REVIEW_PUBLISH", ""),
                                  rsync_args=shlex.split(env.get("RSYNC_AUTH", "")))},
        github_accounts={"comment": {"id": "comment", "token_env": "GH_TOKEN"} if env.get("GH_TOKEN") else {},
                         "project": {"id": "project", "project": True}},
        github_writes=env.get("REVIEW_GITHUB_WRITES") == "1",
        project_id=env.get("REVIEW_PROJECT_ID"),
        # how passes are shown their inputs (review_presentation); legacy unless set
        presentation=review_presentation.normalise(env.get("REVIEW_PRESENTATION") or None),
        pool_size=int(env.get("REVIEW_POOL_SIZE", "8")),
        heavy_size=int(env.get("REVIEW_HEAVY_SIZE", "4")),
        permit_timeout=float(env.get("REVIEW_PERMIT_TIMEOUT", "120")),
        admission=float(env.get("REVIEW_ADMISSION", "14400")),
        wall_timeouts={kind: float(env.get("REVIEW_WALL_" + kind.upper(), seconds))
                       for kind, seconds in {"primary": 5400, "cold": 1800,
                                             "validation": 1800, "reconciliation": 2700}.items()},
        quota={"paused": args.quota != 0, "observation": args.quota_message},
        # delivery belongs to the standalone drainer (review-outbox.sh each
        # minute); the controller only enqueues
        controller_delivers=env.get("REVIEW_CONTROLLER_DELIVERS", "0") == "1",
        # followup considers only PRs reviewed or posted on within this many days
        followup_days=float(env.get("REVIEW_FOLLOWUP_DAYS", "14")),
        # REST reads over a kept-alive connection with conditional requests
        github_http=env.get("REVIEW_GITHUB_HTTP", "1") == "1",
        # a PR reviewed less than this many hours ago waits for a later run
        rereview_hours=float(env.get("REVIEW_REREVIEW_HOURS", "12")),
        path=path,
    )
    if args.dry_run:
        print(json.dumps(config, sort_keys=True))
        return
    directory = Path(env["REVIEW_DATA"]) / "runs" / (args.tag + "-" + uuid.uuid4().hex[:12])
    atomic(directory / "configuration.json", config)
    print(directory)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--claude-home", required=True)
    parser.add_argument("--codex-home", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--comments", required=True)
    parser.add_argument("--quota", type=int, default=0)
    parser.add_argument("--quota-message", default="")
    parser.add_argument("--dry-run", action="store_true")
    freeze(parser.parse_args())


if __name__ == "__main__":
    main()
