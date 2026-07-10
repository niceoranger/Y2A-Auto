import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.subtitle_translator import LLMRequester
from modules.glossary_store import GlossaryStore


class TestGlossaryInjectionInRequester(unittest.TestCase):
    def _requester(self):
        return LLMRequester(openai_config={"OPENAI_API_KEY": "x", "OPENAI_MODEL_NAME": "m"}, task_id="t")

    def test_build_system_prompt_accepts_glossary(self):
        req = self._requester()
        base = req._build_structured_system_prompt("zh")
        base2 = req._build_structured_system_prompt("zh", glossary_text="")
        self.assertEqual(base, base2)
        gtext = GlossaryStore({"WhisperX": "WhisperX"}).format_for_prompt({"WhisperX": "WhisperX"})
        injected = req._build_structured_system_prompt("zh", glossary_text=gtext)
        self.assertIn("WhisperX", injected)

    def test_strict_build_system_prompt_accepts_glossary(self):
        req = self._requester()
        gtext = "\n\nSTRICT_G_MARKER"
        injected = req._build_strict_structured_system_prompt("zh", glossary_text=gtext)
        self.assertIn("STRICT_G_MARKER", injected)


if __name__ == "__main__":
    unittest.main()
