import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.config_manager import DEFAULT_CONFIG


class TestCompositeConfig(unittest.TestCase):
    def test_defaults_present(self):
        for k in ["COMPOSITE_MAX_DELOGO_SEGMENTS", "COMPOSITE_DELOGO_PAD_PX",
                  "COMPOSITE_TIMEOUT_SECONDS", "COMPOSITE_BURN_SUBTITLE"]:
            self.assertIn(k, DEFAULT_CONFIG)
        self.assertEqual(DEFAULT_CONFIG["COMPOSITE_MAX_DELOGO_SEGMENTS"], 40)
        self.assertEqual(DEFAULT_CONFIG["COMPOSITE_DELOGO_PAD_PX"], 6)
        self.assertEqual(DEFAULT_CONFIG["COMPOSITE_TIMEOUT_SECONDS"], 10800)
        self.assertTrue(DEFAULT_CONFIG["COMPOSITE_BURN_SUBTITLE"])

    def test_remaster_switch_still_off(self):
        self.assertIn("REMASTER_PIPELINE_ENABLED", DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["REMASTER_PIPELINE_ENABLED"])


if __name__ == "__main__":
    unittest.main()
