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
        self.assertEqual(DEFAULT_CONFIG["COMPOSITE_MAX_DELOGO_SEGMENTS"], 200)
        self.assertEqual(DEFAULT_CONFIG["COMPOSITE_DELOGO_PAD_PX"], 6)
        self.assertEqual(DEFAULT_CONFIG["COMPOSITE_TIMEOUT_SECONDS"], 10800)
        self.assertTrue(DEFAULT_CONFIG["COMPOSITE_BURN_SUBTITLE"])
        self.assertEqual(DEFAULT_CONFIG["COMPOSITE_WIDE_BAND_RATIO"], 0.12)

    def test_remaster_switch_still_off(self):
        self.assertIn("REMASTER_PIPELINE_ENABLED", DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["REMASTER_PIPELINE_ENABLED"])

    def test_ocr_erase_switch_off_by_default(self):
        # 2026-09-27 放弃 OCR 字幕擦除:默认只烧译文字幕
        self.assertIn("OCR_ENABLED", DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["OCR_ENABLED"])
        source = (pathlib.Path(__file__).resolve().parents[1] / "modules" / "task_manager.py").read_text(encoding="utf-8")
        self.assertIn("self.config.get('OCR_ENABLED', False)", source)


if __name__ == "__main__":
    unittest.main()
