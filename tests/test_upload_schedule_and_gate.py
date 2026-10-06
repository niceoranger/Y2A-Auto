"""定时分批上传 + 标题时长前缀 + 财经门禁的测试。

- check_and_flush_upload_batch:窗口触发/限量顺延/兜底清空/幂等/启动基线
- _apply_duration_prefix:分档/幂等/字段选择/缺文件兜底
- content_gate.is_finance_related:fail-open 与缓存
"""

import ast
import logging
import os
import pathlib
import sys
import threading
import types
import unittest
from datetime import datetime, timedelta


def _extract_functions(module_path, names):
    """AST 抽取指定函数(含 TaskProcessor 方法),保持源码级测试模式。"""
    source = pathlib.Path(__file__).resolve().parents[1] / module_path
    text = source.read_text(encoding='utf-8')
    tree = ast.parse(text, filename=str(source))
    selected = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
        if not isinstance(node, (ast.AsyncFunctionDef,))
    ]
    class_nodes = [
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == 'TaskProcessor'
    ]
    for cls in class_nodes:
        selected += [
            node for node in cls.body
            if isinstance(node, ast.FunctionDef) and node.name in names
        ]
    isolated = ast.Module(body=selected, type_ignores=[])
    return text, isolated, str(source)


_MODULE_FUNCS = {'_as_int', '_normalize_task_text'}
_METHOD_FUNCS = {
    'check_and_flush_upload_batch', '_upload_batch_allowed', '_parse_hhmm',
    '_get_upload_schedule_times', '_get_upload_final_time',
    '_get_upload_sort_time', '_apply_duration_prefix',
}

_text, _isolated, _source = _extract_functions(
    'modules/task_manager.py', _MODULE_FUNCS | _METHOD_FUNCS
)


class _FakeDateTime(datetime):
    """可控时钟:check_and_flush 按 datetime.now() 判定窗口。"""
    fixed_now = None

    @classmethod
    def now(cls):
        if cls.fixed_now is not None:
            return cls.fixed_now
        return super().now()


def _install_config_stub(times, final, batch_limit):
    """sys.modules 装入可控 config_manager 桩(方法体相对导入在调用时解析)。"""
    stub = types.ModuleType('modules.config_manager')
    stub.load_config = lambda: {
        'UPLOAD_SCHEDULE_TIMES': times,
        'UPLOAD_SCHEDULE_FINAL_TIME': final,
        'UPLOAD_SCHEDULE_BATCH_LIMIT': batch_limit,
    }
    saved = sys.modules.get('modules.config_manager')
    sys.modules['modules.config_manager'] = stub
    return saved


class UploadScheduleTestBase(unittest.TestCase):
    TASK_STATES = {'READY_FOR_UPLOAD': 'ready_for_upload', 'PENDING': 'pending'}

    def setUp(self):
        self.saved_cm = _install_config_stub('01:00,02:00', '', 2)
        self.updated = []      # (task_id, status) 调用记录
        self.started = []      # _check_and_start_next_pending_task 调用记录
        self._tasks = {}       # task_id -> task dict

        def get_tasks_by_status(status):
            return [dict(t) for t in self._tasks.values() if t['status'] == status]

        def update_task(task_id, **fields):
            self.updated.append((task_id, fields.get('status')))
            self._tasks[task_id].update(fields)

        ns = {
            '__package__': 'modules',
            '__name__': 'modules.task_manager_test_extract',
            'os': os,
            'threading': threading,
            'datetime': _FakeDateTime,
            'timedelta': timedelta,
            'timezone': __import__('datetime').timezone,
            'logging': logging,
            'logger': logging.getLogger('test_upload_schedule'),
            'TASK_STATES': self.TASK_STATES,
            'get_tasks_by_status': get_tasks_by_status,
            'update_task': update_task,
            'get_task': lambda task_id: self._tasks.get(task_id),
        }
        exec(compile(_isolated, _source, 'exec'), ns)

        outer = self

        class FakeProcessor:
            def __init__(self, config):
                self.config = config
                self._upload_batch_ids = set()
                self._last_upload_window = None
                self._upload_batch_lock = threading.Lock()

            def _check_and_start_next_pending_task(self):
                outer.started.append(True)

            def _get_video_duration(self, video_path, task_logger):
                return outer.durations.get(video_path)

        for name in _METHOD_FUNCS:
            # exec 已应用源码里的 @staticmethod 装饰器,直接赋值;
            # 再包一层 staticmethod() 在 py3.9 下会双重包装不可调用
            setattr(FakeProcessor, name, ns[name])
        self.ns = ns
        self.FakeProcessor = FakeProcessor
        _FakeDateTime.fixed_now = None
        self.durations = {}

    def tearDown(self):
        if self.saved_cm is not None:
            sys.modules['modules.config_manager'] = self.saved_cm
        else:
            sys.modules.pop('modules.config_manager', None)
        _FakeDateTime.fixed_now = None

    def _mk_task(self, task_id, status='ready_for_upload', sort_key=None, **extra):
        task = {'id': task_id, 'status': status, 'created_at': '2026-10-01 00:00:00'}
        task.update(extra)
        self._tasks[task_id] = task
        return task

    def _enable(self, **overrides):
        config = {'UPLOAD_SCHEDULE_ENABLED': True}
        config.update(overrides)
        return self.FakeProcessor(config)


class CheckAndFlushUploadBatchTest(UploadScheduleTestBase):
    def test_disabled_schedule_releases_backlog(self):
        """开关关闭=处理完即上传:被拦截积压的待上传任务应放行重入队列。"""
        proc = self.FakeProcessor({'UPLOAD_SCHEDULE_ENABLED': False})
        self._mk_task('t1')
        self._mk_task('t2')
        proc.check_and_flush_upload_batch()
        statuses = {tid: self._tasks[tid]['status'] for tid in ('t1', 't2')}
        self.assertEqual(set(statuses.values()), {'pending'})
        self.assertEqual(self.started, [True])
        # 再次 tick:无积压,空转
        self.updated.clear()
        proc.check_and_flush_upload_batch()
        self.assertEqual(self.updated, [])

    def test_disabled_schedule_noop_when_no_backlog(self):
        proc = self.FakeProcessor({'UPLOAD_SCHEDULE_ENABLED': False})
        proc.check_and_flush_upload_batch()
        self.assertEqual(self.updated, [])

    def test_first_tick_sets_baseline_without_backlog(self):
        proc = self._enable()
        self._mk_task('t1')
        _FakeDateTime.fixed_now = datetime(2026, 10, 6, 0, 30)
        proc.check_and_flush_upload_batch()
        # 启动基线:已过窗口不追旧账
        self.assertEqual(self.updated, [])
        self.assertIsNotNone(proc._last_upload_window)

    def test_window_enqueue_respects_batch_limit_and_order(self):
        proc = self._enable()
        # 下载完成时间排序:t3 最早
        for tid, mtime in (('t1', 300), ('t2', 200), ('t3', 100)):
            task = self._mk_task(tid)
            task['_sort_mtime'] = mtime
        real_getmtime = os.path.getmtime

        def fake_getmtime(path):
            for t in self._tasks.values():
                if t.get('video_path_local') == path:
                    return t['_sort_mtime']
            return real_getmtime(path)

        for tid in ('t1', 't2', 't3'):
            self._tasks[tid]['video_path_local'] = f'/tmp/{tid}.mp4'
        self.ns['os'] = types.SimpleNamespace(getmtime=fake_getmtime, path=os.path)

        _FakeDateTime.fixed_now = datetime(2026, 10, 6, 0, 30)
        proc.check_and_flush_upload_batch()  # 基线
        # _get_upload_sort_time 先 exists 再 getmtime,两者都要桩
        fake_path = types.SimpleNamespace(
            exists=lambda p: any(t.get('video_path_local') == p for t in self._tasks.values()),
            getmtime=fake_getmtime,
        )
        self.ns['os'] = types.SimpleNamespace(path=fake_path)
        _FakeDateTime.fixed_now = datetime(2026, 10, 6, 1, 30)  # 跨过 01:00
        proc.check_and_flush_upload_batch()

        enqueued = [tid for tid, st in self.updated if st == 'pending']
        self.assertEqual(enqueued, ['t3', 't2'])  # 早下载早传,限量 2
        self.assertEqual(self._tasks['t1']['status'], 'ready_for_upload')  # 顺延
        self.assertEqual(self.started, [True])

    def test_same_window_tick_is_idempotent(self):
        proc = self._enable()
        self._mk_task('t1')
        _FakeDateTime.fixed_now = datetime(2026, 10, 6, 0, 30)
        proc.check_and_flush_upload_batch()
        _FakeDateTime.fixed_now = datetime(2026, 10, 6, 1, 30)
        proc.check_and_flush_upload_batch()
        first = list(self.updated)
        proc.check_and_flush_upload_batch()  # 同窗口重复 tick
        self.assertEqual(self.updated, first)

    def test_next_window_flushes_overflow(self):
        proc = self._enable()
        for tid in ('t1', 't2', 't3'):
            self._mk_task(tid)
        _FakeDateTime.fixed_now = datetime(2026, 10, 6, 0, 30)
        proc.check_and_flush_upload_batch()
        _FakeDateTime.fixed_now = datetime(2026, 10, 6, 1, 30)
        proc.check_and_flush_upload_batch()
        _FakeDateTime.fixed_now = datetime(2026, 10, 6, 2, 30)  # 第二个窗口
        proc.check_and_flush_upload_batch()
        statuses = {tid: self._tasks[tid]['status'] for tid in ('t1', 't2', 't3')}
        self.assertEqual(set(statuses.values()), {'pending'})

    def test_final_time_flushes_all_without_limit(self):
        self.saved_cm and sys.modules.pop('modules.config_manager', None)
        saved = _install_config_stub('01:00', '02:00', 1)
        try:
            proc = self._enable()
            for i in range(4):
                self._mk_task(f't{i}')
            _FakeDateTime.fixed_now = datetime(2026, 10, 6, 0, 30)
            proc.check_and_flush_upload_batch()
            _FakeDateTime.fixed_now = datetime(2026, 10, 6, 2, 30)  # 兜底时刻
            proc.check_and_flush_upload_batch()
            self.assertEqual(len(self.updated), 4)  # 不限量一次清空
        finally:
            sys.modules['modules.config_manager'] = saved

    def test_upload_batch_allowed_gate(self):
        proc = self._enable()
        proc._upload_batch_ids.add('t1')
        self.assertTrue(proc._upload_batch_allowed('t1'))
        self.assertFalse(proc._upload_batch_allowed('t2'))


class ApplyDurationPrefixTest(UploadScheduleTestBase):
    PREFIX_CFG = {
        'UPLOAD_TITLE_PREFIX_ENABLED': True,
        'UPLOAD_TITLE_PREFIX_SHORT': '【快讯】',
        'UPLOAD_TITLE_PREFIX_SHORT_MAX_MIN': 10,
        'UPLOAD_TITLE_PREFIX_MEDIUM': '【解析】',
        'UPLOAD_TITLE_PREFIX_MEDIUM_MAX_MIN': 30,
        'UPLOAD_TITLE_PREFIX_LONG': '【专家解读】',
    }

    def _run(self, title_original='原标题', translated='', seconds=300, video_exists=True):
        proc = self._enable(**self.PREFIX_CFG)
        self._mk_task('t1', video_title_original=title_original,
                      video_title_translated=translated)

        if video_exists:
            self._tasks['t1']['video_path_local'] = '/tmp/fake.mp4'
            self.ns['os'] = types.SimpleNamespace(
                path=types.SimpleNamespace(
                    exists=lambda p: p == '/tmp/fake.mp4',
                    getmtime=os.path.getmtime,
                )
            )
            self.durations['/tmp/fake.mp4'] = seconds
        else:
            self._tasks['t1']['video_path_local'] = '/nonexistent.mp4'
        log = logging.getLogger('test_prefix')
        proc._apply_duration_prefix('t1', log)
        return self._tasks['t1']

    def test_short_video_gets_short_prefix_on_original_field(self):
        task = self._run(seconds=300)  # 5 分钟
        self.assertEqual(task['video_title_original'], '【快讯】原标题')

    def test_translated_field_preferred_when_present(self):
        task = self._run(translated='译名标题', seconds=25 * 60)
        self.assertEqual(task['video_title_translated'], '【解析】译名标题')
        self.assertEqual(task['video_title_original'], '原标题')

    def test_long_video_gets_long_prefix(self):
        task = self._run(seconds=45 * 60)
        self.assertEqual(task['video_title_original'], '【专家解读】原标题')

    def test_existing_prefix_is_idempotent(self):
        task = self._run(title_original='【快讯】原标题', seconds=300)
        self.assertEqual(task['video_title_original'], '【快讯】原标题')

    def test_missing_video_keeps_title(self):
        task = self._run(video_exists=False)
        self.assertEqual(task['video_title_original'], '原标题')

    def test_disabled_is_noop(self):
        proc = self.FakeProcessor({'UPLOAD_TITLE_PREFIX_ENABLED': False})
        self._mk_task('t1', video_title_original='原标题')
        proc._apply_duration_prefix('t1', logging.getLogger('test_prefix'))
        self.assertEqual(self._tasks['t1']['video_title_original'], '原标题')


class ContentGateTest(unittest.TestCase):
    def setUp(self):
        from modules import content_gate as cg
        self.cg = cg
        self._orig_classify = cg._qwen_classify
        self._orig_cache = dict(cg._cache)
        cg._cache.clear()

    def tearDown(self):
        self.cg._qwen_classify = self._orig_classify
        self.cg._cache.clear()
        self.cg._cache.update(self._orig_cache)

    def test_empty_title_passes(self):
        self.assertTrue(self.cg.is_finance_related('   '))

    def test_model_failure_fails_open(self):
        def down(title):
            raise RuntimeError('model unreachable')
        self.cg._qwen_classify = down
        self.assertTrue(self.cg.is_finance_related('Some breaking news'))

    def test_model_verdict_respected_and_cached(self):
        calls = []
        self.cg._qwen_classify = lambda t: calls.append(t) or False
        self.assertFalse(self.cg.is_finance_related('NBA finals recap'))
        self.assertFalse(self.cg.is_finance_related('NBA finals recap'))  # 第二次走缓存
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
