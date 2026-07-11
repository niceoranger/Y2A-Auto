import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.dub_generator import DubGenerator


def _write_stub_runner(path, exit_code=0, final_json=None, lines=None):
    body = ["#!/usr/bin/env bash", "echo '[stub-runner] invoked:' \"$@\" >&2"]
    for ln in (lines or ["[dub] 解析字幕", "[dub] 合成"]):
        body.append(f"echo {json.dumps(ln, ensure_ascii=False)}")
    if final_json is not None:
        body.append("echo " + json.dumps(json.dumps(final_json, ensure_ascii=False), ensure_ascii=False))
    body.append(f"exit {exit_code}")
    with open(path, "w") as f:
        f.write("\n".join(body) + "\n")
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TestDubGenerator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.srt = os.path.join(self.tmp, "zh.srt"); open(self.srt, "w").close()
        self.novoc = os.path.join(self.tmp, "no_vocals.wav"); open(self.novoc, "w").close()
        self.out = os.path.join(self.tmp, "dubbed.wav")
        self.stub = os.path.join(self.tmp, "fake-runner")

    def test_missing_python_bin(self):
        _write_stub_runner(self.stub, exit_code=0, final_json={"ok": True})
        g = DubGenerator(python_bin="/nonexistent/python", runner_path=self.stub)
        ok, res = g.generate(translated_srt_path=self.srt, no_vocals_wav=self.novoc,
                             output_path=self.out, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("python", str(res).lower())

    def test_missing_runner(self):
        g = DubGenerator(python_bin="/bin/bash", runner_path="/nonexistent/runner.py")
        ok, res = g.generate(translated_srt_path=self.srt, no_vocals_wav=self.novoc,
                             output_path=self.out, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("runner", str(res).lower())

    def test_missing_srt(self):
        _write_stub_runner(self.stub, exit_code=0, final_json={"ok": True})
        g = DubGenerator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = g.generate(translated_srt_path="/nope/x.srt", no_vocals_wav=self.novoc,
                             output_path=self.out, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("不存在", str(res))

    def test_missing_no_vocals(self):
        _write_stub_runner(self.stub, exit_code=0, final_json={"ok": True})
        g = DubGenerator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = g.generate(translated_srt_path=self.srt, no_vocals_wav="/nope/bg.wav",
                             output_path=self.out, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("不存在", str(res))

    def test_success_parses_json(self):
        _write_stub_runner(self.stub, exit_code=0,
                           final_json={"ok": True, "dubbed": self.out, "segments": 5,
                                       "device_used": "cpu", "speaker": "Ana Florence"},
                           lines=["[dub] 解析到 5 段字幕"])
        g = DubGenerator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = g.generate(translated_srt_path=self.srt, no_vocals_wav=self.novoc,
                             output_path=self.out, task_id="t1")
        self.assertTrue(ok, msg=str(res))
        self.assertEqual(res["dubbed"], self.out)
        self.assertEqual(res["segments"], 5)

    def test_nonzero_exit(self):
        _write_stub_runner(self.stub, exit_code=1,
                           final_json={"ok": False, "error": "xtts 合成失败"})
        g = DubGenerator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = g.generate(translated_srt_path=self.srt, no_vocals_wav=self.novoc,
                             output_path=self.out, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("合成失败", str(res))

    def test_progress_callback(self):
        _write_stub_runner(self.stub, exit_code=0,
                           final_json={"ok": True, "dubbed": self.out},
                           lines=["解析字幕", "合成中", "混音"])
        events = []
        g = DubGenerator(python_bin="/bin/bash", runner_path=self.stub)
        g.generate(translated_srt_path=self.srt, no_vocals_wav=self.novoc,
                   output_path=self.out, task_id="t1", progress_callback=events.append)
        self.assertTrue(any("合成" in e for e in events))


if __name__ == '__main__':
    unittest.main()
