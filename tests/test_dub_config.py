import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.config_manager import DEFAULT_CONFIG


class TestDubConfig(unittest.TestCase):
    def test_defaults_present(self):
        for k in [
            "DUB_ENABLED", "DUB_PYTHON", "DUB_RUNNER", "DUB_XTTS_MODEL", "DUB_SPEAKER",
            "DUB_LANGUAGE", "DUB_DEVICE", "DUB_MAX_TEMPO", "DUB_TIMEOUT_SECONDS",
        ]:
            self.assertIn(k, DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["DUB_ENABLED"])  # 2026-09-16: 默认不配音
        self.assertEqual(DEFAULT_CONFIG["DUB_PYTHON"], "/Users/mac/asr-venv/bin/python")
        self.assertEqual(
            DEFAULT_CONFIG["DUB_XTTS_MODEL"],
            "tts_models/multilingual/multi-dataset/xtts_v2",
        )
        self.assertEqual(DEFAULT_CONFIG["DUB_SPEAKER"], "Ana Florence")
        self.assertEqual(DEFAULT_CONFIG["DUB_LANGUAGE"], "zh")
        self.assertEqual(DEFAULT_CONFIG["DUB_DEVICE"], "cpu")
        self.assertEqual(DEFAULT_CONFIG["DUB_MAX_TEMPO"], 1.5)
        self.assertEqual(DEFAULT_CONFIG["DUB_TIMEOUT_SECONDS"], 14400)

    def test_remaster_switch_still_off(self):
        self.assertIn("REMASTER_PIPELINE_ENABLED", DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["REMASTER_PIPELINE_ENABLED"])


if __name__ == "__main__":
    unittest.main()
