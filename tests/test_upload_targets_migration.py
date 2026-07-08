import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.config_manager import DEFAULT_CONFIG, _normalize_upload_targets_value


class TestUploadTargetsConfig(unittest.TestCase):
    def test_defaults_present(self):
        self.assertIn('UPLOAD_TARGETS', DEFAULT_CONFIG)
        self.assertEqual(DEFAULT_CONFIG['UPLOAD_TARGETS'], ['acfun'])
        self.assertIn('SAU_BIN', DEFAULT_CONFIG)
        self.assertIn('SAU_UPLOAD_TIMEOUT_SECONDS', DEFAULT_CONFIG)
        for p in ['DOUYIN', 'KUAISHOU', 'XIAOHONGSHU', 'TENCENT', 'BAIJIAHAO', 'TIKTOK']:
            self.assertIn(f'SAU_ACCOUNT_{p}', DEFAULT_CONFIG)

    def test_normalize_value(self):
        self.assertEqual(_normalize_upload_targets_value(['acfun', 'douyin']),
                         ['acfun', 'douyin'])
        self.assertEqual(_normalize_upload_targets_value('bilibili,douyin'),
                         ['bilibili', 'douyin'])
        self.assertEqual(_normalize_upload_targets_value('both'),
                         ['acfun', 'bilibili'])
        self.assertEqual(_normalize_upload_targets_value(''), [])
        self.assertEqual(_normalize_upload_targets_value(None), [])
        self.assertEqual(_normalize_upload_targets_value(['nonsense']), [])


if __name__ == '__main__':
    unittest.main()
