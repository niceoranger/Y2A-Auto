#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""平台注册表 —— 多平台上传的单一事实源。

加一个 sau 平台 = 在 PLATFORMS 加一行(本期抖音之外的 sau 平台由 Phase 2 仅配置启用)。
"""
import json

# kind: native = Y2A 自带上传器; sau = 通过 social-auto-upload 子进程上传
PLATFORMS = {
    'acfun':       {'kind': 'native', 'has_partition': True},
    'bilibili':    {'kind': 'native', 'has_partition': True},
    'douyin':      {'kind': 'sau', 'sau_name': 'douyin',      'has_partition': False},
    'kuaishou':    {'kind': 'sau', 'sau_name': 'kuaishou',    'has_partition': False},
    'xiaohongshu': {'kind': 'sau', 'sau_name': 'xiaohongshu', 'has_partition': False},
    'tencent':     {'kind': 'sau', 'sau_name': 'tencent',     'has_partition': False},
    'baijiahao':   {'kind': 'sau', 'sau_name': 'baijiahao',   'has_partition': False},
    'tiktok':      {'kind': 'sau', 'sau_name': 'tk',          'has_partition': False},
}

_LEGACY_ENUM_MAP = {
    'acfun': ['acfun'],
    'bilibili': ['bilibili'],
    'both': ['acfun', 'bilibili'],
}


def is_native_platform(p):
    return PLATFORMS.get(p, {}).get('kind') == 'native'


def is_sau_platform(p):
    return PLATFORMS.get(p, {}).get('kind') == 'sau'


def platform_has_partition(p):
    return bool(PLATFORMS.get(p, {}).get('has_partition'))


def sau_platforms():
    return [p for p, info in PLATFORMS.items() if info.get('kind') == 'sau']


def sau_name_for(p):
    return PLATFORMS.get(p, {}).get('sau_name', p)


def migrate_legacy_upload_target(value):
    """旧单枚举 acfun|bilibili|both → 平台列表。非法/空返回 []。"""
    v = str(value or '').strip().lower()
    return list(_LEGACY_ENUM_MAP.get(v, []))


def normalize_upload_targets(raw):
    """归一化为合法平台列表,保序去重,过滤非法值。

    接受:list、逗号分隔字符串、旧枚举('both')、None/''。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip().lower()
        if not s:
            return []
        if s in _LEGACY_ENUM_MAP:
            return list(_LEGACY_ENUM_MAP[s])
        items = [x.strip().lower() for x in raw.split(',')]
    elif isinstance(raw, (list, tuple)):
        items = [str(x).strip().lower() for x in raw]
    else:
        return []

    seen = set()
    out = []
    for it in items:
        if it and it in PLATFORMS and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _task_has_platform_upload_response(task, platform):
    if not task:
        return False
    if is_native_platform(platform):
        if platform == 'bilibili':
            return bool(task.get('bilibili_upload_response'))
        return bool(task.get('acfun_upload_response'))
    # sau 平台
    raw = task.get('sau_upload_responses')
    if not raw:
        return False
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return platform in (data or {})
    except (ValueError, TypeError):
        return False


def get_pending_platforms(task, targets):
    """从 targets 列表中过滤掉已上传成功的平台。"""
    norm = normalize_upload_targets(targets)
    return [p for p in norm if not _task_has_platform_upload_response(task, p)]
