import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.prompt_manager import get_subtitle_system_prompt, get_subtitle_strict_system_prompt


class TestGlossaryPromptInjection(unittest.TestCase):
    def test_no_glossary_matches_default(self):
        a = get_subtitle_system_prompt(mode="builtin", user_text="", target_language="zh")
        b = get_subtitle_system_prompt(mode="builtin", user_text="", target_language="zh", glossary_text="")
        self.assertEqual(a, b)

    def test_glossary_text_injected(self):
        gtext = "\n\n术语对照表：\n- WhisperX → WhisperX"
        out = get_subtitle_system_prompt(mode="builtin", user_text="", target_language="zh", glossary_text=gtext)
        self.assertIn("WhisperX", out)
        self.assertIn("术语对照表", out)

    def test_glossary_injected_before_json_suffix(self):
        gtext = "\n\nGLOSSARY_MARKER_XYZ"
        out = get_subtitle_system_prompt(mode="builtin", user_text="", target_language="zh", glossary_text=gtext)
        from modules.prompt_manager import _SUBTITLE_JSON_SUFFIX
        marker_pos = out.index("GLOSSARY_MARKER_XYZ")
        json_pos = out.index(_SUBTITLE_JSON_SUFFIX[:20])
        self.assertLess(marker_pos, json_pos)

    def test_strict_variant_injects_glossary(self):
        gtext = "\n\nGLOSSARY_STRICT_MARKER"
        out = get_subtitle_strict_system_prompt(mode="builtin", user_text="", target_language="zh", glossary_text=gtext)
        self.assertIn("GLOSSARY_STRICT_MARKER", out)


if __name__ == "__main__":
    unittest.main()
