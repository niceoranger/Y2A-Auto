import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.config_manager import DEFAULT_CONFIG


class TestOcrConfig(unittest.TestCase):
    def test_defaults_present(self):
        for k in [
            "OCR_PYTHON", "OCR_RUNNER", "OCR_SAMPLE_INTERVAL_SEC",
            "OCR_LANG", "OCR_DEVICE", "OCR_IOU_THRESHOLD", "OCR_BOTTOM_BAND_MIN_Y", "OCR_TIMEOUT_SECONDS",
        ]:
            self.assertIn(k, DEFAULT_CONFIG)
        self.assertEqual(DEFAULT_CONFIG["OCR_PYTHON"], "/Users/mac/asr-venv/bin/python")
        self.assertEqual(DEFAULT_CONFIG["OCR_RUNNER"], "modules/ocr_runner.py")
        self.assertEqual(DEFAULT_CONFIG["OCR_SAMPLE_INTERVAL_SEC"], 2.0)
        self.assertEqual(DEFAULT_CONFIG["OCR_LANG"], "ch")
        self.assertEqual(DEFAULT_CONFIG["OCR_DEVICE"], "cpu")
        self.assertEqual(DEFAULT_CONFIG["OCR_IOU_THRESHOLD"], 0.5)
        self.assertEqual(DEFAULT_CONFIG["OCR_BOTTOM_BAND_MIN_Y"], 0.6)
        self.assertEqual(DEFAULT_CONFIG["OCR_TIMEOUT_SECONDS"], 3600)

    def test_remaster_switch_still_off(self):
        self.assertIn("REMASTER_PIPELINE_ENABLED", DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["REMASTER_PIPELINE_ENABLED"])


if __name__ == "__main__":
    unittest.main()
