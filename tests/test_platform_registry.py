import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.platform_registry import (
    PLATFORMS,
    normalize_upload_targets,
    get_pending_platforms,
    is_sau_platform,
    is_native_platform,
    platform_has_partition,
    sau_platforms,
    migrate_legacy_upload_target,
)


class TestPlatformRegistry(unittest.TestCase):
    def test_known_platforms_present(self):
        for p in ['acfun', 'bilibili', 'douyin', 'kuaishou',
                 'xiaohongshu', 'tencent', 'baijiahao', 'tiktok']:
            self.assertIn(p, PLATFORMS)

    def test_native_vs_sau(self):
        self.assertTrue(is_native_platform('acfun'))
        self.assertTrue(is_native_platform('bilibili'))
        self.assertTrue(is_sau_platform('douyin'))
        self.assertFalse(is_sau_platform('acfun'))

    def test_partition_flags(self):
        self.assertTrue(platform_has_partition('acfun'))
        self.assertTrue(platform_has_partition('bilibili'))
        self.assertFalse(platform_has_partition('douyin'))

    def test_normalize_filters_invalid_and_preserves_order(self):
        self.assertEqual(normalize_upload_targets(['bilibili', 'douyin', 'nonsense']),
                         ['bilibili', 'douyin'])

    def test_normalize_default_empty(self):
        self.assertEqual(normalize_upload_targets(None), [])
        self.assertEqual(normalize_upload_targets(''), [])
        self.assertEqual(normalize_upload_targets('both'), ['acfun', 'bilibili'])

    def test_normalize_from_comma_string(self):
        self.assertEqual(normalize_upload_targets('bilibili,douyin'),
                         ['bilibili', 'douyin'])

    def test_sau_platforms_list(self):
        self.assertIn('douyin', sau_platforms())
        self.assertNotIn('acfun', sau_platforms())

    def test_get_pending_platforms_skips_done(self):
        task = {'sau_upload_responses': '{"douyin": {"url":"x"}}'}
        self.assertEqual(get_pending_platforms(task, ['douyin', 'kuaishou']),
                         ['kuaishou'])

    def test_get_pending_platforms_native(self):
        task = {'acfun_upload_response': '{"x":1}'}
        self.assertEqual(get_pending_platforms(task, ['acfun', 'bilibili']),
                         ['bilibili'])

    def test_migrate_legacy_enum(self):
        self.assertEqual(migrate_legacy_upload_target('acfun'), ['acfun'])
        self.assertEqual(migrate_legacy_upload_target('both'),
                         ['acfun', 'bilibili'])
        self.assertEqual(migrate_legacy_upload_target(None), [])
        self.assertEqual(migrate_legacy_upload_target('garbage'), [])


if __name__ == '__main__':
    unittest.main()
