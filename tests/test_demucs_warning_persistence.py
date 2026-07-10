import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import modules.task_manager as tm


class TestDemucsWarningPersistence(unittest.TestCase):
    """回归测试:demucs_warning_message 必须能真正落盘并读回。

    审查发现的 IMPORTANT 缺陷——该列曾缺失于 schema/迁移/ALLOWED_COLUMNS,
    导致 update_task 静默丢弃,软失败契约(失败时记录警告)形同虚设。
    """

    def setUp(self):
        # 把 DB 重定向到临时文件(DB_PATH 是模块级全局,函数运行时才读)
        self.tmpdir = tempfile.mkdtemp()
        self._orig_db_path = tm.DB_PATH
        tm.DB_PATH = os.path.join(self.tmpdir, "tasks.db")
        tm.init_db()

    def tearDown(self):
        tm.DB_PATH = self._orig_db_path
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_demucs_warning_message_round_trip(self):
        task_id = tm.add_task("https://youtu.be/demucs_test")
        tm.update_task(task_id, demucs_warning_message="demucs: mps 降级 CPU 后仍失败")
        task = tm.get_task(task_id)
        self.assertEqual(task["demucs_warning_message"], "demucs: mps 降级 CPU 后仍失败")

    def test_demucs_warning_message_cleared_on_success(self):
        task_id = tm.add_task("https://youtu.be/demucs_test2")
        tm.update_task(task_id, demucs_warning_message="demucs: 临时失败")
        tm.update_task(task_id, demucs_warning_message=None)
        task = tm.get_task(task_id)
        self.assertIsNone(task["demucs_warning_message"])


if __name__ == "__main__":
    unittest.main()
