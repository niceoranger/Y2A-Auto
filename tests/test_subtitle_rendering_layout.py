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
        # 2026-09-25 竖屏字号按画宽 5.8% 锚定:1080 宽 → ~62.6
        self.assertAlmostEqual(style['FontSize'], 1080 * 0.058, delta=1.0)
        self.assertGreaterEqual(style['Outline'], 2.0)

    def test_font_size_scales_proportionally_across_resolutions(self):
        """字号随画幅等比:历史 1080p 不变,低清/竖屏不再被下限托大。"""
        cases = {
            # (width, height): (字号, 占画高比例允许区间)
            (1920, 1080): (70.0, (0.060, 0.070)),
            (2560, 1440): (93.0, (0.060, 0.070)),
            (640, 360): (23.0, (0.060, 0.070)),
            (1280, 720): (47.0, (0.060, 0.070)),
        }
        for (w, h), (expected_font, ratio_range) in cases.items():
            style = TaskProcessor._build_streaming_ass_style(w, h)
            self.assertAlmostEqual(
                style['FontSize'], expected_font, delta=1.0,
                msg=f"{w}x{h} 横屏字号应保持屏高 6.5%",
            )
            ratio = style['FontSize'] / h
            self.assertTrue(
                ratio_range[0] <= ratio <= ratio_range[1],
                msg=f"{w}x{h} 字号占画高比例 {ratio:.3f} 失衡",
            )

    def test_portrait_font_size_anchors_to_width(self):
        """竖屏按画宽 5.8% 锚定:单字占宽 ~5.8%,不再随高度放大。"""
        for w, h in ((720, 1280), (1080, 1920), (360, 640)):
            style = TaskProcessor._build_streaming_ass_style(w, h)
            ratio = style['FontSize'] / w
            self.assertAlmostEqual(ratio, 0.058, delta=0.004, msg=f"{w}x{h}")
            # 底距同样随画高等比,低清竖屏不再被下限顶高
            self.assertLessEqual(style['MarginV'] / h, 0.14)

    def test_portrait_layout_uses_fewer_lines_and_stays_safe(self):
        max_line_length, max_lines = TaskProcessor._estimate_subtitle_layout_limits(1080, 1920)
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '竖屏字幕不应过宽或压得太低，否则会与互动区、底部贴纸发生冲突。',
            1080,
            1920,
            return_meta=True,
        )

        # 2026-09-29 用户需求: 任何朝向最多 2 行。
        self.assertEqual(max_lines, 2)
        self.assertLessEqual(text.count(r'\N') + 1, 2)
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
        # 2026-09-29 两行硬上限: 该文本 53 视觉单位超过竖屏两行容量(约 37),
        # 保持两行并置 overflow_warning,由 ASS 文档层按时间轴拆分。
        self.assertLessEqual(len(lines), 2)
        self.assertTrue(meta.get('overflow_warning'))
        normalized = text.replace(r'\N', '')
        self.assertIn('Release', normalized)
        self.assertIn('notes', normalized)
        self.assertIn('workflow', normalized)
        self.assertIn('status', normalized)
        self.assertFalse(any(line[:1] in '，。！？；：、)]}】）》」』' for line in lines if line))

    def test_portrait_long_wrap_prefers_fewer_balanced_lines(self):
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '竖屏字幕要避开互动区和底部贴纸，长句往上收，避免视觉重心过低。',
            1080,
            1920,
            return_meta=True,
        )

        lines = self._extract_ass_lines(text)
        self.assertTrue(text)
        self.assertLessEqual(len(lines), 2)
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

    def test_wrap_hard_caps_two_lines_on_corpus_regressions(self):
        """2026-09-29 用户需求: 任何朝向最多 2 行。

        回归样本取自当日线上视频的真实 cue(此前被折成 3-4 行同屏)。
        """
        corpus_cases = [
            # ce2209bb 0:41:24 竖版渲染为四行的横屏 cue
            ('指数将为你带来多元化，但多元化正面临越来越大的挑战，而且我们需要认真思考。我们都在谈论被动和主动策略应该存在于组合中',
             1920, 1080),
            # bda17a77 0:39:27 渲染为三行的横屏 cue
            ('现在加入我们的有更多报道的是内布拉斯加州共和党众议员迈克·弗拉德。他担任众议院金融服务委员会成员，并主持主街caucus。众议员，欢迎您再次来到彭博社',
             1920, 1080),
            # be4edbd1 竖屏四行 cue
            ('设备，其大小与 Vision Pro 的电池组相当。结果是一副感觉更轻的眼镜，',
             1080, 1920),
            # d9033a45 竖屏四行 cue
            ('这份名单还广泛流传给了竞争对手银行，他们借此针对名单上的人，伺机挖走客户、争夺交易',
             1080, 1920),
            # d22da8f6 横屏四行 cue
            ('美国和伊朗在关于重新开放霍尔木兹海峡的谈判中似乎又回到了原点，这场长达七个月的冲突即将结束。特朗普总统在周六证实，',
             1920, 1080),
        ]
        for text, vw, vh in corpus_cases:
            with self.subTest(video=f'{vw}x{vh}', text=text[:12]):
                wrapped, _ = TaskProcessor._wrap_subtitle_text_for_ass(
                    text, vw, vh, return_meta=True,
                )
                lines = self._extract_ass_lines(wrapped)
                self.assertLessEqual(len(lines), 2, f'{len(lines)} 行超上限: {wrapped}')

    def test_medium_cue_stays_single_line_after_width_recalibration(self):
        """宽度标定(0.72×字号/单位)后,横屏 34 单位内的 cue 应保持单行。

        旧估算按 1.0×字号计宽,把实际放得下的 26-34 单位 cue 提前折成两行
        (用户报告的"一行简短的字幕进行了换行")。
        """
        for text in (
            '行业先驱兼研究员李飞飞，所以那里有点值得关注的地方',
            '这是今天最重要的新闻',
            '一句话里有三十来个汉字在横屏其实完全放得下不应该被折行',
        ):
            with self.subTest(text=text):
                wrapped, meta = TaskProcessor._wrap_subtitle_text_for_ass(
                    text, 1920, 1080, return_meta=True,
                )
                self.assertNotIn(r'\N', wrapped)
                self.assertFalse(meta.get('forced_wrap'))
                self.assertFalse(meta.get('overflow_warning'))

    def test_ass_document_disables_libass_rewrap(self):
        """WrapStyle=2: 渲染器不得把已定稿的两行再折出第三行。"""
        ass_text = TaskProcessor._build_default_ass_document(
            [{'start': 0.0, 'end': 2.0, 'text': '短句'}],
            font_family='NotoSansCJKsc-Regular',
            video_width=1920,
            video_height=1080,
        )

        self.assertIn('WrapStyle: 2', ass_text)

    def test_ass_document_splits_overflowing_cue_in_time(self):
        """两行仍放不下的超长 cue:按时间轴拆成两条顺序 cue,恒定字号不缩放。"""
        long_text = (
            '这份名单还广泛流传给了竞争对手银行，他们借此针对名单上的人，'
            '伺机挖走客户、争夺交易,这句话很长以至于竖屏两行也放不下'
        )
        ass_text = TaskProcessor._build_default_ass_document(
            [{'start': 100.0, 'end': 104.5, 'text': long_text}],
            font_family='NotoSansCJKsc-Regular',
            video_width=1080,
            video_height=1920,
        )

        dialogue_lines = [
            line for line in ass_text.splitlines() if line.startswith('Dialogue:')
        ]
        self.assertEqual(len(dialogue_lines), 2)
        first_fields = dialogue_lines[0].split(',', 9)
        second_fields = dialogue_lines[1].split(',', 9)
        # 顺序时间轴:第一段的结束即第二段的开始。
        self.assertEqual(first_fields[2], second_fields[1])
        self.assertLess(first_fields[1], first_fields[2])
        self.assertLess(second_fields[1], second_fields[2])
        # 每条 cue 单行,无字号覆盖。
        for fields in (first_fields, second_fields):
            self.assertNotIn(r'\N', fields[9])
            self.assertNotIn(r'{\fs', fields[9])
        self.assertEqual(
            long_text.replace(',', '').replace('，', ''),
            (first_fields[9] + second_fields[9]).replace(',', '').replace('，', ''),
        )

    def test_wrap_aligns_two_lines_to_sentence_boundaries(self):
        """2026-09-30 阅读习惯:两行拆分优先对齐句子边界。

        一行里不得同时出现上一句的尾巴和下一句的开头。
        """
        text, meta = TaskProcessor._wrap_subtitle_text_for_ass(
            '我们在全球有250万家企业。企业板块环比增长了100%，这是一个非常惊人的数字。',
            1920,
            1080,
            return_meta=True,
        )

        lines = self._extract_ass_lines(text)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith('。'), f'首行应以句号收尾: {lines[0]}')
        self.assertTrue(lines[1].endswith('。'), f'次行应以句号收尾: {lines[1]}')

    def test_portrait_wrap_aligns_to_sentence_boundaries(self):
        text, _ = TaskProcessor._wrap_subtitle_text_for_ass(
            '设备，其大小与 Vision Pro 的电池组相当。结果是一副感觉更轻的眼镜，',
            1080,
            1920,
            return_meta=True,
        )

        lines = self._extract_ass_lines(text)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith('。'), f'首行应以句号收尾: {lines[0]}')

    def test_multi_sentence_cue_displays_sequentially_in_time(self):
        """句子组超过两行时按时间轴逐句分时,每条 cue 都是完整句子。"""
        long_text = (
            '现在加入我们的有更多报道的是内布拉斯加州共和党众议员迈克·弗拉德。'
            '他担任众议院金融服务委员会成员，并主持主街caucus。'
            '众议员，欢迎您再次来到彭博社'
        )
        ass_text = TaskProcessor._build_default_ass_document(
            [{'start': 10.0, 'end': 15.66, 'text': long_text}],
            font_family='NotoSansCJKsc-Regular',
            video_width=1920,
            video_height=1080,
        )

        dialogue_lines = [
            line for line in ass_text.splitlines() if line.startswith('Dialogue:')
        ]
        self.assertEqual(len(dialogue_lines), 3)
        fields = [line.split(',', 9) for line in dialogue_lines]
        # 时间轴连续且单调。
        for prev_fields, curr_fields in zip(fields, fields[1:]):
            self.assertEqual(prev_fields[2], curr_fields[1])
            self.assertLess(prev_fields[1], prev_fields[2])
        # 每条 cue 单行且在句末收尾(末条允许无标点结尾)。
        for idx, cue_fields in enumerate(fields):
            self.assertNotIn(r'\N', cue_fields[9])
            if idx < len(fields) - 1:
                self.assertTrue(cue_fields[9].endswith('。'), f'第{idx + 1}条应在句末收尾: {cue_fields[9]}')
        # 内容守恒(忽略标点空白差异)。
        joined = ''.join(cue_fields[9] for cue_fields in fields)
        self.assertEqual(joined.replace('·', ''), long_text.replace('·', ''))
        self.assertNotIn(r'{\fs', joined)

    def test_sentence_split_fallback_keeps_two_line_cap_when_duration_short(self):
        """时长不足以逐句分时(每段至少 0.8s)时,兜底展示仍不超过两行。"""
        long_text = (
            '第一句先说个结论。第二句展开讲讲原因和背景。第三句给一个具体的例子。'
            '第四句总结收尾。第五句展望未来。'
        )
        ass_text = TaskProcessor._build_default_ass_document(
            [{'start': 0.0, 'end': 1.9, 'text': long_text}],
            font_family='NotoSansCJKsc-Regular',
            video_width=1920,
            video_height=1080,
        )

        dialogue_lines = [
            line for line in ass_text.splitlines() if line.startswith('Dialogue:')
        ]
        self.assertEqual(len(dialogue_lines), 1)
        self.assertLessEqual(dialogue_lines[0].split(',', 9)[9].count(r'\N'), 1)


if __name__ == '__main__':
    unittest.main()
