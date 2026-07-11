import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.remaster_composite import (
    norm_box_to_pixels,
    build_delogo_filter,
    build_vf_chain,
    _ffmpeg_path_escape,
    build_composite_cmd,
)


class TestNormBoxToPixels(unittest.TestCase):
    def test_basic_conversion_with_pad(self):
        x, y, w, h = norm_box_to_pixels([0.25, 0.80, 0.50, 0.10], 640, 360, pad_px=6)
        self.assertEqual((x, y), (154, 282))
        self.assertEqual((w, h), (332, 48))

    def test_clamps_to_frame(self):
        x, y, w, h = norm_box_to_pixels([0.0, 0.0, 1.0, 1.0], 640, 360, pad_px=6)
        self.assertEqual((x, y), (0, 0))
        self.assertLessEqual(x + w, 639)
        self.assertLessEqual(y + h, 359)

    def test_min_size_one(self):
        x, y, w, h = norm_box_to_pixels([0.5, 0.5, 0.0, 0.0], 640, 360, pad_px=0)
        self.assertGreaterEqual(w, 1)
        self.assertGreaterEqual(h, 1)


class TestBuildDelogoFilter(unittest.TestCase):
    def test_empty_segments_returns_empty(self):
        self.assertEqual(build_delogo_filter([], 640, 360), "")

    def test_single_segment(self):
        segs = [{"start": 0.0, "end": 2.5, "box": [0.25, 0.80, 0.50, 0.10]}]
        out = build_delogo_filter(segs, 640, 360, pad_px=0, max_segments=40)
        self.assertIn("delogo=", out)
        self.assertIn("x=160", out)
        self.assertIn("y=288", out)
        self.assertIn("enable='between(t,0.0,2.5)'", out)

    def test_multiple_segments_chained_with_comma(self):
        segs = [
            {"start": 0.0, "end": 1.0, "box": [0.1, 0.8, 0.2, 0.1]},
            {"start": 1.0, "end": 2.0, "box": [0.1, 0.8, 0.2, 0.1]},
        ]
        out = build_delogo_filter(segs, 640, 360, pad_px=0)
        self.assertEqual(out.count("delogo="), 2)
        self.assertIn(",", out)

    def test_caps_at_max_segments(self):
        segs = [{"start": float(i), "end": i + 0.5, "box": [0.1, 0.8, 0.2, 0.1]}
                for i in range(100)]
        out = build_delogo_filter(segs, 640, 360, pad_px=0, max_segments=10)
        self.assertEqual(out.count("delogo="), 10)


class TestBuildVfChain(unittest.TestCase):
    def test_both_parts(self):
        chain = build_vf_chain("delogo=x=1:y=2:w=3:h=4", "subtitles=a.srt")
        self.assertEqual(chain, "delogo=x=1:y=2:w=3:h=4,subtitles=a.srt")

    def test_delogo_only(self):
        self.assertEqual(build_vf_chain("delogo=x=1:y=2:w=3:h=4", ""), "delogo=x=1:y=2:w=3:h=4")

    def test_subtitle_only(self):
        self.assertEqual(build_vf_chain("", "subtitles=a.srt"), "subtitles=a.srt")

    def test_both_empty_returns_none(self):
        self.assertIsNone(build_vf_chain("", ""))


class TestFfmpegPathEscape(unittest.TestCase):
    def test_escapes_colon_and_backslash(self):
        self.assertEqual(_ffmpeg_path_escape("/a/b.srt"), "/a/b.srt")
        self.assertEqual(_ffmpeg_path_escape("C:\\x\\y.srt"), "C\\:\\\\x\\\\y.srt")



class TestBuildCompositeCmd(unittest.TestCase):
    def test_with_dubbed_audio_maps_external_audio(self):
        cmd = build_composite_cmd(
            ffmpeg_bin="/ff/ffmpeg", input_video="/v/in.mp4",
            dubbed_audio="/v/dub.wav", vf_chain="delogo=x=1:y=2:w=3:h=4",
            output_video="/v/out.mp4",
        )
        self.assertEqual(cmd.count("-i"), 2)
        i0 = cmd.index("-i")
        self.assertEqual(cmd[i0 + 1], "/v/in.mp4")
        self.assertIn("0:v:0", cmd)
        self.assertIn("1:a:0", cmd)
        self.assertIn("-vf", cmd)
        self.assertEqual(cmd[-1], "/v/out.mp4")

    def test_without_dubbed_audio_uses_original(self):
        cmd = build_composite_cmd(
            ffmpeg_bin="/ff/ffmpeg", input_video="/v/in.mp4",
            dubbed_audio=None, vf_chain="delogo=x=1:y=2:w=3:h=4",
            output_video="/v/out.mp4",
        )
        self.assertEqual(cmd.count("-i"), 1)
        self.assertIn("0:v:0", cmd)

    def test_no_vf_chain_still_valid_when_only_audio_swap(self):
        cmd = build_composite_cmd(
            ffmpeg_bin="/ff/ffmpeg", input_video="/v/in.mp4",
            dubbed_audio="/v/dub.wav", vf_chain=None,
            output_video="/v/out.mp4",
        )
        self.assertIn("-c:v", cmd)
        self.assertIn("copy", cmd)


if __name__ == "__main__":
    unittest.main()
