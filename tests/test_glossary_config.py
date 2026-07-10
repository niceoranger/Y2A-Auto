import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.config_manager import DEFAULT_CONFIG


class TestGlossaryConfig(unittest.TestCase):
    def test_keys_present_with_defaults(self):
        self.assertIn("GLOSSARY_RAG_ENABLED", DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["GLOSSARY_RAG_ENABLED"])
        self.assertEqual(DEFAULT_CONFIG["GLOSSARY_MAX_TERMS"], 50)


if __name__ == "__main__":
    unittest.main()
