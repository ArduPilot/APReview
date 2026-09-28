"""Reject incomplete or foreign results before they can become accepted reviews."""
import json
import os
from pathlib import Path
import stat

IDENTITY = ("run", "job", "attempt", "generation", "repository", "node_id",
            "number", "head", "base", "merge_base", "kind")
FILES = {"primary": "review.json", "cold": "cold.json", "validation": "validate.json",
         "reconciliation": "final.json"}
MAX_BYTES = 4 * 1024 * 1024


def bounded(value, depth=0):
    if depth > 20:
        raise ValueError("result nesting")
    if isinstance(value, str) and len(value) > 131072:
        raise ValueError("result string too long")
    if isinstance(value, (list, dict)):
        if len(value) > 4096:
            raise ValueError("result collection too long")
        for item in (value.values() if isinstance(value, dict) else value):
            bounded(item, depth + 1)


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError("missing or unknown fields")


def unique(items):
    if not isinstance(items, list) or not all(isinstance(x, dict) and isinstance(x.get("id"), str) for x in items):
        raise ValueError("finding list")
    ids = [x["id"] for x in items]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate finding")
    return set(ids)


def evidence(value):
    fields(value, ("commands", "artifacts", "configuration"))
    if not isinstance(value["commands"], list) or not isinstance(value["artifacts"], list) or not isinstance(value["configuration"], str):
        raise ValueError("evidence shape")
    for command in value["commands"]:
        fields(command, ("command", "exit", "observed"))
        if type(command["exit"]) is not int or not all(isinstance(command[k], str) for k in ("command", "observed")):
            raise ValueError("command evidence")
    if not all(isinstance(p, str) and p and not Path(p).is_absolute() and ".." not in Path(p).parts for p in value["artifacts"]):
        raise ValueError("unsafe evidence reference")


def finding(value, prefix):
    fields(value, ("id", "kind", "severity", "claim", "location", "status", "evidence"))
    if not value["id"].startswith(prefix + ":") or not value["id"][len(prefix) + 1:]:
        raise ValueError("finding namespace")
    if value["kind"] not in ("BUG", "ISSUE", "NOTE") or value["status"] not in ("VERIFIED", "UNCONFIRMED"):
        raise ValueError("finding enum")
    if not all(isinstance(value[k], str) and value[k] for k in ("severity", "claim")):
        raise ValueError("finding rationale")
    location = value["location"]
    if location != {"non_line_specific": True}:
        fields(location, ("file", "line", "side", "revision"))
        if type(location["line"]) is not int or location["line"] < 1 or location["side"] not in ("old", "new") or not all(isinstance(location[k], str) and location[k] for k in ("file", "revision")):
            raise ValueError("finding location")
    evidence(value["evidence"])


def validate(result, job):
    bounded(result)
    common = ("schema", *IDENTITY, "status", "gaps", "heavy")
    kind = job["kind"]
    extra = {"primary": ("verdict", "findings", "clean", "previous"),
             "cold": ("verdict", "findings", "clean", "previous"),
             "validation": ("outcomes", "new"),
             "reconciliation": ("verdict", "outcomes", "section_md", "comment_md", "summary")}[kind]
    fields(result, common + extra)
    if result["schema"] != 1 or any(type(result[k]) is not type(job[k]) or result[k] != job[k] for k in IDENTITY):
        raise ValueError("result identity")
    if result["status"] not in ("complete", "incomplete") or type(result["heavy"]) is not bool or not isinstance(result["gaps"], list) or not all(isinstance(g, str) and g for g in result["gaps"]):
        raise ValueError("result status")
    if (result["status"] == "complete") == bool(result["gaps"]):
        raise ValueError("result gaps")
    if "verdict" in result:
        if result["verdict"] == "APPROVE":
            result["verdict"] = "ACCEPT"
        if result["verdict"] not in ("ACCEPT", "COMMENT", "REQUEST CHANGES"):
            raise ValueError("verdict")
    if kind in ("primary", "cold"):
        unique(result["findings"])
        for item in result["findings"]:
            finding(item, kind)
        if not isinstance(result["clean"], list) or not all(isinstance(x, str) for x in result["clean"]):
            raise ValueError("clean list")
        if unique(result["previous"]) != set(job.get("previous_ids", [])):
            raise ValueError("previous finding coverage")
        for item in result["previous"]:
            fields(item, ("id", "disposition", "rationale"))
            if item["disposition"] not in ("RESOLVED", "STILL OPEN", "DISPUTED") or not isinstance(item["rationale"], str) or not item["rationale"]:
                raise ValueError("previous finding disposition")
    elif kind == "validation":
        if unique(result["outcomes"]) != set(job["primary_ids"]):
            raise ValueError("validation coverage")
        for item in result["outcomes"]:
            fields(item, ("id", "outcome", "evidence"))
            if item["outcome"] not in ("CONFIRM", "ADJUST", "REFUTE"):
                raise ValueError("validation outcome")
            evidence(item["evidence"])
        unique(result["new"])
        for item in result["new"]:
            finding(item, "validation")
    else:
        ids = unique(result["outcomes"])
        if ids != set(job["finding_ids"]):
            raise ValueError("reconciliation coverage")
        outcomes = {item["id"]: item for item in result["outcomes"]}
        for item in result["outcomes"]:
            fields(item, ("id", "blocking", "actionable", "disposition", "rationale", "evidence"), ("target",))
            if type(item["blocking"]) is not bool or type(item["actionable"]) is not bool or item["disposition"] not in ("retained", "adjusted", "refuted", "merged"):
                raise ValueError("final disposition")
            evidence(item["evidence"])
            if not isinstance(item["rationale"], str) or (item["disposition"] != "retained" and not item["rationale"]):
                raise ValueError("withdrawal rationale")
            seen = {item["id"]}
            while item["disposition"] == "merged":
                target = item.get("target")
                if target not in ids or target in seen:
                    raise ValueError("merge target or cycle")
                seen.add(target)
                item = outcomes[target]
        blocking = any(x["blocking"] and x["disposition"] not in ("refuted", "merged") for x in outcomes.values())
        actionable = any(x["actionable"] and x["disposition"] not in ("refuted", "merged") for x in outcomes.values())
        expected = "REQUEST CHANGES" if blocking else "COMMENT" if actionable else "ACCEPT"
        if result["verdict"] != expected:
            raise ValueError("verdict contradicts findings")
        if not all(isinstance(result[k], str) and result[k] for k in ("section_md", "comment_md", "summary")):
            raise ValueError("missing prose")
    return result


def read_result(path, job):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("result is not a bounded regular file")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("result too large")
    def pairs(items):
        value = dict(items)
        if len(value) != len(items):
            raise ValueError("duplicate JSON field")
        return value
    def constant(value):
        raise ValueError("non-finite JSON")
    return validate(json.loads(raw, object_pairs_hook=pairs, parse_constant=constant), job)


def evidence_paths(value):
    if isinstance(value, dict):
        if "artifacts" in value:
            yield from value["artifacts"]
        for child in value.values():
            yield from evidence_paths(child)
    elif isinstance(value, list):
        for child in value:
            yield from evidence_paths(child)


def canned(job):
    result = {k: job[k] for k in IDENTITY}
    result.update(schema=1, status="complete", gaps=[], heavy=False)
    if job["kind"] in ("primary", "cold"):
        result.update(verdict="ACCEPT", findings=[], clean=["stub"],
                      previous=[{"id": ident, "disposition": "RESOLVED", "rationale": "stub"}
                                for ident in job.get("previous_ids", [])])
    elif job["kind"] == "validation":
        result.update(outcomes=[], new=[])
    else:
        result.update(verdict="ACCEPT", outcomes=[
            {"id": ident, "blocking": False, "actionable": False, "disposition": "refuted",
             "rationale": "stub", "evidence": {"commands": [], "artifacts": [], "configuration": "stub"}}
            for ident in job.get("finding_ids", [])], section_md="Stub review.",
                      comment_md="Stub review.", summary="Stub complete")
    return result
