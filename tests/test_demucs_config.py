import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.config_manager import DEFAULT_CONFIG


class TestDemucsConfig(unittest.TestCase):
    def test_defaults_present(self):
        for k in ["DEMUCS_PYTHON", "DEMUCS_RUNNER", "DEMUCS_MODEL_NAME",
                  "DEMUCS_DEVICE", "DEMUCS_STEMS", "DEMUCS_TIMEOUT_SECONDS"]:
            self.assertIn(k, DEFAULT_CONFIG)
        self.assertEqual(DEFAULT_CONFIG["DEMUCS_MODEL_NAME"], "htdemucs_ft")
        self.assertEqual(DEFAULT_CONFIG["DEMUCS_DEVICE"], "mps")
        self.assertEqual(DEFAULT_CONFIG["DEMUCS_STEMS"], 2)
        self.assertEqual(DEFAULT_CONFIG["DEMUCS_PYTHON"], "/Users/mac/asr-venv/bin/python")

    def test_remaster_switch_still_off(self):
        # Demucs 复用 REMASTER 总开关，不新增开关；默认仍关
        self.assertIn("REMASTER_PIPELINE_ENABLED", DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["REMASTER_PIPELINE_ENABLED"])


if __name__ == "__main__":
    unittest.main()
