import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.sau_uploader import SauPlatformUploader


def _write_stub_sau(path, exit_code=0, stdout_lines=None, stdout_json=None):
    """生成一个假的 sau 可执行脚本。"""
    lines = stdout_lines or ["[sau] starting", "[sau] uploading 50%", "[sau] uploading 100%"]
    body_lines = ["#!/usr/bin/env bash", "echo '[sau-stub] invoked:' \"$@\" >&2"]
    for ln in lines:
        body_lines.append(f"echo {json.dumps(ln)}")
    if stdout_json is not None:
        body_lines.append("echo " + json.dumps(json.dumps(stdout_json)))
    body_lines.append(f"exit {exit_code}")
    with open(path, "w") as f:
        f.write("\n".join(body_lines) + "\n")
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TestSauUploader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.video = os.path.join(self.tmp, "v.mp4"); open(self.video, "w").close()
        self.cover = os.path.join(self.tmp, "c.jpg"); open(self.cover, "w").close()
        self.stub = os.path.join(self.tmp, "fake-sau")

    def test_missing_sau_bin_returns_error(self):
        up = SauPlatformUploader(sau_bin="/nonexistent/sau")
        ok, res = up.upload_video(video_file_path=self.video, cover_file_path=self.cover,
                                  title="t", description="d", tags=["x"],
                                  platform="douyin", account="acc1")
        self.assertFalse(ok)
        self.assertIn("sau", str(res).lower())

    def test_success_parses_result(self):
        _write_stub_sau(self.stub, exit_code=0,
                        stdout_json={"url": "https://douyin/x", "rawid": "abc"})
        up = SauPlatformUploader(sau_bin=self.stub)
        ok, res = up.upload_video(video_file_path=self.video, cover_file_path=self.cover,
                                  title="t", description="d", tags=["a", "b"],
                                  platform="douyin", account="acc1")
        self.assertTrue(ok, msg=str(res))
        self.assertEqual(res["url"], "https://douyin/x")
        self.assertEqual(res["platform"], "douyin")

    def test_nonzero_exit_returns_error(self):
        _write_stub_sau(self.stub, exit_code=2, stdout_lines=["boom: login required"])
        up = SauPlatformUploader(sau_bin=self.stub)
        ok, res = up.upload_video(video_file_path=self.video, cover_file_path=self.cover,
                                  title="t", description="d", tags=[],
                                  platform="douyin", account="acc1")
        self.assertFalse(ok)
        self.assertIn("boom", str(res))

    def test_missing_video_file(self):
        _write_stub_sau(self.stub, exit_code=0)
        up = SauPlatformUploader(sau_bin=self.stub)
        ok, res = up.upload_video(video_file_path="/nope/x.mp4", cover_file_path=self.cover,
                                  title="t", description="d", tags=[],
                                  platform="douyin", account="acc1")
        self.assertFalse(ok)
        self.assertIn("不存在", str(res))

    def test_progress_callback_invoked(self):
        _write_stub_sau(self.stub, exit_code=0,
                        stdout_lines=["uploading 50%", "uploading 100%"])
        events = []
        up = SauPlatformUploader(sau_bin=self.stub)
        up.upload_video(video_file_path=self.video, cover_file_path=self.cover,
                        title="t", description="d", tags=[], platform="douyin",
                        account="acc1", progress_callback=events.append)
        self.assertTrue(any("50" in e or "100" in e for e in events))


if __name__ == '__main__':
    unittest.main()