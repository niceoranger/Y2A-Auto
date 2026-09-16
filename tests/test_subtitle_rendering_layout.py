import ast
import logging
import os
import pathlib
import unicodedata
import unittest


def _load_task_processor_class():
    module_path = pathlib.Path(__file__).resolve().parents[1] / 'modules' / 'task_manager.py'
    source = module_path.read_text(encoding='utf-8')
    module_ast = ast.parse(source, filename=str(module_path))
    selected = [
        node for node in module_ast.body
        if isinstance(node, ast.ClassDef) and node.name == 'TaskProcessor'
    ]
    isolated_module = ast.Module(body=selected, type_ignores=[])
    namespace = {
        'os': os,
        'unicodedata': unicodedata,
        'logger': logging.getLogger('test_task_processor_layout'),
    }
    exec(compile(isolated_module, str(module_path), 'exec'), namespace)
    return namespace['TaskProcessor']


TaskProcessor = _load_task_processor_class()


class SubtitleRenderingLayoutTests(unittest.TestCase):
    @staticmethod
    def _extract_ass_lines(text):
        normalized = str(text or '')
        if normalized.startswith(r'{\fs'):
            closing_index = normalized.find('}')
            if closing_index >= 0:
                normalized = normalized[closing_index + 1:]
        return [line for line in normalized.split(r'\N') if line]

    @staticmethod
    def _extract_dialogue_texts(ass_text):
        dialogue_texts = []
        for line in str(ass_text or '').splitlines():
            if line.startswith('Dialogue:'):
                dialogue_texts.append(line.split(',', 9)[-1])
        return dialogue_texts

    def test_landscape_ass_style_uses_clear_bottom_safe_area(self):
        style = TaskProcessor._build_streaming_ass_style(1920, 1080)

        self.assertEqual(style['PlayResX'], 1920)
        self.assertEqual(style['PlayResY'], 1080)
        # Online-style captions sit slightly lower and use more horizontal
        # width than the previous cinematic safe-area margins.
        self.assertGreaterEqual(style['MarginV'], 60.0)
        self.assertGreaterEqual(style['MarginL'], 40.0)
        self.assertGreaterEqual(style['MarginR'], 40.0)
        self.assertGreaterEqual(style['FontSize'], 52.0)
        self.assertGreaterEqual(style['Outline'], 2.2)
        self.assertEqual(style['Alignment'], 2)

    def test_landscape_layout_uses_wider_lines(self):
        max_line_length, max_lines = TaskProcessor._estimate_subtitle_layout_limits(1920, 1080)

        # 2026-09-16 恒定字号: 横屏最多两行平衡拆分,字号恒定不再逐条缩小。
        self.assertEqual(max_lines, 2)
        self.assertGreaterEqual(max_line_length, 22)

    def test_portrait_ass_style_keeps_higher_vertical_margin(self):
        style = TaskProcessor._build_streaming_ass_style(1080, 1920)

        self.assertEqual(style['PlayResX'], 1080)
        self.assertEqual(style['PlayResY'], 1920)
        self.assertGreaterEqual(style['MarginV'], 200.0)
        self.assertGreaterEqual(style['FontSize'], 66.0)
        self.assertGreaterEqual(style['Outline'], 2.0)

    def test_portrait_layout_uses_fewer_lines_and_stays_safe(self):
        max_line_length, max_lines = TaskProcessor._estimate_subtitle_layout_limits(1080, 1920)
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '竖屏字幕不应过宽或压得太低，否则会与互动区、底部贴纸发生冲突。',
            1080,
            1920,
            return_meta=True,
        )

        self.assertEqual(max_lines, 5)
        self.assertLessEqual(text.count(r'\N') + 1, 5)
        self.assertFalse(meta.get('overflow_warning'))

    def test_wrap_subtitle_text_for_ass_balances_long_cjk_text(self):
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '这是一个用于验证字幕换行能力的很长中文句子，需要保持底部居中显示并且不能溢出画面。',
            1920,
            1080,
            return_meta=True,
        )

        self.assertTrue(text)
        # 恒定字号: 放不下就折两行,绝不逐条缩字号。
        lines = self._extract_ass_lines(text)
        self.assertLessEqual(len(lines), 2)
        self.assertIsNone(meta.get('font_override'))
        self.assertNotIn(r'{\fs', text)
        self.assertFalse(meta.get('overflow_warning'))

    def test_long_landscape_wrap_avoids_breaking_common_phrases(self):
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '这是一条用于验证字幕换行能力的很长中文句子，需要保持底部居中显示并且不能溢出画面。',
            1920,
            1080,
            return_meta=True,
        )

        self.assertTrue(text)
        lines = self._extract_ass_lines(text)
        self.assertLessEqual(len(lines), 2)
        self.assertFalse('前\\N提' in text or '前 提' in text)
        self.assertFalse(meta.get('overflow_warning'))

    def test_mixed_language_wrap_keeps_latin_words_intact(self):
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '如果一句字幕里同时出现 RTX 5090、YouTube Shorts 和 AI workflow，这种中英混排也要保持节奏稳定。',
            1920,
            1080,
            return_meta=True,
        )

        normalized = text.replace(r'\N', '')
        lines = self._extract_ass_lines(text)
        self.assertIn('workflow', normalized)
        self.assertNotIn('w\\Norkflow', text)
        self.assertNotIn('You\\NTube', text)
        # 横屏最多两行,字号恒定。
        self.assertLessEqual(len(lines), 2)
        self.assertFalse(meta.get('overflow_warning'))

    def test_wrap_avoids_splitting_cjk_run_mid_char(self):
        """CJK text should prefer breaking at punctuation/script boundaries
        over splitting between two consecutive CJK characters."""
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '这是我在真实PlayStation硬件上运行的自制《天际》演示版，需要保持硬件这个词完整，不能拆得七零八落。',
            1920,
            1080,
            return_meta=True,
        )

        self.assertTrue(text)
        lines = text.split('\\N')
        for line in lines:
            cjk_count = sum(1 for c in line if TaskProcessor._is_cjk_like_char(c))
            self.assertGreater(
                cjk_count, 1,
                f'Line "{line}" contains only {cjk_count} CJK char(s) — likely a broken compound',
            )
        self.assertFalse(meta.get('overflow_warning'))

    def test_portrait_mixed_language_wrap_stays_balanced(self):
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '在 1080×1920 的竖屏里，Release notes、workflow status 这类英文短语也应该完整保留，避免断开后显得很廉价。',
            1080,
            1920,
            return_meta=True,
        )

        lines = self._extract_ass_lines(text)
        self.assertTrue(text)
        # 恒定字号: 平衡拆分目标 ≤5 行;边界 cue 退化为不限行数的宽度安全贪心
        # 换行(该文本 53 视觉单位/行容量 10,约需 7-8 行),不再溢出告警。
        self.assertGreaterEqual(len(lines), 4)
        self.assertLessEqual(len(lines), 8)
        normalized = text.replace(r'\N', '')
        self.assertIn('Release', normalized)
        self.assertIn('notes', normalized)
        self.assertIn('workflow', normalized)
        self.assertIn('status', normalized)
        self.assertFalse(any(line[:1] in '，。！？；：、)]}】）》」』' for line in lines if line))
        self.assertFalse(meta.get('overflow_warning'))

    def test_portrait_long_wrap_prefers_fewer_balanced_lines(self):
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '竖屏字幕要避开互动区和底部贴纸，长句往上收，避免视觉重心过低。',
            1080,
            1920,
            return_meta=True,
        )

        lines = self._extract_ass_lines(text)
        self.assertTrue(text)
        self.assertLessEqual(len(lines), 5)
        self.assertFalse(any(line[:1] in '，。！？；：、)]}】）》」』' for line in lines if line))
        self.assertFalse(meta.get('overflow_warning'))

    def test_single_line_priority_keeps_short_text_on_one_line(self):
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '短句应单行显示',
            1920,
            1080,
            return_meta=True,
        )

        self.assertTrue(text)
        self.assertNotIn(r'\N', text)
        self.assertFalse(meta.get('forced_wrap'))

    def test_long_cue_wraps_two_lines_at_constant_font(self):
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '这是一句中等长度的中文测试字幕，在横屏下默认字体一行放不下，应当折成两行且字号保持不变。',
            1920,
            1080,
            return_meta=True,
            prefer_single_line=True,
        )

        self.assertTrue(text)
        lines = self._extract_ass_lines(text)
        self.assertLessEqual(len(lines), 2)
        self.assertIsNone(meta.get('font_override'))
        self.assertNotIn(r'{\fs', text)
        self.assertFalse(meta.get('overflow_warning'))

    def test_wrap_result_same_with_or_without_single_line_priority(self):
        text_priority, meta_priority = TaskProcessor._wrap_subtitle_text_for_ass(
            '这是一句中等长度的中文测试字幕，在横屏下默认字体一行放不下，无论单行优先与否都恒定字号。',
            1920,
            1080,
            return_meta=True,
            prefer_single_line=True,
        )
        text_plain, meta_plain = TaskProcessor._wrap_subtitle_text_for_ass(
            '这是一句中等长度的中文测试字幕，在横屏下默认字体一行放不下，无论单行优先与否都恒定字号。',
            1920,
            1080,
            return_meta=True,
            prefer_single_line=False,
        )

        self.assertEqual(text_priority, text_plain)
        self.assertIsNone(meta_priority.get('font_override'))
        self.assertIsNone(meta_plain.get('font_override'))
        self.assertFalse(meta_priority.get('overflow_warning'))
        self.assertFalse(meta_plain.get('overflow_warning'))

    def test_ass_document_merges_existing_cue_line_breaks_for_landscape(self):
        ass_text = TaskProcessor._build_default_ass_document(
            [{'start': 0.0, 'end': 2.0, 'text': '第一行\n第二行'}],
            font_family='NotoSansCJKsc-Regular',
            video_width=1920,
            video_height=1080,
        )

        dialogue_texts = self._extract_dialogue_texts(ass_text)
        self.assertEqual(len(dialogue_texts), 1)
        self.assertNotIn(r'\N', dialogue_texts[0])
        self.assertIn('第一行第二行', dialogue_texts[0])

    def test_ass_document_wraps_long_landscape_cue_two_lines(self):
        ass_text = TaskProcessor._build_default_ass_document(
            [{
                'start': 0.0,
                'end': 3.0,
                'text': '这是一条用于验证横屏恒定字号烧录的超长字幕，放不下时应当折成两行而不是缩小字号。',
            }],
            font_family='NotoSansCJKsc-Regular',
            video_width=1920,
            video_height=1080,
        )

        dialogue_texts = self._extract_dialogue_texts(ass_text)
        self.assertEqual(len(dialogue_texts), 1)
        self.assertNotIn(r'{\fs', dialogue_texts[0])
        self.assertLessEqual(dialogue_texts[0].count(r'\N'), 1)


if __name__ == '__main__':
    unittest.main()
