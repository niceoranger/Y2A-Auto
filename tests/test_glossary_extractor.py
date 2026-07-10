# tests/test_glossary_extractor.py
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.glossary_extractor import GlossaryExtractor


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeClient:
    """返回预设 content 的假 OpenAI client。"""
    def __init__(self, content):
        self._content = content
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        return _FakeResponse(self._content)


class TestGlossaryExtractor(unittest.TestCase):
    def _make(self, content, max_terms=50):
        ex = GlossaryExtractor(openai_config={"OPENAI_API_KEY": "x"}, task_id="t", max_terms=max_terms)
        ex.client = _FakeClient(content)
        return ex

    def test_extract_parses_terms(self):
        ex = self._make('{"terms":[{"source":"WhisperX","target":"WhisperX"},{"source":"meme","target":"梗"}]}')
        terms = ex.extract(["a WhisperX clip about a meme"], target_language="zh")
        self.assertEqual(terms.get("WhisperX"), "WhisperX")
        self.assertEqual(terms.get("meme"), "梗")

    def test_extract_truncates_to_max_terms(self):
        items = ",".join([f'{{"source":"t{i}","target":"x{i}"}}' for i in range(10)])
        ex = self._make('{"terms":[' + items + ']}', max_terms=3)
        terms = ex.extract(["some text"], target_language="zh")
        self.assertEqual(len(terms), 3)

    def test_extract_bad_json_returns_empty(self):
        ex = self._make("not json at all")
        self.assertEqual(ex.extract(["text"], target_language="zh"), {})

    def test_extract_empty_texts_returns_empty_without_calling(self):
        ex = self._make('{"terms":[{"source":"a","target":"b"}]}')
        self.assertEqual(ex.extract([], target_language="zh"), {})

    def test_extract_no_client_returns_empty(self):
        ex = GlossaryExtractor(openai_config={"OPENAI_API_KEY": "x"}, task_id="t")
        ex.client = None
        self.assertEqual(ex.extract(["text"], target_language="zh"), {})

    def test_extract_skips_blank_entries(self):
        ex = self._make('{"terms":[{"source":"","target":"x"},{"source":"Foo","target":""},{"source":"Bar","target":"吧"}]}')
        terms = ex.extract(["Foo Bar"], target_language="zh")
        self.assertEqual(terms, {"Bar": "吧"})


if __name__ == "__main__":
    unittest.main()
