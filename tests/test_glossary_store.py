# tests/test_glossary_store.py
import os
import sys
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.glossary_store import GlossaryStore


class TestGlossaryStore(unittest.TestCase):
    def test_match_case_insensitive(self):
        store = GlossaryStore({"WhisperX": "WhisperX", "GitHub": "GitHub"})
        hits = store.match(["I use whisperx daily", "no match here"])
        self.assertIn("WhisperX", hits)
        self.assertNotIn("GitHub", hits)

    def test_match_longest_first_avoids_substring_dup(self):
        store = GlossaryStore({"New York": "纽约", "New York City": "纽约市"})
        hits = store.match(["Welcome to New York City"])
        keys = list(hits.keys())
        self.assertEqual(keys[0], "New York City")
        self.assertIn("New York", hits)

    def test_match_empty_when_no_hit(self):
        store = GlossaryStore({"Foo": "福"})
        self.assertEqual(store.match(["nothing relevant"]), {})

    def test_match_empty_store(self):
        store = GlossaryStore({})
        self.assertEqual(store.match(["anything"]), {})

    def test_format_for_prompt_nonempty(self):
        store = GlossaryStore({"WhisperX": "WhisperX"})
        text = store.format_for_prompt({"WhisperX": "WhisperX"})
        self.assertIn("WhisperX", text)
        self.assertIn("术语", text)

    def test_format_for_prompt_empty_returns_blank(self):
        store = GlossaryStore({"WhisperX": "WhisperX"})
        self.assertEqual(store.format_for_prompt({}), "")

    def test_save_and_load_roundtrip(self):
        store = GlossaryStore({"WhisperX": "WhisperX", "梗": "meme"})
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "glossary.json")
            store.save(p)
            self.assertTrue(os.path.exists(p))
            loaded = GlossaryStore.load(p)
            self.assertEqual(loaded.terms, store.terms)
            self.assertEqual(len(loaded), 2)

    def test_load_missing_file_returns_empty(self):
        loaded = GlossaryStore.load("/nonexistent/glossary.json")
        self.assertEqual(loaded.terms, {})


if __name__ == "__main__":
    unittest.main()
