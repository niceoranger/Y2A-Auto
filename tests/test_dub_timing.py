import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.dub_timing import fit_segment_audio


class TestDubTiming(unittest.TestCase):
    def test_pad_when_shorter(self):
        # 自然 1.0s, 目标 2.0s → pad, time_ratio=1.0, pad_to=2.0
        r = fit_segment_audio(natural_sec=1.0, target_sec=2.0, max_tempo=1.5)
        self.assertEqual(r["mode"], "pad")
        self.assertEqual(r["time_ratio"], 1.0)
        self.assertEqual(r["pad_to_sec"], 2.0)
        self.assertIsNone(r["truncate_to_sec"])

    def test_stretch_within_max_tempo(self):
        # 自然 3.0s, 目标 2.0s → 需加速 1.5x → time_ratio=2/3
        r = fit_segment_audio(natural_sec=3.0, target_sec=2.0, max_tempo=1.5)
        self.assertEqual(r["mode"], "stretch")
        self.assertAlmostEqual(r["time_ratio"], 2.0 / 3.0, places=6)
        self.assertIsNone(r["pad_to_sec"])
        self.assertIsNone(r["truncate_to_sec"])

    def test_stretch_and_truncate_when_exceeds_max_tempo(self):
        # 自然 4.0s, 目标 2.0s → 需 2x 加速, 但 max_tempo=1.5 → time_ratio=1/1.5, truncate 到 2.0
        r = fit_segment_audio(natural_sec=4.0, target_sec=2.0, max_tempo=1.5)
        self.assertEqual(r["mode"], "stretch_and_truncate")
        self.assertAlmostEqual(r["time_ratio"], 1.0 / 1.5, places=6)
        self.assertEqual(r["truncate_to_sec"], 2.0)
        self.assertIsNone(r["pad_to_sec"])

    def test_exact_match(self):
        r = fit_segment_audio(natural_sec=2.0, target_sec=2.0, max_tempo=1.5)
        self.assertEqual(r["mode"], "pad")
        self.assertEqual(r["time_ratio"], 1.0)
        self.assertEqual(r["pad_to_sec"], 2.0)

    def test_invalid_non_positive(self):
        r = fit_segment_audio(natural_sec=0.0, target_sec=1.0, max_tempo=1.5)
        self.assertEqual(r["mode"], "skip")
        r2 = fit_segment_audio(natural_sec=1.0, target_sec=0.0, max_tempo=1.5)
        self.assertEqual(r2["mode"], "skip")


if __name__ == "__main__":
    unittest.main()
