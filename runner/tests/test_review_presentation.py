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

    def test_the_files_variant_points_nowhere_into_job_json(self):
        v3 = rp.prompts("v3-files")
        self.assertEqual(rp.normalise("v3-files"), {"inputs": "files", "prompts": "v3-files", "renderer": 2})
        for kind, text in v3.items():
            self.assertEqual(text.count("job.json"), 1, kind)            # only "do not read job.json"
            self.assertIn("do not read job.json", text)
            self.assertIn("inputs/README.md", text)
        self.assertIn("inputs/results/primary.md", v3["validation"])
        self.assertIn("inputs/fresh.md", v3["reconciliation"])

    def test_every_variant_carries_the_environment_and_contradiction_fixes(self):
        for variant in ("v1", "v2-schema", "v3-files", "v4-paging"):
            texts = rp.prompts(variant)
            for kind, text in texts.items():
                self.assertIn("never source review-env.sh", text, (variant, kind))
            for kind in ("cold", "validation"):          # the sandbox forbids namespaces; nothing prescribes them
                self.assertNotIn("--netns, which uses", texts[kind], (variant, kind))
                self.assertNotIn("Fetch dependencies before namespace entry", texts[kind], (variant, kind))
            self.assertNotIn("RESOLVED, STILL OPEN or DISPUTED disposition", texts["validation"], variant)
            self.assertIn("write no dispositions", texts["validation"], variant)
            self.assertIn("settle each with an outcome", texts["reconciliation"], variant)
            self.assertIn("RESOLVED, STILL OPEN or DISPUTED", texts["primary"], variant)
            self.assertEqual("25,000 characters" in texts["primary"], variant == "v4-paging", variant)

    def test_paging_is_its_own_presentation(self):
        self.assertEqual(rp.normalise("v4-paging"), {"inputs": "files", "prompts": "v4-paging", "renderer": 2})
        v3, v4 = rp.prompts("v3-files"), rp.prompts("v4-paging")
        for kind in v3:
            self.assertTrue(v4[kind].startswith(v3[kind].rstrip("\n")))

    def test_paging_follows_shared_state_to_its_readers(self):
        v3, v4 = rp.prompts("v3-files"), rp.prompts("v4-paging")
        for kind in v4:
            self.assertIn("grep for every reader", v4[kind], kind)
            self.assertIn("never what was not read", v4[kind], kind)
            self.assertNotIn("grep for every reader", v3[kind], kind)

    def test_a_drifted_v1_prompt_cannot_build_v3(self):
        from unittest.mock import patch
        drifted = dict(rp.FILE_POINTERS, all=[("a sentence no prompt has", "x")])
        with patch.object(rp, "FILE_POINTERS", drifted):
            with self.assertRaises(ValueError):
                rp.prompts("v3-files")

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
