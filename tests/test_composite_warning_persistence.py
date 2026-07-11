import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import modules.task_manager as tm


class TestCompositeWarningPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig = tm.DB_PATH
        tm.DB_PATH = os.path.join(self.tmpdir, "tasks.db")
        tm.init_db()

    def tearDown(self):
        tm.DB_PATH = self._orig
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_round_trip(self):
        task_id = tm.add_task("https://youtu.be/composite_test")
        tm.update_task(task_id, composite_warning_message="composite: ffmpeg failed")
        self.assertEqual(tm.get_task(task_id)["composite_warning_message"],
                         "composite: ffmpeg failed")

    def test_cleared_on_success(self):
        task_id = tm.add_task("https://youtu.be/composite_test2")
        tm.update_task(task_id, composite_warning_message="composite: temp")
        tm.update_task(task_id, composite_warning_message=None)
        self.assertIsNone(tm.get_task(task_id)["composite_warning_message"])


if __name__ == "__main__":
    unittest.main()
