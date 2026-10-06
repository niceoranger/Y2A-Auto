"""单视频/播放列表 URL 安全校验 + 密钥类配置留空保留语义的测试。

覆盖 2026-10-06 安全修复:
- _is_safe_video_url: 手动任务入口的 flag injection 防护(域名白名单+视频ID校验)
- _is_safe_playlist_url: 抽取公共校验后的行为回归
- update_config: 密钥字段提交空值表示"保持不变"
"""

import ast
import logging
import os
import pathlib
import re
import sys
import types
import unittest
from urllib.parse import parse_qs, urlparse


def _load_url_safety_functions():
    """AST 抽取校验函数,youtube_handler 模块级依赖(flask等)不进测试进程。"""
    module_path = pathlib.Path(__file__).resolve().parents[1] / "modules" / "youtube_handler.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))

    function_names = {"_normalize_youtube_url", "_is_safe_playlist_url", "_is_safe_video_url"}
    variable_names = {"_YOUTUBE_PLAYLIST_ID_PATTERN", "_YOUTUBE_VIDEO_ID_PATTERN"}
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in function_names:
            selected.append(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in variable_names:
                    selected.append(node)

    isolated = ast.Module(body=selected, type_ignores=[])
    namespace = {"re": re, "urlparse": urlparse, "parse_qs": parse_qs}
    exec(compile(isolated, str(module_path), "exec"), namespace)
    return namespace


_NS = _load_url_safety_functions()
_is_safe_video_url = _NS["_is_safe_video_url"]
_is_safe_playlist_url = _NS["_is_safe_playlist_url"]

_VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class IsSafeVideoUrlTest(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("test-url-safety")

    def test_accepts_standard_watch_url(self):
        self.assertEqual(_is_safe_video_url(_VIDEO_URL, self.logger), _VIDEO_URL)

    def test_accepts_watch_url_with_extra_params(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s&list=PLxyz"
        self.assertEqual(_is_safe_video_url(url, self.logger), url)

    def test_accepts_short_link(self):
        url = "https://youtu.be/dQw4w9WgXcQ"
        self.assertEqual(_is_safe_video_url(url, self.logger), url)

    def test_accepts_shorts_live_embed_v_paths(self):
        for path in ("shorts", "live", "embed", "v"):
            with self.subTest(path=path):
                url = f"https://www.youtube.com/{path}/dQw4w9WgXcQ"
                self.assertEqual(_is_safe_video_url(url, self.logger), url)

    def test_accepts_music_subdomain(self):
        url = "https://music.youtube.com/watch?v=dQw4w9WgXcQ"
        self.assertEqual(_is_safe_video_url(url, self.logger), url)

    def test_accepts_schemeless_url_by_prepending_https(self):
        self.assertEqual(
            _is_safe_video_url("www.youtube.com/watch?v=dQw4w9WgXcQ", self.logger),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

    def test_rejects_leading_dash_flag_injection(self):
        for payload in ("--exec=touch /tmp/pwn", "--config-location=/tmp/x", "-o /tmp/x"):
            with self.subTest(payload=payload):
                self.assertIsNone(_is_safe_video_url(payload, self.logger))

    def test_rejects_non_youtube_host(self):
        self.assertIsNone(_is_safe_video_url("https://evil.com/watch?v=dQw4w9WgXcQ", self.logger))

    def test_rejects_userinfo(self):
        self.assertIsNone(
            _is_safe_video_url("https://user:pass@youtube.com/watch?v=dQw4w9WgXcQ", self.logger)
        )

    def test_rejects_missing_or_malformed_video_id(self):
        for url in (
            "https://www.youtube.com/watch",
            "https://www.youtube.com/watch?v=",
            "https://youtu.be/short",
            "https://www.youtube.com/",
        ):
            with self.subTest(url=url):
                self.assertIsNone(_is_safe_video_url(url, self.logger))

    def test_rejects_oversized_url(self):
        self.assertIsNone(_is_safe_video_url("https://youtu.be/dQw4w9WgXcQ?" + "a" * 3000, self.logger))


class IsSafePlaylistUrlRegressionTest(unittest.TestCase):
    """抽取公共校验后,播放列表校验行为必须与之前一致。"""

    def setUp(self):
        self.logger = logging.getLogger("test-url-safety")

    def test_accepts_valid_playlist_url(self):
        url = "https://www.youtube.com/playlist?list=PL1234567890abcdefgh"
        self.assertEqual(_is_safe_playlist_url(url, self.logger), url)

    def test_rejects_playlist_path_without_list_param(self):
        self.assertIsNone(_is_safe_playlist_url("https://www.youtube.com/playlist", self.logger))

    def test_rejects_non_youtube_host(self):
        self.assertIsNone(
            _is_safe_playlist_url("https://evil.com/playlist?list=PL123", self.logger)
        )


def _import_config_manager():
    """真实导入 modules.config_manager。

    全量跑测试时,先运行的测试可能已向 sys.modules 装入只含 load_config
    的桩模块;检测到桩时移除后重导,保证拿到带 update_config 的真实模块。
    仅在缺 PIL 等重依赖时打桩 modules.utils。
    """
    def _looks_real(cm):
        return all(hasattr(cm, attr) for attr in ("update_config", "save_config", "DEFAULT_CONFIG"))

    import modules.config_manager as cm
    if _looks_real(cm):
        return cm

    sys.modules.pop("modules.config_manager", None)
    try:
        import modules.config_manager as cm
        if _looks_real(cm):
            return cm
    except ModuleNotFoundError:
        pass
    modules_utils = types.ModuleType("modules.utils")
    modules_utils.get_app_subdir = lambda name: os.path.join(
        os.getcwd(), "temp", "unit-tests", name
    )
    sys.modules["modules.utils"] = modules_utils
    sys.modules.pop("modules.config_manager", None)
    import modules.config_manager as cm
    return cm


class SecretConfigPreserveTest(unittest.TestCase):
    """密钥字段设置页不回显,保存提交空值必须保留已存值,不得清空。"""

    def setUp(self):
        self.cm = _import_config_manager()
        self._orig_load = self.cm.load_config
        self._orig_save = self.cm.save_config
        base = dict(self.cm.DEFAULT_CONFIG)
        base.update(
            {
                "OPENAI_API_KEY": "sk-existing",
                "YOUTUBE_API_KEY": "ya-existing",
                "password": "pw-existing",
                "MAX_CONCURRENT_TASKS": 2,
            }
        )
        self.cm.load_config = lambda: dict(base)
        self.cm.save_config = lambda cfg, path=None: None

    def tearDown(self):
        self.cm.load_config = self._orig_load
        self.cm.save_config = self._orig_save

    def test_blank_secret_submission_keeps_existing_value(self):
        result = self.cm.update_config(
            {"OPENAI_API_KEY": "", "YOUTUBE_API_KEY": "   ", "password": ""}
        )
        self.assertEqual(result["OPENAI_API_KEY"], "sk-existing")
        self.assertEqual(result["YOUTUBE_API_KEY"], "ya-existing")
        self.assertEqual(result["password"], "pw-existing")

    def test_new_secret_value_overrides(self):
        result = self.cm.update_config({"OPENAI_API_KEY": "sk-new", "password": "pw-new"})
        self.assertEqual(result["OPENAI_API_KEY"], "sk-new")
        self.assertEqual(result["password"], "pw-new")

    def test_non_secret_fields_still_update_normally(self):
        result = self.cm.update_config({"MAX_CONCURRENT_TASKS": "5"})
        self.assertEqual(str(result["MAX_CONCURRENT_TASKS"]), "5")


if __name__ == "__main__":
    unittest.main()
