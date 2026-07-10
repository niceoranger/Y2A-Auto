import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.demucs_separator import DemucsSeparator


def _write_stub_runner(path, exit_code=0, final_json=None, lines=None):
    body = ["#!/usr/bin/env bash", "echo '[stub-runner] invoked:' \"$@\" >&2"]
    for ln in (lines or ["[demucs] 提取音频", "[demucs] 分离"]):
        body.append(f"echo {json.dumps(ln, ensure_ascii=False)}")
    if final_json is not None:
        body.append("echo " + json.dumps(json.dumps(final_json, ensure_ascii=False), ensure_ascii=False))
    body.append(f"exit {exit_code}")
    with open(path, "w") as f:
        f.write("\n".join(body) + "\n")
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TestDemucsSeparator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.video = os.path.join(self.tmp, "v.mp4"); open(self.video, "w").close()
        self.outdir = os.path.join(self.tmp, "out"); os.makedirs(self.outdir, exist_ok=True)
        self.stub = os.path.join(self.tmp, "fake-runner")

    def test_missing_python_bin(self):
        sep = DemucsSeparator(python_bin="/nonexistent/python", runner_path=self.stub)
        _write_stub_runner(self.stub, exit_code=0, final_json={"ok": True})
        ok, res = sep.separate(video_path=self.video, output_dir=self.outdir, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("python", str(res).lower())

    def test_missing_runner(self):
        sep = DemucsSeparator(python_bin="/bin/bash", runner_path="/nonexistent/runner.py")
        ok, res = sep.separate(video_path=self.video, output_dir=self.outdir, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("runner", str(res).lower())

    def test_missing_video(self):
        _write_stub_runner(self.stub, exit_code=0, final_json={"ok": True})
        sep = DemucsSeparator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = sep.separate(video_path="/nope/x.mp4", output_dir=self.outdir, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("不存在", str(res))

    def test_success_parses_json(self):
        v = os.path.join(self.outdir, "demucs_vocals_t1.wav")
        nv = os.path.join(self.outdir, "demucs_no_vocals_t1.wav")
        _write_stub_runner(self.stub, exit_code=0,
                           final_json={"ok": True, "vocals": v, "no_vocals": nv,
                                       "device_used": "mps"},
                           lines=["[demucs] 提取音频"])
        sep = DemucsSeparator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = sep.separate(video_path=self.video, output_dir=self.outdir, task_id="t1")
        self.assertTrue(ok, msg=str(res))
        self.assertEqual(res["vocals"], v)
        self.assertEqual(res["no_vocals"], nv)

    def test_nonzero_exit(self):
        _write_stub_runner(self.stub, exit_code=1,
                           final_json={"ok": False, "error": "demucs 分离失败(mps)"})
        sep = DemucsSeparator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = sep.separate(video_path=self.video, output_dir=self.outdir, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("分离失败", str(res))

    def test_progress_callback(self):
        _write_stub_runner(self.stub, exit_code=0,
                           final_json={"ok": True, "vocals": "a", "no_vocals": "b"},
                           lines=["提取音频", "分离中", "移动产物"])
        events = []
        sep = DemucsSeparator(python_bin="/bin/bash", runner_path=self.stub)
        sep.separate(video_path=self.video, output_dir=self.outdir, task_id="t1",
                     progress_callback=events.append)
        self.assertTrue(any("分离" in e for e in events))


if __name__ == '__main__':
    unittest.main()
