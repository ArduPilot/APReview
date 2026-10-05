import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import review_schema as rs  # noqa: E402

BASE = os.path.join(os.environ.get("REVIEW_TEST_DIR", "/data/review"), "supervisor-tests")
EVIDENCE = {"commands": [{"command": "make", "exit": 0, "observed": "ok"}], "artifacts": ["evidence/x.log"],
            "configuration": "sitl"}


def job(kind):
    j = dict(run="/runs/r", job="pr:o/r#1:" + kind, attempt="a1", generation=2, repository="o/r",
             node_id="N", number=1, head="h" * 40, base="b" * 40, merge_base="m" * 40, kind=kind,
             previous_ids=["previous:1"], primary_ids=["primary:F1"], finding_ids=["primary:F1", "previous:1"])
    return j


def finding(prefix):
    return {"id": prefix + ":F1", "kind": "BUG", "severity": "high", "claim": "it breaks",
            "location": {"file": "a.c", "line": 3, "side": "new", "revision": "h" * 40},
            "status": "VERIFIED", "evidence": copy.deepcopy(EVIDENCE)}


def example(kind):
    """A full, valid result built from the description's fields."""
    j = job(kind)
    result = {k: j[k] for k in rs.IDENTITY}
    result.update(schema=1, status="complete", gaps=[], heavy=False)
    if kind in ("primary", "cold"):
        result.update(verdict="REQUEST CHANGES", findings=[finding(kind)], clean=["builds"],
                      previous=[{"id": "previous:1", "disposition": "STILL OPEN", "rationale": "still there"}])
    elif kind == "validation":
        result.update(outcomes=[{"id": "primary:F1", "outcome": "CONFIRM", "evidence": copy.deepcopy(EVIDENCE)}],
                      new=[finding("validation")])
    else:
        result.update(verdict="REQUEST CHANGES", section_md="s", comment_md="c", summary="one line", outcomes=[
            {"id": "primary:F1", "blocking": True, "actionable": True, "disposition": "retained",
             "rationale": "", "evidence": copy.deepcopy(EVIDENCE)},
            {"id": "previous:1", "blocking": False, "actionable": False, "disposition": "merged",
             "rationale": "same bug", "evidence": copy.deepcopy(EVIDENCE), "target": "primary:F1"}])
    return result, j


# the rationale rule as described; test_a_rationale_is_needed_unless_retained checks it
RATIONALE = "string; non-empty unless retained"
OPTIONAL = {"final outcome": {"target"}}


def list_shape(text):
    """The shape a "list of <shape>..." description names, or None."""
    rest = text[len("list of "):] if text.startswith("list of ") else ""
    return next((name for name in sorted(rs.SHAPES, key=len, reverse=True) if rest.startswith(name)), None)


def verdict(outcomes):
    """The reconciliation verdict the description's rule gives."""
    live = [x for x in outcomes if x["disposition"] not in ("refuted", "merged")]
    return ("REQUEST CHANGES" if any(x["blocking"] for x in live)
            else "COMMENT" if any(x["actionable"] for x in live) else "ACCEPT")


def valid(result, j):
    try:
        rs.validate(copy.deepcopy(result), j)
        return True
    except (ValueError, TypeError, KeyError):
        return False


class Parity(unittest.TestCase):
    """The description passes read and the validator agree."""

    def test_examples_built_from_the_description_are_valid(self):
        for kind in rs.FILES:
            self.assertTrue(valid(*example(kind)), kind)

    def test_every_described_field_is_required_and_no_other_is_allowed(self):
        for kind in rs.FILES:
            result, j = example(kind)
            self.assertEqual(set(result), set(rs.COMMON) | set(rs.RESULTS[kind]), kind)
            for field in result:
                broken = dict(result)
                del broken[field]
                self.assertFalse(valid(broken, j), (kind, field))
            self.assertFalse(valid(dict(result, extra=1), j), kind)

    def test_top_level_enumerations_match(self):
        for kind in rs.FILES:
            result, j = example(kind)
            for field, spec in {**rs.COMMON, **rs.RESULTS[kind]}.items():
                if not isinstance(spec, tuple):
                    continue
                self.assertFalse(valid(dict(result, **{field: "NOT-A-VALUE"}), j), (kind, field))
                for value in spec:
                    if field == "verdict" and kind == "reconciliation":
                        continue        # follows from the outcomes, checked by the rules
                    if field == "status" and value == "incomplete":
                        self.assertFalse(valid(dict(result, status=value), j))      # needs a gap
                        self.assertTrue(valid(dict(result, status=value, gaps=["why"]), j), kind)
                        continue
                    self.assertTrue(valid(dict(result, **{field: value}), j), (kind, field, value))

    def nested(self, kind, path):
        """The object at path in a fresh example, and the example."""
        result, j = example(kind)
        target = result
        for step in path:
            target = target[step]
        return result, j, target

    def check_shape(self, kind, path, shape, optional=()):
        result, j, target = self.nested(kind, path)
        self.assertEqual(set(target) | set(optional), set(rs.SHAPES[shape]), (kind, shape))
        for field in list(target):
            result, j, target = self.nested(kind, path)
            del target[field]
            if field not in optional:
                self.assertFalse(valid(result, j), (kind, shape, field))
        result, j, target = self.nested(kind, path)
        target["extra"] = 1
        self.assertFalse(valid(result, j), (kind, shape))
        for field, spec in rs.SHAPES[shape].items():
            if isinstance(spec, tuple) and field in target:
                for value in spec:
                    result, j, target = self.nested(kind, path)
                    target[field] = value
                    if field == "disposition" and value == "merged":
                        target["target"] = "previous:1" if target["id"] != "previous:1" else "primary:F1"
                        for other in result.get("outcomes", []):
                            if other is not target and other.get("target") == target["id"]:
                                other["disposition"] = "retained"       # no merge cycle
                                del other["target"]
                    if field == "disposition" and value != "retained":
                        target["rationale"] = "why"
                    if kind == "reconciliation":
                        result["verdict"] = verdict(result["outcomes"])
                    self.assertTrue(valid(result, j), (kind, shape, field, value))
                result, j, target = self.nested(kind, path)
                target[field] = "NOT-A-VALUE"
                self.assertFalse(valid(result, j), (kind, shape, field))

    def test_nested_shapes_match(self):
        self.check_shape("primary", ("findings", 0), "finding")
        self.check_shape("primary", ("findings", 0, "location"), "location")
        self.check_shape("primary", ("findings", 0, "evidence"), "evidence")
        self.check_shape("primary", ("findings", 0, "evidence", "commands", 0), "command")
        self.check_shape("primary", ("previous", 0), "previous")
        self.check_shape("validation", ("outcomes", 0), "validation outcome")
        self.check_shape("reconciliation", ("outcomes", 0), "final outcome", optional=("target",))

    # for each type a description names: a value it allows, and ones it does not
    TYPES = {rs.TEXT: ("x", (5, None)), rs.NONEMPTY: ("x", ("", 5, None)), rs.INT: (2, ("2", None, 2.5)),
             "integer, 1 or more": (2, (0, -1, "2", None)), rs.BOOL: (False, ("yes", 0, None))}

    # list descriptions: an element they allow and ones they do not
    ELEMENTS = {"list of strings": ("x", (5, None)), "list of non-empty strings": ("x", ("", 5, None)),
                "list of non-empty paths relative to the job directory, without ..": ("a/b", ("", 5, "/a", "a/../b"))}

    def check_types(self, kind, path, spec):
        for field, text in spec.items():
            text = str(text)
            if text in self.ELEMENTS:
                good, bads = self.ELEMENTS[text]
                result, j, target = self.nested(kind, path)
                target[field] = [good]
                self.assertTrue(valid(result, j), (kind, path, field, good))
                for bad in bads:
                    result, j, target = self.nested(kind, path)
                    target[field] = [bad]
                    self.assertFalse(valid(result, j), (kind, path, field, bad))
            elif text.startswith("list of"):
                # a list of a shape: its elements are that shape, and anything
                # but an object is not an element
                shape = list_shape(text)
                self.assertTrue(shape, (kind, path, field, text))
                result, j, target = self.nested(kind, path)
                self.assertTrue(target[field], (kind, path, field))       # the example must exercise it
                for element in target[field]:
                    self.assertEqual(set(element) | OPTIONAL.get(shape, set()), set(rs.SHAPES[shape]),
                                     (kind, path, field, shape))
                if target[field]:
                    target[field] = [5]
                    self.assertFalse(valid(result, j), (kind, path, field))
            elif text.split(",")[0] in rs.SHAPES:
                # a field described by a shape: its value is that shape, and
                # not an object is wrong
                shape = text.split(",")[0]
                result, j, target = self.nested(kind, path)
                self.assertEqual(set(target[field]) | OPTIONAL.get(shape, set()), set(rs.SHAPES[shape]),
                                 (kind, path, field, shape))
                for bad in ("x", 5, None, []):
                    result, j, target = self.nested(kind, path)
                    target[field] = bad
                    self.assertFalse(valid(result, j), (kind, path, field, bad))
            if text not in self.TYPES and not text.startswith("list of"):
                continue
            if text in self.ELEMENTS:
                continue
            good, bads = self.TYPES.get(text, (None, ("x", 5, None)))
            if good is not None:
                result, j, target = self.nested(kind, path)
                target[field] = good
                if kind == "reconciliation":
                    result["verdict"] = verdict(result["outcomes"])
                self.assertTrue(valid(result, j), (kind, path, field, good))
            for bad in bads:
                result, j, target = self.nested(kind, path)
                target[field] = bad
                self.assertFalse(valid(result, j), (kind, path, field, bad))

    def test_described_types_are_the_validators(self):
        for kind in rs.FILES:
            self.check_types(kind, (), {**rs.COMMON, **rs.RESULTS[kind]})
        self.check_types("primary", ("findings", 0), rs.SHAPES["finding"])
        self.check_types("primary", ("findings", 0, "location"), rs.SHAPES["location"])
        self.check_types("primary", ("findings", 0, "evidence"), rs.SHAPES["evidence"])
        self.check_types("primary", ("findings", 0, "evidence", "commands", 0), rs.SHAPES["command"])
        self.check_types("primary", ("previous", 0), rs.SHAPES["previous"])
        self.check_types("reconciliation", ("outcomes", 0), rs.SHAPES["final outcome"])
        self.check_types("validation", ("outcomes", 0), rs.SHAPES["validation outcome"])
        # a location may also be the non-line-specific marker the description names
        result, j = example("primary")
        result["findings"][0]["location"] = {"non_line_specific": True}
        self.assertTrue(valid(result, j))

    def test_every_description_is_one_the_tests_check(self):
        known = set(self.TYPES) | set(self.ELEMENTS)
        for spec in [rs.COMMON, *rs.RESULTS.values(), *rs.SHAPES.values()]:
            for field, text in spec.items():
                text = str(text)
                ok = (isinstance(spec[field], tuple) or text in known or list_shape(text)
                      or text.split(",")[0] in rs.SHAPES or field in ("id", "schema", "target")
                      or text.startswith("copied exactly")
                      or (field, text) == ("rationale", RATIONALE))       # test_a_rationale_is_needed_unless_retained
                self.assertTrue(ok, (field, text))

    def test_a_rationale_is_needed_unless_retained(self):
        self.assertEqual(rs.SHAPES["final outcome"]["rationale"], RATIONALE)
        for disposition, rationale, ok in (("retained", "", True), ("adjusted", "", False), ("refuted", "", False),
                                           ("adjusted", "why", True), ("retained", 5, False)):
            result, j, target = self.nested("reconciliation", ("outcomes", 0))
            target.update(disposition=disposition, rationale=rationale)
            result["verdict"] = verdict(result["outcomes"])
            self.assertEqual(valid(result, j), ok, (disposition, rationale))

    def test_described_limits_and_rules_are_the_validators(self):
        text = rs.describe("primary")
        for limit in (rs.MAX_STRING, rs.MAX_ITEMS, rs.MAX_DEPTH, rs.MAX_BYTES):
            self.assertIn(str(limit), text)
        result, j = example("primary")
        self.assertFalse(valid(dict(result, findings=[finding("primary"), finding("primary")]), j))   # unique ids
        for bad in ("", "/abs/x", "../x", "a/../b"):
            broken = copy.deepcopy(result)
            broken["findings"][0]["evidence"]["artifacts"] = [bad]
            self.assertFalse(valid(broken, j), bad)
        self.assertFalse(valid(dict(result, clean=["x" * (rs.MAX_STRING + 1)]), j))
        self.assertTrue(valid(dict(result, clean=["x" * rs.MAX_STRING]), j))
        self.assertFalse(valid(dict(result, clean=["x"] * (rs.MAX_ITEMS + 1)), j))
        self.assertTrue(valid(dict(result, clean=["x"] * rs.MAX_ITEMS), j))
        self.assertTrue(valid(dict(result, verdict="APPROVE"), j))
        # a reconciliation passes the check incomplete, as described; selection refuses it elsewhere
        final, fj = example("reconciliation")
        self.assertTrue(valid(dict(final, status="incomplete", gaps=["why"]), fj))
        self.assertIn("never used", rs.describe("reconciliation"))

    def test_the_verdict_rule_is_the_validators(self):
        result, j = example("reconciliation")
        for blocking, actionable in ((True, True), (False, True), (False, False)):
            outcomes = result["outcomes"]
            outcomes[0].update(blocking=blocking, actionable=actionable)
            for value in rs.RESULTS["reconciliation"]["verdict"]:
                self.assertEqual(valid(dict(result, verdict=value), j), value == verdict(outcomes))

    def test_descriptions_render_for_every_kind(self):
        for kind in rs.FILES:
            text = rs.describe(kind)
            for field in {**rs.COMMON, **rs.RESULTS[kind]}:
                self.assertIn("`%s`" % field, text)


class Skeleton(unittest.TestCase):
    def test_a_skeleton_fails_until_every_decision_is_made(self):
        for kind in rs.FILES:
            for obligations in (True, False):
                j = job(kind)
                if not obligations:
                    j.update(previous_ids=[], primary_ids=[], finding_ids=[])
                skeleton = rs.skeleton(j)
                self.assertFalse(valid(skeleton, j), (kind, obligations))
                self.assertEqual({k: skeleton[k] for k in rs.IDENTITY}, {k: j[k] for k in rs.IDENTITY})

    def test_a_skeleton_lists_every_id_the_pass_must_cover(self):
        self.assertEqual([x["id"] for x in rs.skeleton(job("primary"))["previous"]], ["previous:1"])
        self.assertEqual([x["id"] for x in rs.skeleton(job("validation"))["outcomes"]], ["primary:F1"])
        self.assertEqual([x["id"] for x in rs.skeleton(job("reconciliation"))["outcomes"]],
                         ["primary:F1", "previous:1"])


class Check(unittest.TestCase):
    def test_the_check_command(self):
        import shutil
        os.makedirs(BASE, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="schema-", dir=BASE))
        self.addCleanup(shutil.rmtree, directory)
        result, j = example("primary")
        (directory / "job.json").write_text(json.dumps(j))
        (directory / "review.json").write_text(json.dumps(result))
        (directory / "skeleton.json").write_text(json.dumps(rs.skeleton(j)))
        def check(*args):
            return subprocess.run([sys.executable, str(BIN / "review_schema.py"), "check", *args],
                                  capture_output=True, text=True)
        good = check(str(directory / "review.json"))
        self.assertEqual((good.returncode, good.stdout.strip()), (0, "valid"))
        bad = check(str(directory / "skeleton.json"), str(directory / "job.json"))
        self.assertEqual(bad.returncode, 1)
        self.assertTrue(bad.stdout.startswith("invalid: "))


if __name__ == "__main__":
    unittest.main()
