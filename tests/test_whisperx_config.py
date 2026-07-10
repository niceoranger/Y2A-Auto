import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.config_manager import DEFAULT_CONFIG


class TestWhisperXConfig(unittest.TestCase):
    def test_defaults_present(self):
        self.assertIn("REMASTER_PIPELINE_ENABLED", DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["REMASTER_PIPELINE_ENABLED"])
        for k in ["WHISPERX_ASR_PYTHON", "WHISPERX_RUNNER", "WHISPERX_MODEL_NAME",
                  "WHISPERX_DEVICE", "WHISPERX_COMPUTE_TYPE", "WHISPERX_BATCH_SIZE",
                  "WHISPERX_TIMEOUT_SECONDS"]:
            self.assertIn(k, DEFAULT_CONFIG)
        self.assertEqual(DEFAULT_CONFIG["WHISPERX_MODEL_NAME"], "large-v3")
        self.assertEqual(DEFAULT_CONFIG["WHISPERX_DEVICE"], "cpu")
        self.assertEqual(DEFAULT_CONFIG["WHISPERX_COMPUTE_TYPE"], "int8")
        self.assertEqual(DEFAULT_CONFIG["WHISPERX_ASR_PYTHON"], "/Users/mac/asr-venv/bin/python")


if __name__ == "__main__":
    unittest.main()
