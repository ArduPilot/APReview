"""The ownership boundary shared by shell admission and deterministic discovery.

Absence means old ownership. A present but unreadable/invalid file is fatal.
Repository ownership covers every mode; labels and modes transfer their whole
destination, never ownership inferred from a PR's mutable labels.
"""
import os
from pathlib import Path
import re

from review_store import read

DEFAULT = {"schema": 1, "repositories": [], "labels": [], "modes": []}
MODES = {"all", "followup", "rsync", "pr", "author"}


def validate(value):
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ValueError("unknown routing schema")
    if set(value) - set(DEFAULT):
        raise ValueError("unknown routing field")
    for key in ("repositories", "labels", "modes"):
        items = value.get(key)
        if not isinstance(items, list) or any(not isinstance(x, str) or not x for x in items):
            raise ValueError("routing needs a string array: " + key)
    if set(value["modes"]) - MODES:
        raise ValueError("unknown routing mode")
    if any(not re.fullmatch(r"[\w.-]+/[\w.-]+", x) for x in value["repositories"]):
        raise ValueError("routing repository must be owner/repo")
    return {**value, "repositories": sorted({x.lower() for x in value["repositories"]})}


def load(root=None):
    path = Path(root or os.environ["REVIEW_ROOT"]) / "etc/routing.json"
    if not os.path.lexists(path):
        return validate(DEFAULT)
    return validate(read(path))


def target(argument, config):
    value = argument.strip()
    reserved = value.lstrip("/-").lower()
    if reserved in ("all", "followup", "rsync"):
        return reserved, "rsyncproject/rsync" if reserved == "rsync" else None
    match = re.fullmatch(r"https?://github.com/([^/]+/[^/]+)/pull/(\d+)(?:[/?#].*)?", value)
    if match:
        return "pr", match[1].lower()
    match = re.fullmatch(r"(?:(.*?)#)?(\d+)", value)
    if match:
        name = match[1]
        if not name:
            name = next(r["repo"] for r in config["repos"] if r.get("discovery") == "main")
        elif "/" not in name:
            choices = [r["repo"] for r in config["repos"] if r.get("key") == name]
            if not choices:
                choices = [r["repo"] for r in config["repos"] if r["repo"].split("/")[1] == name]
            if len(choices) != 1:
                raise ValueError("unknown or ambiguous PR repository")
            name = choices[0]
        return "pr", name.lower()
    return ("author", None) if value.startswith("@") else (value, None)


def owner(routing, mode, repository=None):
    if mode.startswith("@"):
        mode = "author"
    if repository:
        # A transferred report mode never authorizes old-owned PRs. Otherwise
        # a new label review and an old manual/followup review could overlap.
        return "new" if repository.lower() in routing["repositories"] or "all" in routing["modes"] else "old"
    if "all" in routing["modes"] or mode in routing["modes"] or mode in routing["labels"]:
        return "new"
    return "old"


def route(argument, routing, config):
    mode, repo = target(argument, config)
    if (repo is None and mode not in MODES and mode not in routing["labels"]
            and mode not in ("DevCallTopic", "DevCallEU", "AIReview")
            and "author" in routing["modes"] and "all" not in routing["modes"]):
        # Bare names preserve the command's label-before-author precedence.
        # Only this mixed-routing ambiguity needs a bounded read-only lookup;
        # the default old route and explicit @author never contact GitHub.
        from review_discovery import Discovery
        from review_github import GitHub
        resolved = Discovery(GitHub(), {"repos": config, "labels": routing["labels"]}).resolve(argument)
        mode, repo = target(resolved, config)
    return owner(routing, mode, repo)


def candidates(rows, routing, mode, path):
    return [row for row in rows if owner(routing, mode, row["repository"]) == path]
