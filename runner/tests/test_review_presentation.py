import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import review_presentation as rp  # noqa: E402


class Presentation(unittest.TestCase):
    def test_a_run_without_one_is_legacy_and_records_nothing(self):
        self.assertEqual(rp.normalise(None), rp.LEGACY)
        self.assertTrue(rp.legacy(None))
        self.assertIsNone(rp.recorded(None))
        self.assertIsNone(rp.recorded(dict(rp.LEGACY)))

    def test_one_this_code_cannot_run_is_refused(self):
        for bad in ({"inputs": "files", "prompts": "v1", "renderer": None},
                    {"inputs": "json", "prompts": "v1"}, ["json"], {"inputs": "json", "prompts": "v9", "renderer": None},
                    # known parts in a combination that does not exist
                    {"inputs": "json", "prompts": "v2-schema", "renderer": None}, "v9"):
            with self.assertRaises(ValueError):
                rp.normalise(bad)

    def test_a_known_new_presentation_is_recorded(self):
        schema = rp.normalise("v2-schema")
        self.assertEqual(schema, {"inputs": "json", "prompts": "v2-schema", "renderer": 1})
        self.assertEqual(rp.recorded(schema), schema)
        self.assertFalse(rp.legacy(schema))

    def test_the_schema_variant_adds_its_guidance_to_every_prompt(self):
        v1, v2 = rp.prompts("v1"), rp.prompts("v2-schema")
        for kind in v1:
            self.assertTrue(v2[kind].startswith(v1[kind].rstrip("\n")))
            self.assertIn("inputs/result-skeleton.json", v2[kind])
        with self.assertRaises(ValueError):
            rp.prompts("v9")

    def test_the_legacy_prompts_are_the_command_files(self):
        texts = rp.prompts("v1")
        self.assertEqual(set(texts), {"primary", "cold", "validation", "reconciliation"})
        self.assertEqual(texts["validation"], (rp.COMMANDS / "review-validate.md").read_text())


if __name__ == "__main__":
    unittest.main()
