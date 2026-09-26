"""OCR 误检校验:识别文本非空/非单字符 + 置信度阈值,聚类前过滤。"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.ocr_runner import (
    _ocr_predict_pages,
    count_word_chars,
    filter_valid_text_dets,
    is_valid_text_det,
)


class TestTextValidation(unittest.TestCase):
    def test_count_word_chars(self):
        self.assertEqual(count_word_chars("hello world"), 10)
        self.assertEqual(count_word_chars("你好,世界!"), 4)
        self.assertEqual(count_word_chars(" A-1 "), 2)
        self.assertEqual(count_word_chars(None), 0)
        self.assertEqual(count_word_chars("..."), 0)

    def test_is_valid_text_det(self):
        # 误检特征:空文本、纯标点、单个字符
        self.assertFalse(is_valid_text_det(""))
        self.assertFalse(is_valid_text_det(None))
        self.assertFalse(is_valid_text_det("..."))
        self.assertFalse(is_valid_text_det("m"))
        self.assertFalse(is_valid_text_det("好"))
        # 真字幕特征:至少两个文字字符
        self.assertTrue(is_valid_text_det("OK"))
        self.assertTrue(is_valid_text_det("FORT TRUMP"))
        self.assertTrue(is_valid_text_det("美联储加息"))

    def test_filter_valid_text_dets(self):
        dets = [
            # 有效:有文本且置信度达标
            {"t": 1.0, "box": [0.1, 0.8, 0.3, 0.05], "score": 0.95, "text": "POLISH PRESIDENT"},
            # 无效:高置信度但识别不出文本(胸针/麦克风类误检)
            {"t": 2.0, "box": [0.4, 0.8, 0.03, 0.04], "score": 0.998, "text": ""},
            # 无效:单字符
            {"t": 3.0, "box": [0.5, 0.8, 0.02, 0.04], "score": 0.9, "text": "W"},
            # 无效:有文本但置信度低于阈值
            {"t": 4.0, "box": [0.6, 0.8, 0.3, 0.05], "score": 0.3, "text": "something"},
            # 无 text 键(旧数据)按无效处理
            {"t": 5.0, "box": [0.7, 0.8, 0.3, 0.05], "score": 0.99},
        ]
        kept = filter_valid_text_dets(dets, min_score=0.6)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["text"], "POLISH PRESIDENT")

    def test_filter_threshold_zero_keeps_all_text_dets(self):
        dets = [
            {"t": 1.0, "box": [0.1, 0.8, 0.3, 0.05], "score": 0.1, "text": "low score"},
            {"t": 2.0, "box": [0.4, 0.8, 0.3, 0.05], "score": 0.2, "text": "also low"},
        ]
        self.assertEqual(len(filter_valid_text_dets(dets, min_score=0.0)), 2)


class _FakeRapidOcr:
    """模拟 RapidOCR 引擎:直接可调用,返回 (dets, elapsed)。"""

    def __init__(self, dets):
        self._dets = dets

    def __call__(self, path):
        return self._dets, 0.1


_BOX = [[10, 10], [100, 10], [100, 40], [10, 40]]


class TestPredictPagesCarriesText(unittest.TestCase):
    """RapidOCR 契约:det = [box, text, score] → (box, score, text)。"""

    def test_carries_text_and_score(self):
        ocr = _FakeRapidOcr([
            [_BOX, "FORT TRUMP", 0.97],
        ])
        items = _ocr_predict_pages(ocr, "fake.jpg")
        self.assertEqual(len(items), 1)
        box_pts, score, text = items[0]
        self.assertAlmostEqual(score, 0.97)
        self.assertEqual(text, "FORT TRUMP")

    def test_empty_text_high_score_is_garbage(self):
        # 高置信度但识别不出文本(胸针/麦克风类误检):文本为空,靠校验层拦下
        det_box = [[10, 50], [80, 50], [80, 80], [10, 80]]
        ocr = _FakeRapidOcr([[det_box, "", 0.998]])
        items = _ocr_predict_pages(ocr, "fake.jpg")
        self.assertEqual(len(items), 1)
        _, score, text = items[0]
        self.assertEqual(text, "")
        self.assertFalse(is_valid_text_det(text))

    def test_none_score_treated_as_zero(self):
        ocr = _FakeRapidOcr([
            [_BOX, "MARKETS", None],
        ])
        items = _ocr_predict_pages(ocr, "fake.jpg")
        self.assertEqual(items[0][1], 0.0)
        self.assertEqual(items[0][2], "MARKETS")


if __name__ == "__main__":
    unittest.main()
