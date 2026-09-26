import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.ocr_locator import (
    OcrLocator,
    guess_language_from_srt_file,
    guess_language_from_text,
    resolve_ocr_lang,
)


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

    def test_min_rec_score_passed_to_runner(self):
        args_file = os.path.join(self.tmp, "runner_args.txt")
        body = [
            "#!/usr/bin/env bash",
            f"printf '%s\\n' \"$@\" > {json.dumps(args_file)}",
            "echo '{\"ok\": true}'",
            "exit 0",
        ]
        with open(self.stub, "w") as f:
            f.write("\n".join(body) + "\n")
        st = os.stat(self.stub)
        os.chmod(self.stub, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        loc = OcrLocator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = loc.locate(
            video_path=self.video, output_json=self.out, task_id="t1",
            min_rec_score=0.55,
        )
        self.assertTrue(ok, msg=str(res))
        with open(args_file) as f:
            args = f.read().splitlines()
        self.assertIn("--min-rec-score", args)
        self.assertEqual(args[args.index("--min-rec-score") + 1], "0.55")


class TestResolveOcrLang(unittest.TestCase):
    def test_known_iso_codes(self):
        self.assertEqual(resolve_ocr_lang("en"), "en")
        self.assertEqual(resolve_ocr_lang("zh"), "ch")
        self.assertEqual(resolve_ocr_lang("ZH"), "ch")
        self.assertEqual(resolve_ocr_lang("zh-CN"), "ch")
        self.assertEqual(resolve_ocr_lang("ja"), "japan")
        self.assertEqual(resolve_ocr_lang("ko"), "korean")
        self.assertEqual(resolve_ocr_lang("fr"), "french")
        self.assertEqual(resolve_ocr_lang("de"), "german")
        self.assertEqual(resolve_ocr_lang("ru"), "russian")

    def test_unknown_or_empty_falls_back(self):
        self.assertEqual(resolve_ocr_lang(""), "ch")
        self.assertEqual(resolve_ocr_lang("auto"), "ch")
        self.assertEqual(resolve_ocr_lang("xx"), "ch")
        self.assertEqual(resolve_ocr_lang(None, fallback="en"), "en")
        self.assertEqual(resolve_ocr_lang("xx", fallback="en"), "en")
        self.assertEqual(resolve_ocr_lang("xx", fallback=""), "ch")


class TestGuessLanguage(unittest.TestCase):
    def test_english_sample(self):
        text = "Poland President Karol Nawrocki says it is just a matter of time before a permanent US military base is built."
        self.assertEqual(guess_language_from_text(text), "en")

    def test_chinese_sample(self):
        text = "波兰总统表示,美军永久基地建成只是时间问题,他希望在他任期结束前完成。"
        self.assertEqual(guess_language_from_text(text), "zh")

    def test_too_short_returns_empty(self):
        self.assertEqual(guess_language_from_text("hi"), "")
        self.assertEqual(guess_language_from_text(""), "")

    def test_srt_file_language(self):
        srt = os.path.join(tempfile.mkdtemp(), "asr_test.srt")
        with open(srt, "w", encoding="utf-8") as f:
            f.write(
                "1\n00:00:00,000 --> 00:00:03,000\n"
                "Poland President Karol Nawrocki speaks exclusively to Bloomberg.\n\n"
                "2\n00:00:03,000 --> 00:00:06,000\n"
                "He says it is just a matter of time before the base is built.\n"
            )
        self.assertEqual(guess_language_from_srt_file(srt), "en")

    def test_missing_srt_returns_empty(self):
        self.assertEqual(guess_language_from_srt_file("/nope/missing.srt"), "")



if __name__ == "__main__":
    unittest.main()
