"""Reject incomplete or foreign results before they can become accepted reviews."""
import json
import os
from pathlib import Path
import stat
import sys

IDENTITY = ("run", "job", "attempt", "generation", "repository", "node_id",
            "number", "head", "base", "merge_base", "kind")
FILES = {"primary": "review.json", "cold": "cold.json", "validation": "validate.json",
         "reconciliation": "final.json"}
MAX_BYTES = 4 * 1024 * 1024
MAX_DEPTH, MAX_STRING, MAX_ITEMS = 20, 131072, 4096


def bounded(value, depth=0):
    if depth > MAX_DEPTH:
        raise ValueError("result nesting")
    if isinstance(value, str) and len(value) > MAX_STRING:
        raise ValueError("result string too long")
    if isinstance(value, (list, dict)):
        if len(value) > MAX_ITEMS:
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
    # A complete review may well have gaps: things it could not exercise are
    # reported, and stay UNCONFIRMED. Incomplete means the review itself could
    # not be carried out, and that has to say why.
    if result["status"] == "incomplete" and not result["gaps"]:
        raise ValueError("incomplete result names no gap")
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


# What a result looks like, as data, for passes to read (describe()) and
# start from (skeleton()). validate() above stays the authority; tests check
# the two agree.
TEXT, NONEMPTY, INT, BOOL = "string", "non-empty string", "integer", "true or false"
SHAPES = {
    "evidence": {"commands": "list of command",
                 "artifacts": "list of non-empty paths relative to the job directory, without ..",
                 "configuration": TEXT},
    "command": {"command": TEXT, "exit": INT, "observed": TEXT},
    "location": {"file": NONEMPTY, "line": "integer, 1 or more", "side": ("old", "new"), "revision": NONEMPTY},
    "finding": {"id": "<pass>:<name>, e.g. primary:F1", "kind": ("BUG", "ISSUE", "NOTE"), "severity": NONEMPTY,
                "claim": NONEMPTY, "location": 'location, or {"non_line_specific": true}',
                "status": ("VERIFIED", "UNCONFIRMED"), "evidence": "evidence"},
    "previous": {"id": "a previous_ids entry", "disposition": ("RESOLVED", "STILL OPEN", "DISPUTED"),
                 "rationale": NONEMPTY},
    "validation outcome": {"id": "a primary_ids entry", "outcome": ("CONFIRM", "ADJUST", "REFUTE"),
                           "evidence": "evidence"},
    "final outcome": {"id": "a finding_ids entry", "blocking": BOOL, "actionable": BOOL,
                      "disposition": ("retained", "adjusted", "refuted", "merged"),
                      "rationale": "string; non-empty unless retained", "evidence": "evidence",
                      "target": "optional; required when merged: the surviving finding's id"},
}
COMMON = {"schema": "1", **{k: "copied exactly from the job, same JSON type (result-skeleton.json has them)"
                             for k in IDENTITY},
          "status": ("complete", "incomplete"), "gaps": "list of non-empty strings",
          "heavy": BOOL}
VERDICTS = ("ACCEPT", "COMMENT", "REQUEST CHANGES")     # APPROVE is read as ACCEPT
RESULTS = {
    "primary": {"verdict": VERDICTS, "findings": "list of finding",
                "clean": "list of strings", "previous": "list of previous, one per previous_ids entry"},
    "validation": {"outcomes": "list of validation outcome, one per primary_ids entry",
                   "new": "list of finding (ids validation:...)"},
    "reconciliation": {"verdict": VERDICTS,
                       "outcomes": "list of final outcome, one per finding_ids entry",
                       "section_md": NONEMPTY, "comment_md": NONEMPTY, "summary": NONEMPTY},
}
RESULTS["cold"] = RESULTS["primary"]
LIMITS = ("No other fields: unknown fields are rejected, in nested objects too.",
          "Finding and outcome ids are unique within their list.",
          "Strings at most %d characters, lists and objects at most %d entries, nesting at most %d deep, "
          "and the whole file at most %d bytes." % (MAX_STRING, MAX_ITEMS, MAX_DEPTH, MAX_BYTES),
          "status incomplete needs at least one gap saying why.")
RULES = {
    "primary": [*LIMITS, "Every previous_ids entry gets exactly one previous disposition."],
    "validation": [*LIMITS, "Every primary_ids entry gets exactly one outcome."],
    "reconciliation": [*LIMITS,
                       "Only a complete reconciliation is accepted: incomplete passes the check but is never used.",
                       "Every finding_ids entry gets exactly one outcome; merged ones name a target, without cycles.",
                       "verdict follows from the outcomes not refuted or merged: any blocking gives "
                       "REQUEST CHANGES, else any actionable gives COMMENT, else ACCEPT."],
}
for kind in ("primary", "reconciliation"):
    RULES[kind].append('verdict "APPROVE" is read as "ACCEPT".')
RULES["cold"] = RULES["primary"]


def describe(kind):
    """schema.md for one pass kind: its fields, then the shapes they use."""
    def line(name, spec):
        if isinstance(spec, tuple):
            spec = "one of " + ", ".join('"%s"' % v for v in spec)
        return "- `%s`: %s" % (name, spec)
    out = ["# The %s result (%s)" % (kind, FILES[kind]), "",
           "Fields, all required unless marked optional:", ""]
    out += [line(k, v) for k, v in {**COMMON, **RESULTS[kind]}.items()]
    out += ["", "Rules:", ""] + ["- " + rule for rule in RULES[kind]]
    review = ("finding", "previous", "location", "evidence", "command")
    used = {"primary": review, "cold": review,
            "validation": ("validation outcome", "finding", "location", "evidence", "command"),
            "reconciliation": ("final outcome", "evidence", "command")}[kind]
    for shape in used:
        out += ["", "## " + shape, ""] + [line(k, v) for k, v in SHAPES[shape].items()]
    return "\n".join(out) + "\n"


def skeleton(job):
    """A result to start from: identity filled in, one entry per id the pass
    must cover, and every decision left null or empty, so it fails validation
    until the pass has made them all."""
    result = {k: job[k] for k in IDENTITY}
    result.update(schema=1, status=None, gaps=[], heavy=None)
    kind = job["kind"]
    if kind in ("primary", "cold"):
        result.update(verdict=None, findings=[], clean=[],
                      previous=[{"id": i, "disposition": None, "rationale": ""} for i in job.get("previous_ids", [])])
    elif kind == "validation":
        result.update(outcomes=[{"id": i, "outcome": None, "evidence": None} for i in job.get("primary_ids", [])],
                      new=[])
    else:
        result.update(verdict=None, section_md="", comment_md="", summary="",
                      outcomes=[{"id": i, "blocking": None, "actionable": None, "disposition": None,
                                 "rationale": "", "evidence": None} for i in job.get("finding_ids", [])])
    return result


def main(argv):
    """review_schema.py check RESULT [JOB]: validate a result file against its
    job (job.json in the result's directory unless given)."""
    if len(argv) not in (2, 3) or argv[0] != "check":
        print("usage: review_schema.py check RESULT [JOB]", file=sys.stderr)
        return 2
    result = Path(argv[1])
    job_path = Path(argv[2]) if len(argv) == 3 else result.parent / "job.json"
    try:
        job = json.loads(job_path.read_text())
        read_result(result, job)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("invalid: %s" % error)
        return 1
    print("valid")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
