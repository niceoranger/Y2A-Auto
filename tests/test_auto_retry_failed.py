"""失败任务自动重试 + 调度器 misfire 宽限期。"""
import ast
import pathlib
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

TASK_MANAGER_PATH = pathlib.Path(__file__).resolve().parents[1] / "modules" / "task_manager.py"
YOUTUBE_MONITOR_PATH = pathlib.Path(__file__).resolve().parents[1] / "modules" / "youtube_monitor.py"


def _load_auto_retry_helpers():
    """AST 抽取纯函数,避免整体导入 task_manager(重依赖)。"""
    source = TASK_MANAGER_PATH.read_text(encoding="utf-8")
    module_ast = ast.parse(source, filename=str(TASK_MANAGER_PATH))
    selected = [
        node for node in module_ast.body
        if isinstance(node, ast.FunctionDef) and node.name == '_filter_auto_retry_candidates'
    ]
    namespace = {'logger': MagicMock()}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(TASK_MANAGER_PATH), 'exec'), namespace)
    return namespace['_filter_auto_retry_candidates']


class TestFilterAutoRetryCandidates(unittest.TestCase):
    def setUp(self):
        self.filter_candidates = _load_auto_retry_helpers()

    def test_picks_tasks_under_limit(self):
        tasks = [
            {'id': 'a', 'retry_count': 0},
            {'id': 'b', 'retry_count': 1},
            {'id': 'c', 'retry_count': 2},
        ]
        picked = self.filter_candidates(tasks, max_retries=2)
        self.assertEqual([t['id'] for t in picked], ['a', 'b'])

    def test_missing_or_invalid_retry_count_treated_as_zero(self):
        tasks = [
            {'id': 'a'},                      # 旧数据无该列
            {'id': 'b', 'retry_count': None},
            {'id': 'c', 'retry_count': 'x'},  # 脏数据
        ]
        picked = self.filter_candidates(tasks, max_retries=2)
        self.assertEqual([t['id'] for t in picked], ['a', 'b', 'c'])

    def test_string_counts_work(self):
        picked = self.filter_candidates([{'id': 'a', 'retry_count': '1'}], max_retries=2)
        self.assertEqual(len(picked), 1)

    def test_empty_or_none_input(self):
        self.assertEqual(self.filter_candidates([], 2), [])
        self.assertEqual(self.filter_candidates(None, 2), [])


class TestSchedulerWiring(unittest.TestCase):
    """验证调度器带 misfire 宽限期(错过触发时间不再整轮跳过)。"""

    def test_youtube_monitor_scheduler_has_misfire_grace(self):
        source = YOUTUBE_MONITOR_PATH.read_text(encoding="utf-8")
        self.assertRegex(
            source,
            r"BackgroundScheduler\(\s*misfire_grace_time\s*=\s*(\d+)",
        )

    def test_task_processor_scheduler_has_misfire_grace(self):
        source = TASK_MANAGER_PATH.read_text(encoding="utf-8")
        # job_defaults 里必须给 misfire_grace_time(覆盖 pending 扫描/卡死恢复/自动重试)
        self.assertRegex(source, r"'misfire_grace_time':\s*\d+")

    def test_retry_count_column_migrated(self):
        source = TASK_MANAGER_PATH.read_text(encoding="utf-8")
        self.assertIn("ALTER TABLE tasks ADD COLUMN retry_count INTEGER DEFAULT 0", source)
        self.assertRegex(source, r"'retry_count':\s*'retry_count = \?'")

    def test_auto_retry_job_registered(self):
        source = TASK_MANAGER_PATH.read_text(encoding="utf-8")
        self.assertIn("def auto_retry_failed_tasks(config=None)", source)
        self.assertIn("id='auto_retry_failed'", source)

    def test_auto_retry_config_keys_exist(self):
        # 不直接 import modules.config_manager:共享打桩会替换该模块(测试顺序敏感)
        source = (pathlib.Path(__file__).resolve().parents[1] / "modules" / "config_manager.py").read_text(encoding="utf-8")
        self.assertRegex(source, r'"AUTO_RETRY_FAILED_MAX_RETRIES":\s*2')
        self.assertRegex(source, r'"AUTO_RETRY_FAILED_INTERVAL_SEC":\s*1800')


class TestMonitorSchedulerGraceIntegration(unittest.TestCase):
    """通过打桩调度器验证监控构造时带 misfire_grace_time。"""

    def test_monitor_scheduler_constructed_with_grace(self):
        try:
            from test_youtube_monitor_config_sql_dedup import _install_stubs
        except ImportError:
            self.skipTest("依赖共享打桩模块")

        try:
            from modules.youtube_monitor import API_INIT_STATUS_MISSING_API_KEY, YouTubeMonitor
        except ModuleNotFoundError:
            _install_stubs()
            sys.modules.pop("modules.youtube_monitor", None)
            from modules.youtube_monitor import API_INIT_STATUS_MISSING_API_KEY, YouTubeMonitor

        import os
        import shutil
        import tempfile
        from unittest.mock import patch

        tmpdir = tempfile.mkdtemp()

        def _fake_subdir(name):
            path = os.path.join(tmpdir, name)
            os.makedirs(path, exist_ok=True)
            return path

        monitor = None
        with patch("modules.youtube_monitor.get_app_subdir", side_effect=_fake_subdir), \
             patch.object(YouTubeMonitor, "_init_youtube_api",
                          return_value=(False, API_INIT_STATUS_MISSING_API_KEY)):
            try:
                monitor = YouTubeMonitor()
                if not hasattr(monitor.scheduler, 'init_kwargs'):
                    self.skipTest("非打桩调度器(真实环境),misfire 宽限期由源码断言测试覆盖")
                self.assertEqual(monitor.scheduler.init_kwargs.get('misfire_grace_time'), 300)
            finally:
                sched = getattr(monitor, 'scheduler', None)
                if sched and getattr(sched, 'running', False):
                    sched.shutdown(wait=False)
                shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
