import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.whisperx_asr import WhisperXAsr


def _write_stub_runner(path, exit_code=0, final_json=None, lines=None):
    body = ["#!/usr/bin/env bash", "echo '[stub-runner] invoked:' \"$@\" >&2"]
    for ln in (lines or ["[whisperx] 加载模型", "[whisperx] 转写"]):
        body.append(f"echo {json.dumps(ln, ensure_ascii=False)}")
    if final_json is not None:
        body.append("echo " + json.dumps(json.dumps(final_json)))
    body.append(f"exit {exit_code}")
    with open(path, "w") as f:
        f.write("\n".join(body) + "\n")
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TestWhisperXAsr(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.video = os.path.join(self.tmp, "v.mp4"); open(self.video, "w").close()
        self.out = os.path.join(self.tmp, "out.srt")
        self.stub = os.path.join(self.tmp, "fake-runner")

    def test_missing_runner(self):
        asr = WhisperXAsr(python_bin="/bin/bash", runner_path="/nonexistent/runner.py")
        ok, res = asr.transcribe(video_path=self.video, output_srt_path=self.out)
        self.assertFalse(ok)
        self.assertIn("runner", str(res).lower())

    def test_success_parses_json(self):
        _write_stub_runner(self.stub, exit_code=0,
                           final_json={"ok": True, "srt": self.out, "segments": 12},
                           lines=["[whisperx] 加载模型"])
        with open(self.out, "w") as f:
            f.write("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        asr = WhisperXAsr(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = asr.transcribe(video_path=self.video, output_srt_path=self.out)
        self.assertTrue(ok, msg=str(res))
        self.assertEqual(res["srt"], self.out)
        self.assertEqual(res["segments"], 12)

    def test_nonzero_exit(self):
        _write_stub_runner(self.stub, exit_code=1,
                           final_json={"ok": False, "error": "model missing"})
        asr = WhisperXAsr(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = asr.transcribe(video_path=self.video, output_srt_path=self.out)
        self.assertFalse(ok)
        self.assertIn("model missing", str(res))

    def test_missing_video(self):
        _write_stub_runner(self.stub, exit_code=0, final_json={"ok": True, "srt": self.out})
        asr = WhisperXAsr(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = asr.transcribe(video_path="/nope/x.mp4", output_srt_path=self.out)
        self.assertFalse(ok)
        self.assertIn("不存在", str(res))

    def test_progress_callback(self):
        _write_stub_runner(self.stub, exit_code=0,
                           final_json={"ok": True, "srt": self.out},
                           lines=["加载模型", "转写中", "对齐"])
        events = []
        asr = WhisperXAsr(python_bin="/bin/bash", runner_path=self.stub)
        asr.transcribe(video_path=self.video, output_srt_path=self.out,
                       progress_callback=events.append)
        self.assertTrue(any("转写" in e or "对齐" in e for e in events))


if __name__ == '__main__':
    unittest.main()
