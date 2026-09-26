import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

try:
    from test_youtube_monitor_config_sql_dedup import _install_stubs
except ImportError:  # 直接以脚本方式运行时的兜底
    _install_stubs = None

if _install_stubs is not None:
    try:
        from modules.youtube_monitor import API_INIT_STATUS_MISSING_API_KEY, YouTubeMonitor
    except ModuleNotFoundError:
        _install_stubs()
        sys.modules.pop("modules.youtube_monitor", None)
        from modules.youtube_monitor import API_INIT_STATUS_MISSING_API_KEY, YouTubeMonitor


def _api_video(video_id, view_count, title=None):
    """构造 _filter_videos 输入格式的 API 原始条目"""
    return {
        'id': video_id,
        'snippet': {
            'title': title or f'video-{video_id}',
            'channelTitle': 'test-channel',
            'channelId': 'chan-1',
            'publishedAt': '2026-09-23T00:00:00Z',
            'liveBroadcastContent': 'none',
            'description': '',
            'tags': [],
        },
        'contentDetails': {'duration': 'PT10M'},
        'statistics': {
            'viewCount': str(view_count),
            'likeCount': '10',
            'commentCount': '1',
        },
    }


class YouTubeMonitorTopNByViewsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.get_app_subdir_patcher = patch(
            "modules.youtube_monitor.get_app_subdir",
            side_effect=self._get_app_subdir,
        )
        self.init_api_patcher = patch.object(
            YouTubeMonitor,
            "_init_youtube_api",
            return_value=(False, API_INIT_STATUS_MISSING_API_KEY),
        )
        self.get_app_subdir_patcher.start()
        self.init_api_patcher.start()
        self.monitor = YouTubeMonitor()
        # run_monitor 入口要求 API 对象非空；抓取方法在各测试中单独打桩
        self.monitor.youtube = object()
        self.monitor._last_api_init_error = None
        self.added_task_urls = []

    def tearDown(self):
        try:
            scheduler = getattr(self.monitor, "scheduler", None)
            if scheduler and getattr(scheduler, "running", False):
                scheduler.shutdown(wait=False)
        finally:
            self.get_app_subdir_patcher.stop()
            self.init_api_patcher.stop()
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _get_app_subdir(self, subdir_name):
        path = os.path.join(self.tmpdir, subdir_name)
        os.makedirs(path, exist_ok=True)
        return path

    def _patch_add_task(self):
        def _fake_add_task(url, *args, **kwargs):
            self.added_task_urls.append(url)
            return f"task-{len(self.added_task_urls)}"

        return patch("modules.youtube_monitor.add_task", side_effect=_fake_add_task)

    def _create_config(self, **overrides):
        config_data = {
            'name': 'top-n-test',
            'channel_mode': 'latest',
            'auto_add_to_tasks': True,
            'rate_limit_requests': 20,
            'max_results': 10,
        }
        config_data.update(overrides)
        return self.monitor.create_monitor_config(config_data)

    def test_config_field_roundtrip_and_default(self):
        config_id = self._create_config(top_n_by_views=3)

        config = self.monitor.get_monitor_config(config_id)

        self.assertEqual(config['top_n_by_views'], 3)

        omitted_id = self._create_config(name='default-check')
        self.assertEqual(self.monitor.get_monitor_config(omitted_id)['top_n_by_views'], 0)

    def test_select_top_by_views_picks_highest_unprocessed(self):
        config_id = self._create_config(top_n_by_views=2)
        videos = [
            {'id': 'a', 'view_count': 9000},
            {'id': 'b', 'view_count': 1000},
            {'id': 'c', 'view_count': 5000},
            {'id': 'd', 'view_count': 3000},
        ]
        # 已处理过的高播放量视频不占名额，由下一个补位
        self.monitor._save_video_history(
            {'id': 'a', 'video_type': 'video', 'title': 'a', 'channel_title': 'c',
             'view_count': 9000, 'like_count': 0, 'comment_count': 0,
             'duration': 'PT10M', 'published_at': '2026-09-23T00:00:00Z'},
            config_id, auto_add_to_tasks=False,
        )

        selected = self.monitor._select_top_by_views(videos, config_id, 2)

        self.assertEqual(selected, {'c', 'd'})

    def test_run_monitor_adds_only_top_n_and_skips_rest(self):
        config_id = self._create_config(top_n_by_views=2)
        api_videos = [
            _api_video('low1', 1000),
            _api_video('high', 9000),
            _api_video('mid', 5000),
            _api_video('low2', 3000),
        ]

        with patch.object(self.monitor, "_fetch_trending_videos", return_value=api_videos):
            with self._patch_add_task():
                ok, message = self.monitor.run_monitor(config_id)

        self.assertTrue(ok, message)
        # 只入队播放量最高的两个（9000、5000），按播放量降序
        self.assertEqual(self.added_task_urls, [
            'https://www.youtube.com/watch?v=high',
            'https://www.youtube.com/watch?v=mid',
        ])

        # 被淘汰的视频不应写入历史，也不建任务
        with sqlite3.connect(self.monitor.db_path) as conn:
            rows = conn.execute(
                'SELECT video_id, added_to_tasks FROM monitor_history WHERE config_id = ? ORDER BY video_id',
                (config_id,),
            ).fetchall()
        self.assertEqual([r[0] for r in rows], ['high', 'mid'])

    def test_run_monitor_second_run_adds_nothing(self):
        config_id = self._create_config(top_n_by_views=2)
        api_videos = [
            _api_video('high', 9000),
            _api_video('mid', 5000),
        ]

        with patch.object(self.monitor, "_fetch_trending_videos", return_value=api_videos):
            with self._patch_add_task():
                self.monitor.run_monitor(config_id)
                self.monitor.run_monitor(config_id)

        self.assertEqual(len(self.added_task_urls), 2)

    def test_top_n_zero_keeps_legacy_behavior(self):
        config_id = self._create_config(top_n_by_views=0)
        api_videos = [
            _api_video('low1', 1000),
            _api_video('high', 9000),
            _api_video('mid', 5000),
        ]

        with patch.object(self.monitor, "_fetch_trending_videos", return_value=api_videos):
            with self._patch_add_task():
                ok, message = self.monitor.run_monitor(config_id)

        self.assertTrue(ok, message)
        # 关闭优选时保持原行为：全部入队
        self.assertEqual(len(self.added_task_urls), 3)


if __name__ == "__main__":
    unittest.main()
