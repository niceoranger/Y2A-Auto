import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import modules.task_manager as tm


class TestOcrWarningPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_db_path = tm.DB_PATH
        tm.DB_PATH = os.path.join(self.tmpdir, "tasks.db")
        tm.init_db()

    def tearDown(self):
        tm.DB_PATH = self._orig_db_path
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ocr_warning_message_round_trip(self):
        task_id = tm.add_task("https://youtu.be/ocr_test")
        tm.update_task(task_id, ocr_warning_message="ocr: runner timeout")
        task = tm.get_task(task_id)
        self.assertEqual(task["ocr_warning_message"], "ocr: runner timeout")

    def test_ocr_warning_message_cleared_on_success(self):
        task_id = tm.add_task("https://youtu.be/ocr_test2")
        tm.update_task(task_id, ocr_warning_message="ocr: temp")
        tm.update_task(task_id, ocr_warning_message=None)
        task = tm.get_task(task_id)
        self.assertIsNone(task["ocr_warning_message"])


if __name__ == "__main__":
    unittest.main()
