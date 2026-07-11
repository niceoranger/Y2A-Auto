import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.ocr_locator import OcrLocator


def _write_stub_runner(path, exit_code=0, final_json=None, lines=None):
    body = ["#!/usr/bin/env bash", "echo '[stub-runner] invoked:' \"$@\" >&2"]
    for ln in (lines or ["[ocr] 抽帧", "[ocr] 检测"]):
        body.append(f"echo {json.dumps(ln, ensure_ascii=False)}")
    if final_json is not None:
        body.append(
            "echo " + json.dumps(json.dumps(final_json, ensure_ascii=False), ensure_ascii=False)
        )
    body.append(f"exit {exit_code}")
    with open(path, "w") as f:
        f.write("\n".join(body) + "\n")
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TestOcrLocator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.video = os.path.join(self.tmp, "v.mp4")
        open(self.video, "w").close()
        self.out = os.path.join(self.tmp, "boxes.json")
        self.stub = os.path.join(self.tmp, "fake-runner")

    def test_missing_python_bin(self):
        _write_stub_runner(self.stub, exit_code=0, final_json={"ok": True})
        loc = OcrLocator(python_bin="/nonexistent/python", runner_path=self.stub)
        ok, res = loc.locate(video_path=self.video, output_json=self.out, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("python", str(res).lower())

    def test_missing_runner(self):
        loc = OcrLocator(python_bin="/bin/bash", runner_path="/nonexistent/runner.py")
        ok, res = loc.locate(video_path=self.video, output_json=self.out, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("runner", str(res).lower())

    def test_missing_video(self):
        _write_stub_runner(self.stub, exit_code=0, final_json={"ok": True})
        loc = OcrLocator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = loc.locate(video_path="/nope/x.mp4", output_json=self.out, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("不存在", str(res))

    def test_success_parses_json(self):
        _write_stub_runner(
            self.stub, exit_code=0,
            final_json={"ok": True, "boxes": self.out, "segments": 3, "frames": 10, "device_used": "cpu"},
            lines=["[ocr] 抽帧"],
        )
        loc = OcrLocator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = loc.locate(video_path=self.video, output_json=self.out, task_id="t1")
        self.assertTrue(ok, msg=str(res))
        self.assertEqual(res["boxes"], self.out)
        self.assertEqual(res["segments"], 3)

    def test_nonzero_exit(self):
        _write_stub_runner(
            self.stub, exit_code=1,
            final_json={"ok": False, "error": "paddleocr 失败"},
        )
        loc = OcrLocator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = loc.locate(video_path=self.video, output_json=self.out, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("失败", str(res))

    def test_progress_callback(self):
        _write_stub_runner(
            self.stub, exit_code=0,
            final_json={"ok": True, "boxes": self.out},
            lines=["抽帧中", "检测中", "聚类"],
        )
        events = []
        loc = OcrLocator(python_bin="/bin/bash", runner_path=self.stub)
        loc.locate(
            video_path=self.video, output_json=self.out, task_id="t1",
            progress_callback=events.append,
        )
        self.assertTrue(any("检测" in e or "聚类" in e for e in events))

    def test_timeout(self):
        body = ["#!/usr/bin/env bash", "sleep 5", "echo '{\"ok\": true}'", "exit 0"]
        with open(self.stub, "w") as f:
            f.write("\n".join(body) + "\n")
        st = os.stat(self.stub)
        os.chmod(self.stub, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        loc = OcrLocator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = loc.locate(
            video_path=self.video, output_json=self.out, task_id="t1", timeout=1,
        )
        self.assertFalse(ok)
        self.assertIn("超时", str(res))



if __name__ == "__main__":
    unittest.main()
