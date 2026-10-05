import sys
import unittest
from pathlib import Path
from unittest.mock import patch

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
                    {"inputs": "json", "prompts": "v1"}, ["json"], {"inputs": "json", "prompts": "v9", "renderer": None}):
            with self.assertRaises(ValueError):
                rp.normalise(bad)

    def test_a_known_new_presentation_is_recorded(self):
        files = {"inputs": "files", "prompts": "v1", "renderer": 1}
        with patch.dict(rp.KNOWN, inputs=("json", "files"), renderer=(None, 1)):
            self.assertEqual(rp.recorded(files), files)
            self.assertFalse(rp.legacy(files))

    def test_the_legacy_prompts_are_the_command_files(self):
        texts = rp.prompts("v1")
        self.assertEqual(set(texts), {"primary", "cold", "validation", "reconciliation"})
        self.assertEqual(texts["validation"], (rp.COMMANDS / "review-validate.md").read_text())


if __name__ == "__main__":
    unittest.main()
