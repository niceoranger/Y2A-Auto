"""下载保留期清理:只删已完成任务的媒体大文件,保留字幕/元数据,跳过非终态任务。"""
import ast
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

TASK_MANAGER_PATH = pathlib.Path(__file__).resolve().parents[1] / "modules" / "task_manager.py"


def _load_cleanup_helpers():
    """AST 抽取清理函数与常量,避免整体导入 task_manager(重依赖)。"""
    source = TASK_MANAGER_PATH.read_text(encoding="utf-8")
    module_ast = ast.parse(source, filename=str(TASK_MANAGER_PATH))
    selected = [
        node for node in module_ast.body
        if (
            isinstance(node, ast.FunctionDef)
            and node.name in ('cleanup_expired_task_media', '_parse_task_datetime_for_cleanup')
        )
        or (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == 'CLEANABLE_MEDIA_EXTENSIONS' for t in node.targets)
        )
    ]
    namespace = {
        'os': os,
        'datetime': datetime,
        'timedelta': __import__('datetime').timedelta,
        'logger': MagicMock(),
        'TASK_STATES': {'COMPLETED': 'completed'},
        'get_all_tasks': lambda: [],
        '_is_task_active': lambda task_id: False,
        'DOWNLOADS_DIR': '',
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(TASK_MANAGER_PATH), 'exec'), namespace)
    return namespace


class CleanupExpiredTaskMediaTests(unittest.TestCase):
    def setUp(self):
        self.namespace = _load_cleanup_helpers()
        self.tmpdir = tempfile.mkdtemp(prefix='downloads_cleanup_test_')
        self.namespace['DOWNLOADS_DIR'] = self.tmpdir
        self.cleanup = self.namespace['cleanup_expired_task_media']

    def _make_task_dir(self, task_id):
        task_dir = os.path.join(self.tmpdir, task_id)
        os.makedirs(task_dir, exist_ok=True)
        return task_dir

    @staticmethod
    def _write(path, size=1024):
        with open(path, 'wb') as f:
            f.write(b'x' * size)

    def test_old_completed_task_media_removed_small_files_kept(self):
        task_dir = self._make_task_dir('old-task')
        media = ['video.mp4', 'video_with_subtitle.mp4', 'video.mp4.asr16k.wav']
        kept = ['translated_x.srt', 'subtitle_burn_x.ass', 'metadata.json', 'video.webp']
        for name in media + kept:
            self._write(os.path.join(task_dir, name))
        self.namespace['get_all_tasks'] = lambda: [
            {'id': 'old-task', 'status': 'completed', 'updated_at': '2026-09-01 00:00:00'},
        ]

        files_removed, bytes_freed = self.cleanup(72)

        self.assertEqual(files_removed, 3)
        self.assertEqual(bytes_freed, 3 * 1024)
        for name in media:
            self.assertFalse(os.path.exists(os.path.join(task_dir, name)), name)
        for name in kept:
            self.assertTrue(os.path.exists(os.path.join(task_dir, name)), name)

    def test_recent_completed_task_untouched(self):
        task_dir = self._make_task_dir('recent-task')
        self._write(os.path.join(task_dir, 'video.mp4'))
        recent = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.namespace['get_all_tasks'] = lambda: [
            {'id': 'recent-task', 'status': 'completed', 'updated_at': recent},
        ]

        files_removed, _ = self.cleanup(72)

        self.assertEqual(files_removed, 0)
        self.assertTrue(os.path.exists(os.path.join(task_dir, 'video.mp4')))

    def test_non_completed_statuses_untouched(self):
        # 待处理/处理中/失败可续传任务一律跳过,避免破坏断点续跑
        for status in ('pending', 'downloading', 'failed', 'awaiting_manual_review'):
            task_dir = self._make_task_dir(f'task-{status}')
            self._write(os.path.join(task_dir, 'video.mp4'))
        self.namespace['get_all_tasks'] = lambda: [
            {'id': f'task-{s}', 'status': s, 'updated_at': '2026-09-01 00:00:00'}
            for s in ('pending', 'downloading', 'failed', 'awaiting_manual_review')
        ]

        files_removed, _ = self.cleanup(72)

        self.assertEqual(files_removed, 0)

    def test_active_task_untouched(self):
        task_dir = self._make_task_dir('active-task')
        self._write(os.path.join(task_dir, 'video.mp4'))
        self.namespace['get_all_tasks'] = lambda: [
            {'id': 'active-task', 'status': 'completed', 'updated_at': '2026-09-01 00:00:00'},
        ]
        self.namespace['_is_task_active'] = lambda task_id: True

        files_removed, _ = self.cleanup(72)

        self.assertEqual(files_removed, 0)
        self.assertTrue(os.path.exists(os.path.join(task_dir, 'video.mp4')))

    def test_invalid_or_zero_retention_is_noop(self):
        task_dir = self._make_task_dir('old-task')
        self._write(os.path.join(task_dir, 'video.mp4'))
        self.namespace['get_all_tasks'] = lambda: [
            {'id': 'old-task', 'status': 'completed', 'updated_at': '2026-09-01 00:00:00'},
        ]

        self.assertEqual(self.cleanup(0), (0, 0))
        self.assertEqual(self.cleanup('not-a-number'), (0, 0))
        self.assertTrue(os.path.exists(os.path.join(task_dir, 'video.mp4')))

    def test_missing_db_time_falls_back_to_file_mtime(self):
        task_dir = self._make_task_dir('no-time-task')
        media_path = os.path.join(task_dir, 'video.mp4')
        self._write(media_path)
        old_stamp = (datetime.now() - __import__('datetime').timedelta(days=5)).timestamp()
        os.utime(media_path, (old_stamp, old_stamp))
        self.namespace['get_all_tasks'] = lambda: [
            {'id': 'no-time-task', 'status': 'completed', 'updated_at': None},
        ]

        files_removed, _ = self.cleanup(72)

        self.assertEqual(files_removed, 1)
        self.assertFalse(os.path.exists(media_path))

    def test_task_dir_missing_in_downloads_skipped(self):
        # 目录已不存在的任务不应报错
        self.namespace['get_all_tasks'] = lambda: [
            {'id': 'ghost-task', 'status': 'completed', 'updated_at': '2026-09-01 00:00:00'},
        ]
        self.assertEqual(self.cleanup(72), (0, 0))


class ParseTaskDatetimeTests(unittest.TestCase):
    def setUp(self):
        self.parse = _load_cleanup_helpers()['_parse_task_datetime_for_cleanup']

    def test_space_and_iso_formats(self):
        from datetime import datetime as dt
        self.assertEqual(self.parse('2026-09-01 12:30:45'), dt(2026, 9, 1, 12, 30, 45))
        self.assertEqual(self.parse('2026-09-01T12:30:45'), dt(2026, 9, 1, 12, 30, 45))
        self.assertEqual(self.parse('2026-09-01T12:30:45.123456'), dt(2026, 9, 1, 12, 30, 45))

    def test_invalid_values_return_none(self):
        self.assertIsNone(self.parse(None))
        self.assertIsNone(self.parse(''))
        self.assertIsNone(self.parse('garbage'))


if __name__ == '__main__':
    unittest.main()
