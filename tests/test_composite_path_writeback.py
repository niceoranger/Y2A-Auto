import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import modules.task_manager as tm


class TestCompositePathWriteback(unittest.TestCase):
    """锁死 soft-fail 写回契约: fail 不改路径; success 改; empty-skip 不改。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_db = tm.DB_PATH
        tm.DB_PATH = os.path.join(self.tmpdir, "tasks.db")
        tm.init_db()
        self.task_id = tm.add_task("https://youtu.be/composite_path")
        self.video = os.path.join(self.tmpdir, "orig.mp4")
        open(self.video, "wb").write(b"\x00" * 64)
        # 指向临时 downloads 目录,避免污染真实 downloads/
        self.task_dir = os.path.join(self.tmpdir, "dl", self.task_id)
        os.makedirs(self.task_dir, exist_ok=True)
        tm.update_task(self.task_id, video_path_local=self.video, status=tm.TASK_STATES["PENDING"])

        self.proc = tm.TaskProcessor()
        self.proc.config = {
            "COMPOSITE_BURN_SUBTITLE": True,
            "COMPOSITE_DELOGO_PAD_PX": 6,
            "COMPOSITE_MAX_DELOGO_SEGMENTS": 40,
            "COMPOSITE_TIMEOUT_SECONDS": 60,
        }
        self.logger = MagicMock()

    def tearDown(self):
        tm.DB_PATH = self._orig_db
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _patch_common(self):
        return patch.multiple(
            tm,
            DOWNLOADS_DIR=os.path.join(self.tmpdir, "dl"),
            get_ffmpeg_path=MagicMock(return_value="/opt/homebrew/bin/ffmpeg"),
        )

    def test_fail_does_not_change_video_path(self):
        with self._patch_common(), patch(
            "modules.remaster_composite.run_composite",
            return_value=(False, "ffmpeg boom"),
        ), patch.object(
            self.proc, "_get_video_stream_info", return_value={"width": 640, "height": 360}
        ):
            ok = self.proc._run_remaster_composite(self.task_id, self.logger)
        self.assertFalse(ok)
        task = tm.get_task(self.task_id)
        self.assertEqual(task["video_path_local"], self.video)
        self.assertIn("composite:", task["composite_warning_message"] or "")

    def test_success_writes_remastered_path(self):
        out = os.path.join(self.task_dir, f"remastered_{self.task_id}.mp4")
        with self._patch_common(), patch(
            "modules.remaster_composite.run_composite",
            return_value=(True, {"output": out, "layers": {"delogo_segments": 1, "burned_subtitle": False, "dubbed_audio": True}}),
        ), patch.object(
            self.proc, "_get_video_stream_info", return_value={"width": 640, "height": 360}
        ):
            ok = self.proc._run_remaster_composite(self.task_id, self.logger)
        self.assertTrue(ok)
        task = tm.get_task(self.task_id)
        self.assertEqual(task["video_path_local"], out)
        self.assertIsNone(task["composite_warning_message"])

    def test_empty_skip_does_not_change_video_path(self):
        with self._patch_common(), patch(
            "modules.remaster_composite.run_composite",
            return_value=(True, {"skipped": True, "layers": {"delogo_segments": 0, "burned_subtitle": False, "dubbed_audio": False}}),
        ), patch.object(
            self.proc, "_get_video_stream_info", return_value={"width": 640, "height": 360}
        ):
            ok = self.proc._run_remaster_composite(self.task_id, self.logger)
        self.assertTrue(ok)
        task = tm.get_task(self.task_id)
        self.assertEqual(task["video_path_local"], self.video)


if __name__ == "__main__":
    unittest.main()
