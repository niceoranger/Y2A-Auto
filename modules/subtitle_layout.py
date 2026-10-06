#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""字幕排版与 ASS 生成的纯函数集。

从 task_manager.TaskProcessor 抽出:无任务状态、不读 self.config,输入输出
均为纯文本/数值。TaskProcessor 保留同名静态方法作委托入口,内部调用方零改动;
新代码请直接使用本模块。
"""
import os
import logging
import unicodedata

logger = logging.getLogger(__name__)

_ASS_PLAY_RES_X = 1920
_ASS_PLAY_RES_Y = 1080
_ASS_STYLE_BASE = {
    'FontSize': 56.0,
    'Outline': 2.0,
    'Shadow': 1.0,
    'MarginV': 40.0,
    'MarginL': 96.0,
    'MarginR': 96.0,
    'Alignment': 2,
    # 2026-09-12 用户需求: 黄底黑字字幕(经典高可读样式)。BorderStyle=3:
    # OutlineColour 即底块颜色(不透明纯黄 &H0000FFFF), Outline 为块内边距。
    'BorderStyle': 3,
    'Outline': 6.0,
    'Shadow': 0.0,
    'Bold': 1,
    'PrimaryColour': '&H00000000',
    'SecondaryColour': '&H00000000',
    'OutlineColour': '&H0000FFFF',
    'BackColour': '&H00000000',
}
# 2026-09-25 字号/底距改为画幅等比:
#   横屏 = 屏高 6.5%(与旧锚点 720/1080/1440/2160 各档完全一致),
#   竖屏 = 画宽 5.8%(窄画幅按高度锚定会让单字占宽超 11%,视觉过大)。
#   取消旧 46/47px 下限——360p 低清源曾被下限托住,字幕视觉比例达 1080p 历史视频的 2 倍。
_ASS_FONT_HEIGHT_RATIO = 0.065
_ASS_LANDSCAPE_FONT_MIN = 18.0
_ASS_LANDSCAPE_FONT_MAX = 132.0
_ASS_LANDSCAPE_MARGIN_V_RATIO = 0.0574
_ASS_LANDSCAPE_MARGIN_V_MIN = 12.0
_ASS_LANDSCAPE_MARGIN_V_MAX = 156.0
_ASS_PORTRAIT_FONT_WIDTH_RATIO = 0.058
_ASS_PORTRAIT_FONT_MIN = 20.0
_ASS_PORTRAIT_FONT_MAX = 120.0
_ASS_PORTRAIT_MARGIN_V_RATIO = 0.122
_ASS_PORTRAIT_MARGIN_V_MIN = 16.0
_ASS_PORTRAIT_MARGIN_V_MAX = 320.0
# Landscape captions use generous side margins to avoid crowding the
# edges, while leaving enough width for single-line cues at base font.
_ASS_LANDSCAPE_SIDE_MARGIN_RATIO = 0.025
_ASS_LANDSCAPE_SIDE_MARGIN_MIN = 32.0
_ASS_LANDSCAPE_SIDE_MARGIN_MAX = 80.0
_ASS_PORTRAIT_SIDE_MARGIN_RATIO = 0.095
_ASS_PORTRAIT_SIDE_MARGIN_MIN = 82.0
_ASS_PORTRAIT_SIDE_MARGIN_MAX = 156.0
_ASS_LANDSCAPE_LAYOUT_DENSITY = 0.93
# 2026-09-29 宽度标定: 实测(项目 ffmpeg+libass, fonts/ 内置字体,换 Arial/
# PingFang/思源结果一致)ASS FontSize=N 时每个全宽字形实际步进 ≈ 0.70N 而非
# 1.0N。旧估算按 1.0N 计宽,保守 1.43 倍,导致短 cue 被提前折行、两行放不下
# 的 cue 触发多行救援(视频出现 3-4 行字幕)。取 0.72 略高于实测下限。
_ASS_TEXT_WIDTH_RATIO = 0.72
# 2026-09-30 用户需求(阅读习惯): 换行/分时拆分优先对齐句子边界,避免一行里
# 同时出现上一句的尾巴和下一句的开头。
_ASS_SENTENCE_END_CHARS = '。！？…!?'
_ASS_SENTENCE_TAIL_CHARS = '”’』」）)]'
# 句子组打包上限相对安全行宽的宽容度: 组内句子合计允许略超安全宽度
# (WrapStyle=2 兜底不折行),换取行界严格对齐句子。
_ASS_SENTENCE_GROUP_TOLERANCE = 1.08
# 单行优先的"仍算单行"阈值密度。宽度安全检查(_check_ass_lines_width_safety)
# 仍是最终裁决:超过安全宽度的 cue 会折成两行(恒定字号,不再逐条缩小)。
_ASS_LANDSCAPE_SINGLE_LINE_DENSITY = 1.04
_ASS_LANDSCAPE_SINGLE_LINE_LIMIT_MIN = 28.0
_ASS_LANDSCAPE_SINGLE_LINE_LIMIT_MAX = 38.0
_ASS_PORTRAIT_LAYOUT_DENSITY = 0.92
# Allow text to use almost the full usable width. A small safety margin
# remains so descenders/outlines do not touch the screen edges.
_ASS_SAFE_WIDTH_RATIO = 0.98
_ASS_HARD_WRAP_MIN_LINE_LENGTH = 8
# Thicker outline for the BorderStyle=4 rounded box, plus a soft shadow.
_ASS_OUTLINE_RATIO = 0.075
_ASS_OUTLINE_MIN = 2.2
_ASS_OUTLINE_MAX = 5.5
_ASS_SHADOW_RATIO = 0.025
_ASS_SHADOW_MIN = 1.0
_ASS_SHADOW_MAX = 2.2
_STREAMING_SRT_TEMPLATE_HEIGHTS = (720, 1080, 1440, 2160)
_STREAMING_SRT_STYLE_TEMPLATES = {
    720: {
        'FontSize': 18.0,
        'Outline': 0.4,
        'Shadow': 0.6,
        'MarginL': 42,
        'MarginR': 42,
        'MarginV': 16,
    },
    1080: {
        'FontSize': 18.0,
        'Outline': 0.4,
        'Shadow': 0.6,
        'MarginL': 42,
        'MarginR': 42,
        'MarginV': 16,
    },
    1440: {
        'FontSize': 18.0,
        'Outline': 0.4,
        'Shadow': 0.6,
        'MarginL': 42,
        'MarginR': 42,
        'MarginV': 16,
    },
    2160: {
        'FontSize': 18.0,
        'Outline': 0.4,
        'Shadow': 0.6,
        'MarginL': 42,
        'MarginR': 42,
        'MarginV': 16,
    },
}
_STREAMING_SRT_PORTRAIT_FONT_SIZE_ANCHORS = (
    (1280.0, 12.5),
    (1920.0, 12.5),
    (2560.0, 13.5),
)
_STREAMING_SRT_PORTRAIT_MARGIN_V_ANCHORS = (
    (1280.0, 18.0),
    (1920.0, 28.0),
    (2560.0, 40.0),
)
_STREAMING_SRT_PORTRAIT_SIDE_MARGIN_RATIO = 0.08
_STREAMING_SRT_LANDSCAPE_LAYOUT_DENSITY = 0.72
_STREAMING_SRT_PORTRAIT_LAYOUT_DENSITY = 0.96


def _clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))

def _interpolate_anchor_value(position, anchors):
    normalized_anchors = []
    for anchor_position, anchor_value in anchors or ():
        try:
            normalized_anchors.append((float(anchor_position), float(anchor_value)))
        except Exception:
            continue

    if not normalized_anchors:
        return 0.0

    normalized_anchors.sort(key=lambda item: item[0])
    if position <= normalized_anchors[0][0]:
        return normalized_anchors[0][1]

    for idx in range(1, len(normalized_anchors)):
        left_position, left_value = normalized_anchors[idx - 1]
        right_position, right_value = normalized_anchors[idx]
        if position <= right_position:
            if right_position <= left_position:
                return right_value
            ratio = (position - left_position) / (right_position - left_position)
            return left_value + (right_value - left_value) * ratio

    return normalized_anchors[-1][1]

def _resolve_ass_dimensions(video_width, video_height):
    try:
        width = int(video_width)
    except Exception:
        width = 0
    try:
        height = int(video_height)
    except Exception:
        height = 0

    if width <= 0:
        width = _ASS_PLAY_RES_X
    if height <= 0:
        height = _ASS_PLAY_RES_Y
    return width, height

def _format_ass_number(value):
    try:
        value_f = float(value)
    except Exception:
        return str(value)
    if value_f.is_integer():
        return str(int(value_f))
    return f"{value_f:.2f}".rstrip('0').rstrip('.')

def _seconds_to_ass_timestamp(seconds):
    try:
        total_cs = max(0, int(round(float(seconds) * 100)))
    except Exception:
        total_cs = 0
    hours = total_cs // 360000
    total_cs %= 360000
    minutes = total_cs // 6000
    total_cs %= 6000
    secs = total_cs // 100
    centis = total_cs % 100
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"

def _escape_ass_text_line(text):
    return str(text or '').replace('\\', r'\\').replace('{', r'\{').replace('}', r'\}')

def _escape_ass_text(text):
    normalized = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
    escaped_lines = []
    for line in normalized.split('\n'):
        escaped_lines.append(TaskProcessor._escape_ass_text_line(line))
    return r'\N'.join(escaped_lines)

def _compose_ass_dialogue_text(lines):
    # 2026-09-16 恒定字号: 不再支持逐条 \fs 覆盖,Dialogue 文本一律使用样式基准字号。
    escaped_lines = [
        _escape_ass_text_line(line)
        for line in (lines or [])
        if str(line or '').strip()
    ]
    if not escaped_lines:
        return ''
    return r'\N'.join(escaped_lines)

def _build_streaming_ass_style(video_width, video_height):
    width, height = _resolve_ass_dimensions(video_width, video_height)

    style = dict(_ASS_STYLE_BASE)
    style.update({
        'PlayResX': width,
        'PlayResY': height,
    })

    is_portrait = height > width
    if is_portrait:
        font_size = _clamp(
            width * _ASS_PORTRAIT_FONT_WIDTH_RATIO,
            _ASS_PORTRAIT_FONT_MIN,
            _ASS_PORTRAIT_FONT_MAX,
        )
        margin_v = _clamp(
            height * _ASS_PORTRAIT_MARGIN_V_RATIO,
            _ASS_PORTRAIT_MARGIN_V_MIN,
            _ASS_PORTRAIT_MARGIN_V_MAX,
        )
        side_margin = _clamp(
            width * _ASS_PORTRAIT_SIDE_MARGIN_RATIO,
            _ASS_PORTRAIT_SIDE_MARGIN_MIN,
            _ASS_PORTRAIT_SIDE_MARGIN_MAX,
        )
    else:
        font_size = _clamp(
            height * _ASS_FONT_HEIGHT_RATIO,
            _ASS_LANDSCAPE_FONT_MIN,
            _ASS_LANDSCAPE_FONT_MAX,
        )
        margin_v = _clamp(
            height * _ASS_LANDSCAPE_MARGIN_V_RATIO,
            _ASS_LANDSCAPE_MARGIN_V_MIN,
            _ASS_LANDSCAPE_MARGIN_V_MAX,
        )
        side_margin = _clamp(
            width * _ASS_LANDSCAPE_SIDE_MARGIN_RATIO,
            _ASS_LANDSCAPE_SIDE_MARGIN_MIN,
            _ASS_LANDSCAPE_SIDE_MARGIN_MAX,
        )

    style.update({
        'FontSize': font_size,
        'Outline': _clamp(
            font_size * _ASS_OUTLINE_RATIO,
            _ASS_OUTLINE_MIN,
            _ASS_OUTLINE_MAX,
        ),
        'Shadow': _clamp(
            font_size * _ASS_SHADOW_RATIO,
            _ASS_SHADOW_MIN,
            _ASS_SHADOW_MAX,
        ),
        'MarginV': margin_v,
        'MarginL': side_margin,
        'MarginR': side_margin,
    })
    return style

def _sanitize_ass_font_name(font_family):
    return str(font_family or 'Arial').replace('\r', ' ').replace('\n', ' ').strip() or 'Arial'

def _resolve_streaming_srt_template_height(video_width, video_height):
    _, height = _resolve_ass_dimensions(video_width, video_height)
    for template_height in _STREAMING_SRT_TEMPLATE_HEIGHTS:
        if height <= template_height:
            return template_height
    return _STREAMING_SRT_TEMPLATE_HEIGHTS[-1]

def _build_streaming_srt_style_description(font_family, video_width, video_height):
    width, height = _resolve_ass_dimensions(video_width, video_height)
    template_height = _resolve_streaming_srt_template_height(width, height)
    template = dict(_STREAMING_SRT_STYLE_TEMPLATES[template_height])
    is_portrait = height > width
    if is_portrait:
        template.update({
            'FontSize': _clamp(
                _interpolate_anchor_value(height, _STREAMING_SRT_PORTRAIT_FONT_SIZE_ANCHORS),
                12.5,
                13.5,
            ),
            'MarginL': int(round(_clamp(width * _STREAMING_SRT_PORTRAIT_SIDE_MARGIN_RATIO, 40.0, 88.0))),
            'MarginR': int(round(_clamp(width * _STREAMING_SRT_PORTRAIT_SIDE_MARGIN_RATIO, 40.0, 88.0))),
            'MarginV': int(round(_clamp(
                _interpolate_anchor_value(height, _STREAMING_SRT_PORTRAIT_MARGIN_V_ANCHORS),
                16.0,
                44.0,
            ))),
        })
    template.update({
        'FontName': _sanitize_ass_font_name(font_family),
        'Alignment': 2,
        # BorderStyle=3: 实底色块字幕（黄底黑字，与主烧录模板一致）
        'BorderStyle': 3,
        'Outline': 2.0,
        'Shadow': 0.0,
        'PrimaryColour': '&H00000000',
        'OutlineColour': '&H0000FFFF',
        'OriginalSize': f"{width}x{height}",
        'TemplateHeight': template_height,
    })
    return template

def _build_streaming_srt_force_style(font_family, video_width, video_height):
    style = _build_streaming_srt_style_description(font_family, video_width, video_height)
    entries = [
        f"FontName={style['FontName']}",
        f"FontSize={_format_ass_number(style['FontSize'])}",
        f"Outline={_format_ass_number(style['Outline'])}",
        f"Shadow={_format_ass_number(style['Shadow'])}",
        f"MarginL={int(style['MarginL'])}",
        f"MarginR={int(style['MarginR'])}",
        f"MarginV={int(style['MarginV'])}",
        f"Alignment={style['Alignment']}",
        f"BorderStyle={style['BorderStyle']}",
        f"PrimaryColour={style['PrimaryColour']}",
        f"OutlineColour={style['OutlineColour']}",
    ]
    payload = ','.join(entries).replace("'", r"\'")
    return f"force_style='{payload}'"

def _build_streaming_srt_filter(render_subtitle_name, font_family, video_width, video_height):
    style = _build_streaming_srt_style_description(font_family, video_width, video_height)
    filter_segments = [
        f"subtitles={render_subtitle_name}",
        f"original_size={style['OriginalSize']}",
        "wrap_unicode=1",
        "fontsdir=fonts",
        "charenc=UTF-8",
        _build_streaming_srt_force_style(font_family, video_width, video_height),
    ]
    return ':'.join(filter_segments)

def _estimate_streaming_srt_layout_limits(video_width, video_height):
    style = _build_streaming_srt_style_description('Arial', video_width, video_height)
    width, height = _resolve_ass_dimensions(video_width, video_height)
    is_portrait = height > width
    usable_width = max(
        120.0,
        float(width) - float(style['MarginL']) - float(style['MarginR']),
    )
    font_size = max(1.0, float(style['FontSize']))
    density = (
        _STREAMING_SRT_PORTRAIT_LAYOUT_DENSITY
        if is_portrait
        else _STREAMING_SRT_LANDSCAPE_LAYOUT_DENSITY
    )
    max_line_length = int(round(usable_width / font_size * density))
    if is_portrait:
        max_line_length = int(_clamp(max_line_length, 12.0, 18.0))
        # 2026-09-29 用户需求: 任何朝向最多 2 行。
        max_lines = 2
    else:
        # Hard-coded single-line target for streaming SRT output.
        max_line_length = int(_clamp(max_line_length, 20.0, 26.0))
        max_lines = 1
    return max_line_length, max_lines

def _create_streaming_srt_engine(video_width=None, video_height=None):
    from .srt_transform_engine import SrtTransformConfig, SrtTransformEngine
    max_line_length, max_lines = _estimate_streaming_srt_layout_limits(
        video_width,
        video_height,
    )

    return SrtTransformEngine(
        SrtTransformConfig(
            max_line_length=max_line_length,
            max_lines=max_lines,
            split_long_cues=False,
            preserve_line_breaks=False,
            normalize_punctuation=False,
            filter_filler_words=False,
        ),
        logger=logger,
    )

def _wrap_streaming_srt_text(text, video_width, video_height):
    normalized = _merge_subtitle_text_parts(
        str(text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')
    )
    if not normalized:
        return ''

    width, height = _resolve_ass_dimensions(video_width, video_height)
    is_portrait = height > width
    style = _build_streaming_srt_style_description('Arial', width, height)
    max_line_length, max_lines = _estimate_streaming_srt_layout_limits(width, height)
    usable_width = max(
        120.0,
        float(width) - float(style['MarginL']) - float(style['MarginR']),
    )
    font_size = max(1.0, float(style['FontSize']))
    outline = float(style['Outline'])
    shadow = float(style['Shadow'])
    single_line_limit = int(_clamp(
        round(usable_width / font_size * _ASS_LANDSCAPE_SINGLE_LINE_DENSITY),
        18.0,
        28.0,
    ))
    # 2026-09-30 阅读习惯: 拆行优先对齐句子边界(竖屏两行场景)。
    sentence_units_limit = _estimate_safe_line_units(
        usable_width,
        font_size,
        outline,
        shadow,
    ) * _ASS_SENTENCE_GROUP_TOLERANCE
    sentence_lines = _split_sentences_into_groups(normalized, sentence_units_limit)
    sentence_aligned = bool(sentence_lines) and (
        len(sentence_lines) == 1 or (max_lines >= 2 and len(sentence_lines) == 2)
    )
    if sentence_aligned:
        wrapped_lines = sentence_lines
    else:
        wrapped_lines = _build_wrapped_lines_for_ass(
            normalized,
            is_portrait=is_portrait,
            max_line_length=max_line_length,
            max_lines=max_lines,
            single_line_limit=single_line_limit,
            aggressive=False,
        )
    fits, _, _, _ = _check_ass_lines_width_safety(
        wrapped_lines,
        usable_width,
        font_size,
        outline,
        shadow,
    )
    # 句子对齐结果不做硬换行回退(那会重新引入跨句混行)。
    if not fits and not sentence_aligned:
        hard_wrap_lines, _ = _find_safe_hard_wrap_lines(
            normalized,
            max_line_length=max_line_length,
            max_lines=max_lines,
            usable_width=usable_width,
            font_size=font_size,
            outline=outline,
            shadow=shadow,
        )
        if hard_wrap_lines:
            wrapped_lines = hard_wrap_lines
    return '\n'.join(line for line in wrapped_lines if line).strip() or normalized

def _prepare_streaming_srt_cues(subtitle_text, video_width=None, video_height=None):
    engine = _create_streaming_srt_engine(video_width, video_height)
    cues = engine.parse_srt(subtitle_text or '')
    if not cues:
        return []
    total_duration = max(float(cue.get('end', 0.0) or 0.0) for cue in cues)
    # Rendering should preserve validated subtitle content. Hallucination cleanup
    # is part of the ASR generation pipeline and is too destructive here,
    # especially for dense translated Chinese cues that are still legitimate.
    cues = engine.resolve_overlaps(cues, total_duration)
    cues = engine.apply_text_processing(cues)
    cues = engine.finalize_cues(cues, total_duration)
    for cue in cues:
        cue['text'] = _wrap_streaming_srt_text(
            cue.get('text', ''),
            video_width,
            video_height,
        )
    return cues

def _build_subtitle_style_description(font_family, video_width, video_height):
    style = _build_streaming_ass_style(video_width, video_height)
    font_name = _sanitize_ass_font_name(font_family)
    force_style = {
        'FontName': font_name,
        'FontSize': _format_ass_number(style['FontSize']),
        'Outline': _format_ass_number(style['Outline']),
        'Shadow': _format_ass_number(style['Shadow']),
        'MarginL': str(int(round(style['MarginL']))),
        'MarginR': str(int(round(style['MarginR']))),
        'MarginV': str(int(round(style['MarginV']))),
        'Alignment': str(style['Alignment']),
    }
    return style, force_style

def _build_subtitle_force_style(font_family, video_width, video_height):
    _, force_style = _build_subtitle_style_description(
        font_family,
        video_width,
        video_height,
    )
    entries = [f"{key}={value}" for key, value in force_style.items()]
    payload = ','.join(entries).replace("'", r"\'")
    return f"force_style='{payload}'"

def _estimate_subtitle_layout_limits(video_width, video_height):
    style = _build_streaming_ass_style(video_width, video_height)
    is_portrait = float(style['PlayResY']) > float(style['PlayResX'])
    usable_width = max(
        120.0,
        float(style['PlayResX']) - float(style['MarginL']) - float(style['MarginR']),
    )
    font_size = max(1.0, float(style['FontSize']))
    density = _ASS_PORTRAIT_LAYOUT_DENSITY if is_portrait else _ASS_LANDSCAPE_LAYOUT_DENSITY
    max_line_length = int(round(usable_width / (font_size * _ASS_TEXT_WIDTH_RATIO) * density))
    if is_portrait:
        # 2026-09-29 用户需求: 任何朝向最多 2 行。竖屏行宽按真实字形步进
        # (_ASS_TEXT_WIDTH_RATIO)估算,1080 宽约 18 视觉单位/行。
        max_line_length = int(_clamp(max_line_length, 7.0, 19.0))
        max_lines = 2
    else:
        # 2026-09-16 恒定字号策略: 横屏允许最多两行平衡拆分,字号恒定不再
        # 逐条缩小(逐条缩字号曾导致同视频字幕忽大忽小)。
        # 2026-09-29 行宽上限同样按真实字形步进放宽(1080p 约 33 单位/行)。
        max_line_length = int(_clamp(max_line_length, 18.0, 34.0))
        max_lines = 2
    return max_line_length, max_lines

def _is_cjk_like_char(char):
    if not char:
        return False
    return unicodedata.east_asian_width(char) in {'W', 'F'}

def _merge_subtitle_text_parts(parts):
    merged = ''
    trailing_no_space = '([{\u3008\u300a\u300c\u300e\u3010'
    leading_no_space = '.,!?;:)]}，。！？；：、…】）》」』'

    for part in parts or []:
        piece = str(part or '').strip()
        if not piece:
            continue
        if not merged:
            merged = piece
            continue

        prev_char = merged[-1]
        curr_char = piece[0]
        if (
            prev_char.isspace()
            or curr_char.isspace()
            or prev_char in trailing_no_space
            or curr_char in leading_no_space
            or (_is_cjk_like_char(prev_char) and _is_cjk_like_char(curr_char))
        ):
            merged += piece
        else:
            merged += f" {piece}"

    return merged.strip()

def _limit_wrapped_lines(lines, max_lines):
    filtered_lines = [str(line or '').strip() for line in (lines or []) if str(line or '').strip()]
    if len(filtered_lines) <= max_lines:
        return filtered_lines

    if max_lines >= 4:
        visible_lines = filtered_lines[:max_lines - 1]
        remainder = _merge_subtitle_text_parts(filtered_lines[max_lines - 1:])
        if remainder:
            visible_lines.append(remainder)
        return [line for line in visible_lines if line]

    merged_text = _merge_subtitle_text_parts(filtered_lines)
    if not merged_text or max_lines <= 1:
        return filtered_lines[:max_lines]

    rebalanced_lines = []
    remaining_text = merged_text
    remaining_slots = int(max_lines)

    while remaining_slots > 1 and remaining_text:
        remaining_units = _estimate_subtitle_text_units(remaining_text)
        target_line_length = max(6.0, remaining_units / remaining_slots)
        split_index = _find_balanced_wrap_index(remaining_text, target_line_length)
        if split_index <= 0:
            fallback_lines = _wrap_subtitle_segment_greedily(remaining_text, target_line_length)
            if len(fallback_lines) <= 1:
                break
            current_line = str(fallback_lines[0] or '').strip()
            next_text = _merge_subtitle_text_parts(fallback_lines[1:])
        else:
            current_line = remaining_text[:split_index].strip()
            next_text = remaining_text[split_index:].strip()

        if not current_line or not next_text:
            break

        rebalanced_lines.append(current_line)
        remaining_text = next_text
        remaining_slots -= 1

    if remaining_text:
        rebalanced_lines.append(remaining_text.strip())
    return [line for line in rebalanced_lines[:max_lines] if line]

def _build_wrapped_lines_for_ass(
    normalized,
    *,
    is_portrait,
    max_line_length,
    max_lines,
    single_line_limit,
    aggressive=False,
):
    raw_segments = [segment.strip() for segment in str(normalized or '').split('\n') if segment.strip()]
    wrapped_lines = []

    for segment in raw_segments:
        if is_portrait or aggressive:
            max_lines = max(1, int(max_lines))
            candidate_lines = _build_optimal_multiline_partition(
                segment,
                max_line_length=max_line_length,
                min_lines=1,
                max_lines=max_lines,
            )
            if candidate_lines:
                wrapped_lines.extend(candidate_lines)
            else:
                wrapped_lines.extend(_wrap_subtitle_segment_greedily(segment, max_line_length))
        elif int(max_lines) <= 1:
            # Hard-coded single-line mode: keep the segment intact so the
            # caller can scale the font or emit an overflow warning instead
            # of wrapping.
            wrapped_lines.append(segment)
        else:
            wrapped_lines.extend(
                _wrap_landscape_segment_for_ass(
                    segment,
                    single_line_limit=single_line_limit,
                    max_line_length=max_line_length,
                )
            )

    return _limit_wrapped_lines(wrapped_lines, max_lines)

def _estimate_ass_line_render_width(line, font_size, outline, shadow):
    text_units = _estimate_subtitle_text_units(str(line or ''))
    if text_units <= 0:
        return 0.0
    # Padding accounts for the rounded box (BorderStyle=4) or outline,
    # the shadow offset and a small safety margin. The coefficients were
    # calibrated against Source Han Sans HW SC rendered at 1080p.
    padding = max(
        8.0,
        float(outline) * 2.8 + float(shadow) * 1.8 + float(font_size) * 0.14,
    )
    # 每视觉单位按 _ASS_TEXT_WIDTH_RATIO×FontSize 计宽(实测字形步进)。
    return text_units * float(font_size) * _ASS_TEXT_WIDTH_RATIO + padding

def _check_ass_lines_width_safety(lines, usable_width, font_size, outline, shadow):
    safe_width = max(1.0, float(usable_width) * _ASS_SAFE_WIDTH_RATIO)
    line_widths = [
        _estimate_ass_line_render_width(line, font_size, outline, shadow)
        for line in (lines or [])
        if str(line or '').strip()
    ]
    if not line_widths:
        return True, 0.0, safe_width, []
    max_width = max(line_widths)
    return max_width <= safe_width, max_width, safe_width, line_widths

def _estimate_safe_line_units(usable_width, font_size, outline, shadow):
    safe_width = max(1.0, float(usable_width) * _ASS_SAFE_WIDTH_RATIO)
    padding = max(6.0, float(outline) * 4.0 + float(shadow) * 2.0 + float(font_size) * 0.08)
    return max(
        float(_ASS_HARD_WRAP_MIN_LINE_LENGTH),
        (safe_width - padding) / max(1.0, float(font_size) * _ASS_TEXT_WIDTH_RATIO),
    )

def _sentence_end_positions(text):
    """返回句子结束边界位置(句末标点及其后紧跟的收尾引号/括号之后)。

    行尾的句末标点不构成边界(其后没有下一句)。
    """
    segment = str(text or '')
    ends = []
    for idx, char in enumerate(segment):
        if char not in _ASS_SENTENCE_END_CHARS:
            continue
        end = idx + 1
        while end < len(segment) and segment[end] in _ASS_SENTENCE_TAIL_CHARS:
            end += 1
        if 0 < end < len(segment):
            ends.append(end)
    return ends

def _sentence_slices(text):
    """按句子边界切片;无内嵌句末标点时返回 []。"""
    segment = str(text or '').strip()
    if not segment:
        return []
    ends = _sentence_end_positions(segment)
    if not ends:
        return []
    slices = []
    prev = 0
    for end in ends:
        slices.append(segment[prev:end])
        prev = end
    slices.append(segment[prev:])
    return slices

def _balanced_sentence_two_groups(normalized, max_units):
    """在句子边界上把文本切成尽量均衡的两组,每组 ≤ max_units。

    优先保证两行各自是完整句子的组合且宽度不超限;找不到可行边界返回 []。
    """
    slices = _sentence_slices(normalized)
    if len(slices) < 2:
        return []
    best_groups = []
    best_diff = None
    left_units = 0.0
    for idx in range(len(slices) - 1):
        left_units += _estimate_subtitle_text_units(slices[idx])
        left_text = ''.join(slices[:idx + 1]).strip()
        right_text = ''.join(slices[idx + 1:]).strip()
        right_units = _estimate_subtitle_text_units(right_text)
        if left_units > max_units or right_units > max_units:
            continue
        diff = abs(left_units - right_units)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_groups = [left_text, right_text]
    return [group for group in best_groups if group]

def _split_sentences_into_groups(normalized, max_units):
    """把整段文字按句子边界贪心打包成若干组,每组 ≤ max_units 视觉单位。

    绝不拆开单个句子;存在单句超过 max_units 时返回 [](调用方回退到
    通用平衡换行,该场景句子本身必须行内折断,无法对齐)。
    返回组数 ≥ 2 的列表;全部句子能装进一组时返回 [整段]。
    """
    slices = _sentence_slices(normalized)
    if not slices:
        return []

    groups = []
    current = ''
    current_units = 0.0
    for piece in slices:
        piece_units = _estimate_subtitle_text_units(piece)
        if piece_units > max_units:
            return []
        if current and current_units + piece_units > max_units:
            groups.append(current.strip())
            current = piece
            current_units = piece_units
        else:
            current += piece
            current_units += piece_units
    if current.strip():
        groups.append(current.strip())
    return [group for group in groups if group]

def _score_partition_line(line, target_units, max_line_length, *, is_last):
    stripped = str(line or '').strip()
    if not stripped:
        return float('inf')

    units = _estimate_subtitle_text_units(stripped)
    # Soft length constraint with a generous tolerance: hard-reject only
    # when a line is extremely long, otherwise apply a strong quadratic
    # penalty.  This keeps the DP out of the greedy fallback for portrait
    # cues where perfect boundary-aligned partitions would otherwise be
    # impossible within a tight budget.
    hard_limit = max(float(max_line_length) * 1.5, float(max_line_length) + 8.0)
    if units > hard_limit:
        return float('inf')

    score = (abs(units - float(target_units)) ** 2) * 1.3
    if units > float(max_line_length):
        score += ((units - float(max_line_length)) ** 2) * 36.0

    minimum_units = max(4.5, float(target_units) * (0.60 if is_last else 0.72))
    if units < minimum_units:
        score += (minimum_units - units) ** 2 * (8.0 if is_last else 16.0)

    if stripped[0] in '.,!?;:，。！？；：、)]}】）》」』':
        score += 25.0
    if stripped[-1] in '([{【（《“‘':
        score += 25.0
    if not is_last and _is_short_orphan_tail(stripped):
        score += 10.0
    return score

def _build_optimal_multiline_partition(
    normalized,
    *,
    max_line_length,
    min_lines,
    max_lines,
):
    from functools import lru_cache

    raw_segments = [segment.strip() for segment in str(normalized or '').split('\n') if segment.strip()]
    merged_text = _merge_subtitle_text_parts(raw_segments)
    if not merged_text:
        return []

    minimum_lines = max(1, int(min_lines))
    maximum_lines = max(minimum_lines, int(max_lines))

    preferred_points = _collect_candidate_wrap_indices(merged_text, include_fallback=False)
    all_points = _collect_candidate_wrap_indices(merged_text, include_fallback=True)

    total_units = _estimate_subtitle_text_units(merged_text)
    best_score = None
    best_lines = []

    for point_source in (preferred_points, all_points):
        candidate_points = point_source
        if not candidate_points:
            continue

        points = [0] + sorted(set(idx for idx in candidate_points if 0 < idx < len(merged_text))) + [len(merged_text)]
        if len(points) < minimum_lines + 1:
            continue

        if len(points) < 2:
            return [merged_text]

        for line_count in range(minimum_lines, maximum_lines + 1):
            target_units = max(6.0, total_units / max(1, line_count))

            @lru_cache(maxsize=None)
            def solve(start_idx, lines_left):
                remaining_text = merged_text[points[start_idx]:].strip()
                if not remaining_text:
                    return float('inf'), tuple()

                if lines_left == 1:
                    line_score = _score_partition_line(
                        remaining_text,
                        target_units,
                        max_line_length,
                        is_last=True,
                    )
                    if line_score == float('inf'):
                        return float('inf'), tuple()
                    return line_score, (remaining_text,)

                best_local = float('inf'), tuple()
                max_next_index = len(points) - lines_left
                for next_idx in range(start_idx + 1, max_next_index + 1):
                    line = merged_text[points[start_idx]:points[next_idx]].strip()
                    if not line:
                        continue

                    current_score = _score_partition_line(
                        line,
                        target_units,
                        max_line_length,
                        is_last=False,
                    )
                    if current_score == float('inf'):
                        continue

                    tail_score, tail_lines = solve(next_idx, lines_left - 1)
                    total_score = current_score + tail_score
                    if total_score < best_local[0]:
                        best_local = total_score, (line,) + tail_lines

                return best_local

            score, lines = solve(0, line_count)
            if not lines:
                continue

            score += max(0, line_count - minimum_lines) * 3.0
            if best_score is None or score < best_score:
                best_score = score
                best_lines = list(lines)

        if best_lines:
            break

    return best_lines

def _find_safe_hard_wrap_lines(
    normalized,
    *,
    max_line_length,
    max_lines,
    usable_width,
    font_size,
    outline,
    shadow,
):
    best_lines = []
    best_width = None

    for line_limit in range(
        int(max_line_length),
        int(_ASS_HARD_WRAP_MIN_LINE_LENGTH) - 1,
        -1,
    ):
        candidate_lines = _build_aggressive_two_line_candidate(
            normalized,
            max_line_length=line_limit,
            max_lines=max_lines,
        )
        fits, max_width, _, _ = _check_ass_lines_width_safety(
            candidate_lines,
            usable_width,
            font_size,
            outline,
            shadow,
        )
        if best_width is None or max_width < best_width:
            best_lines = candidate_lines
            best_width = max_width
        if fits:
            return candidate_lines, True

    return best_lines, False

def _build_aggressive_two_line_candidate(normalized, *, max_line_length, max_lines):
    raw_segments = [segment.strip() for segment in str(normalized or '').split('\n') if segment.strip()]
    merged_text = _merge_subtitle_text_parts(raw_segments)
    if not merged_text:
        return []
    if max_lines <= 1 or _estimate_subtitle_text_units(merged_text) <= max_line_length:
        return [merged_text]

    if max_lines > 2:
        return _limit_wrapped_lines(
            _wrap_subtitle_segment_greedily(merged_text, max_line_length),
            max_lines,
        )

    split_index = _find_balanced_wrap_index(merged_text, max_line_length)
    if split_index > 0:
        return _limit_wrapped_lines(
            [merged_text[:split_index].strip(), merged_text[split_index:].strip()],
            max_lines,
        )

    return _limit_wrapped_lines(
        _wrap_subtitle_segment_greedily(merged_text, max_line_length),
        max_lines,
    )

def _is_preferred_wrap_boundary(char):
    if not char:
        return False
    if char.isspace():
        return True
    return char in '.,!?;:，。！？；：、)]}】）》」』'

def _is_latin_word_char(char):
    if not char:
        return False
    return char.isascii() and (char.isalnum() or char in "'#&+_./-")

def _is_disallowed_wrap_pair(left_char, right_char):
    if not left_char or not right_char:
        return False
    if _is_latin_word_char(left_char) and _is_latin_word_char(right_char):
        return True
    if left_char in '([{【（《“‘' or right_char in ')]}】）》」』”’':
        return True
    if left_char.isdigit() and right_char.isascii() and right_char.isalpha():
        return True
    if _is_latin_word_char(left_char) and right_char == '%':
        return True
    return False

def _is_cjk_cjk_split(text, split_index):
    """Return True if *split_index* falls between two CJK-like characters.

    Splitting mid-CJK-run is generally undesirable because most CJK
    words are multi-character compounds.  The caller should penalise
    these positions so the wrapper prefers punctuation, spaces and
    script boundaries instead.
    """
    segment = str(text or '')
    if split_index <= 0 or split_index >= len(segment):
        return False
    return (
        _is_cjk_like_char(segment[split_index - 1])
        and _is_cjk_like_char(segment[split_index])
    )

def _should_keep_ascii_phrase_together(text, split_index):
    segment = str(text or '')
    if split_index <= 0 or split_index >= len(segment):
        return False
    if not segment[split_index - 1].isspace():
        return False

    left_end = split_index - 1
    left_start = left_end - 1
    while left_start >= 0 and _is_latin_word_char(segment[left_start]):
        left_start -= 1
    left_token = segment[left_start + 1:left_end].strip()

    right_end = split_index
    while right_end < len(segment) and _is_latin_word_char(segment[right_end]):
        right_end += 1
    right_token = segment[split_index:right_end].strip()

    if not left_token or not right_token:
        return False

    combined_length = len(left_token) + len(right_token) + 1
    if combined_length <= 15:
        return True
    if left_token.isupper() and len(left_token) <= 4:
        return True
    return False

def _collect_candidate_wrap_indices(segment, include_fallback=True):
    preferred = []
    fallback = []
    for idx in range(1, len(segment)):
        left_char = segment[idx - 1]
        right_char = segment[idx]
        if _is_disallowed_wrap_pair(left_char, right_char):
            continue
        if left_char.isspace() and _should_keep_ascii_phrase_together(segment, idx):
            continue
        right_text = segment[idx:].strip()
        if _is_preferred_wrap_boundary(left_char) or _semantic_wrap_bonus(right_text) > 0.0:
            preferred.append(idx)
        else:
            fallback.append(idx)
    if not include_fallback:
        return preferred
    return preferred + fallback

def _find_wrap_boundary(chars):
    joined_chars = ''.join(chars)
    for idx in range(len(chars) - 1, -1, -1):
        right_char = chars[idx + 1] if idx + 1 < len(chars) else ''
        if chars[idx].isspace() and _should_keep_ascii_phrase_together(joined_chars, idx + 1):
            continue
        if _is_preferred_wrap_boundary(chars[idx]) and not _is_disallowed_wrap_pair(chars[idx], right_char):
            return idx
    return -1

def _estimate_subtitle_char_units(char):
    if not char:
        return 0.0
    if char.isspace():
        return 0.35
    if unicodedata.east_asian_width(char) in {'W', 'F'}:
        if unicodedata.category(char).startswith('P'):
            return 0.7
        return 1.0
    if char.isascii():
        if char.isalnum():
            return 0.6
        return 0.45
    if unicodedata.category(char).startswith('P'):
        return 0.6
    return 0.8

def _estimate_subtitle_text_units(text):
    return sum(_estimate_subtitle_char_units(char) for char in str(text or ''))

def _is_punctuation_only_text(text):
    stripped = str(text or '').strip()
    if not stripped:
        return False
    return all(
        char.isspace() or unicodedata.category(char).startswith('P')
        for char in stripped
    )

def _is_short_orphan_tail(text):
    stripped = str(text or '').strip()
    if not stripped:
        return False
    if _is_punctuation_only_text(stripped):
        return True

    tail_core = stripped.rstrip('.,!?;:，。！？；：、… ')
    if not tail_core:
        return True

    tail_units = _estimate_subtitle_text_units(tail_core)
    if tail_units <= 3.0:
        return True

    return tail_core in {'吗', '呢', '啊', '吧', '呀', '了', '嘛', '么', '呗', '哇', '哦', '喔'}

def _semantic_wrap_bonus(text):
    stripped = str(text or '').strip()
    if not stripped:
        return 0.0

    strong_prefixes = (
        '而不是', '而非', '并不是', '不是', '但是', '不过', '然而', '因此', '所以',
        '因为', '如果', '虽然', '并且', '或者', '还是', '以及', '然后', '兼顾',
        '保持', '避免', '确保', '否则',
        'rather than', 'instead of', 'because', 'however', 'therefore', 'although',
    )
    weak_prefixes = (
        '而', '但', '却', '并', '或',
        'and', 'but', 'or', 'if', 'when', 'while', 'that', 'which',
    )

    lowered = stripped.lower()
    if lowered.startswith(strong_prefixes):
        return 18.0
    if lowered.startswith(weak_prefixes):
        return 8.0
    return 0.0

def _is_broken_compound_wrap(left_text, right_text):
    left = str(left_text or '').strip().lower()
    right = str(right_text or '').strip().lower()
    if not left or not right:
        return False

    broken_pairs = (
        ('而', '不是'),
        ('并', '不是'),
        ('rather', 'than'),
        ('instead', 'of'),
    )
    return any(left.endswith(prefix) and right.startswith(suffix) for prefix, suffix in broken_pairs)

# 行首禁则: 这些收尾标点不允许出现在行首(中文排版规范)。
_LINE_START_FORBIDDEN_CHARS = '.,!?;:)]}，。！？；：、…】）》」』”’'

def _enforce_line_start_punctuation_rules(wrapped_lines):
    """行首禁则后处理: 若某行以收尾标点开头,把上一行末字符移下来连带标点。

    只移动字符、不合并行,保证每行仍在贪心行宽上限内(上一行变短、下一行
    长度不变时例外为由标点开头的情况,移动后下一行首字符为普通字符)。
    """
    lines = [str(line) for line in (wrapped_lines or [])]
    for i in range(1, len(lines)):
        while (
            lines[i]
            and lines[i][:1] in _LINE_START_FORBIDDEN_CHARS
            and lines[i - 1]
        ):
            moved = lines[i - 1][-1:]
            stripped_prev = lines[i - 1][:-1].rstrip()
            if not stripped_prev:
                break
            lines[i - 1] = stripped_prev
            lines[i] = moved + lines[i]
    return [line for line in lines if line]

def _wrap_subtitle_segment_greedily(segment, max_line_length):
    wrapped_lines = []
    chars = []
    current_units = 0.0
    idx = 0

    while idx < len(segment):
        char = segment[idx]
        char_units = _estimate_subtitle_char_units(char)
        if chars and current_units + char_units > max_line_length:
            break_at = _find_wrap_boundary(chars)
            if break_at >= 0:
                line_chars = chars[:break_at + 1]
                remainder = ''.join(chars[break_at + 1:]).strip()
            else:
                tail_start = len(chars)
                while tail_start > 0 and _is_latin_word_char(chars[tail_start - 1]):
                    tail_start -= 1
                if 0 < tail_start < len(chars):
                    line_chars = chars[:tail_start]
                    remainder = ''.join(chars[tail_start:]).strip()
                elif tail_start == 0 and all(_is_latin_word_char(c) for c in chars):
                    # The accumulated buffer is one unbreakable Latin token that
                    # already exceeds the line length. Keep it intact so the
                    # downstream overflow guard can scale the font or wrap via
                    # rescue logic, rather than splitting the word mid-character.
                    line_chars = chars
                    remainder = ''
                else:
                    line_chars = chars
                    remainder = ''

            line = ''.join(line_chars).strip()
            if line:
                wrapped_lines.append(line)
            chars = list(remainder)
            current_units = _estimate_subtitle_text_units(remainder)
            continue

        chars.append(char)
        current_units += char_units
        idx += 1

    line = ''.join(chars).strip()
    if line:
        wrapped_lines.append(line)
    return _enforce_line_start_punctuation_rules(wrapped_lines)

def _find_balanced_wrap_index(segment, max_line_length):
    best_index = -1
    best_score = None

    candidate_indices = _collect_candidate_wrap_indices(segment)
    if not candidate_indices:
        return -1

    for idx in candidate_indices:
        left = segment[:idx].strip()
        right = segment[idx:].strip()
        if not left or not right:
            continue

        left_units = _estimate_subtitle_text_units(left)
        right_units = _estimate_subtitle_text_units(right)
        boundary_char = segment[idx - 1]
        overflow = max(0.0, left_units - max_line_length) + max(0.0, right_units - max_line_length)
        score = abs(left_units - right_units) + overflow * 8.0

        if not _is_preferred_wrap_boundary(boundary_char):
            score += 4.5
        elif boundary_char.isspace():
            score -= 1.5
        else:
            score -= 4.0
        if _is_short_orphan_tail(right):
            score += 15.0
        if left_units <= 3.0:
            score += 8.0
        # Penalise severely unbalanced splits (ratio > 3:1)
        shorter = min(left_units, right_units)
        longer = max(left_units, right_units)
        if shorter > 0 and longer / shorter > 3.0:
            score += 6.0
        score -= _semantic_wrap_bonus(right)
        if _is_broken_compound_wrap(left, right):
            score += 12.0
        if _is_cjk_cjk_split(segment, idx):
            score += 4.0

        if best_score is None or score < best_score:
            best_score = score
            best_index = idx

    return best_index

# Tolerance (in visual units) above single_line_limit where we still
# prefer keeping the text on one line rather than splitting.
_SINGLE_LINE_TOLERANCE = 3.5

def _wrap_landscape_segment_for_ass(segment, single_line_limit, max_line_length):
    total_units = _estimate_subtitle_text_units(segment)
    if total_units <= single_line_limit:
        return [segment]

    tolerance = _SINGLE_LINE_TOLERANCE

    split_index = _find_balanced_wrap_index(segment, max_line_length)
    if split_index <= 0:
        # No viable balanced split – keep single if within tolerance
        if total_units <= single_line_limit + tolerance:
            return [segment]
        return _wrap_subtitle_segment_greedily(segment, max_line_length)

    first_line = segment[:split_index].strip()
    second_line = segment[split_index:].strip()
    if not first_line or not second_line:
        if total_units <= single_line_limit + tolerance:
            return [segment]
        return _wrap_subtitle_segment_greedily(segment, max_line_length)

    # Orphan tail: second line is too short to justify a split
    if _is_short_orphan_tail(second_line) and total_units <= single_line_limit + tolerance:
        return [segment]

    first_units = _estimate_subtitle_text_units(first_line)
    second_units = _estimate_subtitle_text_units(second_line)

    # Severely unbalanced split (ratio > 3:1): prefer single line if within tolerance
    shorter = min(first_units, second_units)
    longer = max(first_units, second_units)
    if shorter > 0 and longer / shorter > 3.0 and total_units <= single_line_limit + tolerance:
        return [segment]

    if first_units > max_line_length * 1.35 or second_units > max_line_length * 1.35:
        fallback_lines = _wrap_subtitle_segment_greedily(segment, max_line_length)
        if len(fallback_lines) == 2 and not _is_short_orphan_tail(fallback_lines[1]):
            return fallback_lines
        if total_units <= single_line_limit + tolerance:
            return [segment]
        return fallback_lines

    return [first_line, second_line]

def _wrap_subtitle_text_for_ass(
    text,
    video_width,
    video_height,
    return_meta=False,
    *,
    prefer_single_line=True,
):
    # Normalize internal line breaks so that a single SRT cue is always
    # treated as one logical line. 2026-09-16 恒定字号: 单行优先仅指"放得下
    # 就单行";放不下时按布局上限(横屏两行)平衡拆分,绝不逐条缩小字号。
    normalized = _merge_subtitle_text_parts(
        str(text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')
    )
    wrap_meta = {
        'forced_wrap': False,
        'font_override': None,
        'overflow_warning': False,
    }
    if not normalized:
        return ('', wrap_meta) if return_meta else ''

    max_line_length, max_lines = _estimate_subtitle_layout_limits(video_width, video_height)
    style = _build_streaming_ass_style(video_width, video_height)
    is_portrait = float(style['PlayResY']) > float(style['PlayResX'])
    usable_width = max(
        120.0,
        float(style['PlayResX']) - float(style['MarginL']) - float(style['MarginR']),
    )
    font_size = max(1.0, float(style['FontSize']))
    outline = float(style['Outline'])
    shadow = float(style['Shadow'])
    single_line_limit = int(_clamp(
        round(usable_width / (font_size * _ASS_TEXT_WIDTH_RATIO) * _ASS_LANDSCAPE_SINGLE_LINE_DENSITY),
        _ASS_LANDSCAPE_SINGLE_LINE_LIMIT_MIN,
        _ASS_LANDSCAPE_SINGLE_LINE_LIMIT_MAX,
    ))

    # 2026-09-30 阅读习惯: 拆行优先对齐句子边界。两句各自放得下就各占一行;
    # 句子组超过两行(≥3 组)时把组列表交给 ASS 文档层按时间轴逐句分时显示,
    # 这里保留通用两行兜底(时长不足无法分时时)。单句超宽等对齐失败场景
    # 才回退通用平衡换行。
    sentence_units_limit = _estimate_safe_line_units(
        usable_width,
        font_size,
        outline,
        shadow,
    ) * _ASS_SENTENCE_GROUP_TOLERANCE

    # Single-line priority: keep the whole cue on one line if it fits at
    # the base font size; otherwise wrap at constant font size.
    single_kept = False
    if prefer_single_line:
        single_line = [normalized]
        single_fits, _, _, _ = _check_ass_lines_width_safety(
            single_line,
            usable_width,
            font_size,
            outline,
            shadow,
        )
        if single_fits:
            wrapped_lines = single_line
            single_kept = True

    if not single_kept:
        sentence_two = _balanced_sentence_two_groups(normalized, sentence_units_limit)
        greedy_groups = _split_sentences_into_groups(normalized, sentence_units_limit)
        if sentence_two:
            # 均衡两组:两行各自是完整句子的组合。
            wrapped_lines = sentence_two
            wrap_meta['forced_wrap'] = True
        elif len(greedy_groups) == 2:
            wrapped_lines = greedy_groups
            wrap_meta['forced_wrap'] = True
        elif len(greedy_groups) > 2:
            wrapped_lines = _build_wrapped_lines_for_ass(
                normalized,
                is_portrait=is_portrait,
                max_line_length=max_line_length,
                max_lines=max_lines,
                single_line_limit=single_line_limit,
                aggressive=False,
            )
            wrap_meta['sentence_split_lines'] = greedy_groups
        else:
            wrapped_lines = _build_wrapped_lines_for_ass(
                normalized,
                is_portrait=is_portrait,
                max_line_length=max_line_length,
                max_lines=max_lines,
                single_line_limit=single_line_limit,
                aggressive=False,
            )

    candidate_lines = wrapped_lines
    sentence_aligned = not single_kept and (
        wrapped_lines is sentence_two or wrapped_lines is greedy_groups
    ) and len(wrapped_lines) <= 2
    if sentence_aligned:
        # 句子对齐结果不做硬换行回退(那会重新引入跨句混行);超出安全
        # 宽度仅置 overflow_warning,由文档层决定是否分时拆分。
        fits, _, _, _ = _check_ass_lines_width_safety(
            candidate_lines,
            usable_width,
            font_size,
            outline,
            shadow,
        )
    else:
        fits, _, _, _ = _check_ass_lines_width_safety(
            wrapped_lines,
            usable_width,
            font_size,
            outline,
            shadow,
        )
        if not fits:
            hard_wrap_lines, hard_wrap_fits = _find_safe_hard_wrap_lines(
                normalized,
                max_line_length=max_line_length,
                max_lines=max_lines,
                usable_width=usable_width,
                font_size=font_size,
                outline=outline,
                shadow=shadow,
            )
            if hard_wrap_lines:
                candidate_lines = hard_wrap_lines
                wrap_meta['forced_wrap'] = candidate_lines != wrapped_lines
            fits = hard_wrap_fits

    # 2026-09-29 用户需求: 任何朝向最多 2 行。两行仍放不下的超长 cue 不再
    # 追加行数(旧竖屏不限行贪心与横屏 3-4 行救援分支均已删除),保持两行并
    # 置 overflow_warning,由 ASS 文档层(_build_default_ass_document)按
    # 时间轴拆分成两条 cue 分时显示。
    candidate_lines = _limit_wrapped_lines(candidate_lines, max_lines)

    # 2026-09-16 恒定字号: 逐条缩字号已彻底移除,Dialogue 一律使用样式基准
    # 字号。残余超宽仅置 overflow_warning;WrapStyle=2 禁止 libass 二次
    # 换行,屏幕上不会出现第三行。
    if not fits and candidate_lines:
        wrap_meta['overflow_warning'] = True

    wrap_meta['wrap_lines'] = [line for line in candidate_lines if str(line or '').strip()]
    ass_text = _compose_ass_dialogue_text(candidate_lines)
    return (ass_text, wrap_meta) if return_meta else ass_text

def _rebalance_split_cue_durations(cues):
    if not cues:
        return []

    fixed_cues = [dict(cue or {}) for cue in cues]
    minimum_visible = 0.35
    preferred_duration = 0.6

    for idx, cue in enumerate(fixed_cues):
        start = float(cue.get('start', 0.0) or 0.0)
        end = float(cue.get('end', 0.0) or 0.0)
        if end - start >= 0.05:
            continue

        if idx > 0:
            prev = fixed_cues[idx - 1]
            prev_start = float(prev.get('start', 0.0) or 0.0)
            prev_end = float(prev.get('end', 0.0) or 0.0)
            target_start = max(prev_start + minimum_visible, end - preferred_duration)
            if target_start < end:
                prev['end'] = target_start
                cue['start'] = target_start
                cue['end'] = end
                continue

        if idx + 1 < len(fixed_cues):
            nxt = fixed_cues[idx + 1]
            next_start = float(nxt.get('start', 0.0) or 0.0)
            next_end = float(nxt.get('end', 0.0) or 0.0)
            target_end = min(next_end - minimum_visible, start + preferred_duration)
            if target_end > start:
                cue['start'] = start
                cue['end'] = target_end
                nxt['start'] = target_end

    return fixed_cues

def _parse_subtitle_text_to_cues(subtitle_text, video_width=None, video_height=None):
    from .srt_transform_engine import SrtTransformConfig, SrtTransformEngine

    # Hard-coded large limits for SRT parsing: never split an incoming SRT
    # cue here. The ASS burn-in stage (_wrap_subtitle_text_for_ass) decides
    # later whether to scale the font or wrap, based on the real video
    # dimensions and the single-line priority settings.
    engine = SrtTransformEngine(
        SrtTransformConfig(
            max_line_length=999,
            max_lines=99,
            normalize_punctuation=False,
            filter_filler_words=False,
        )
    )
    cues = engine.parse_srt(subtitle_text or '')
    if not cues:
        return []
    processed = engine.apply_text_processing(cues)
    return _rebalance_split_cue_durations(processed)

# 2026-09-29 两行硬上限配套:超长 cue 按时间轴拆分的时长门槛。
_ASS_OVERFLOW_SPLIT_MIN_DURATION = 2.0
_ASS_OVERFLOW_SPLIT_MIN_PART = 0.8

def _split_ass_cue_in_time(cue, lines):
    """把一条 cue 按行/句子组拆成多条顺序 cue,分时显示。

    恒定字号不动(不逐条缩字号);时长按各组视觉单位比例分配,每段至少
    _ASS_OVERFLOW_SPLIT_MIN_PART 秒,组数过多或时长不足时保持整条原样
    (避免闪帧)。lines 为未转义的原始行;返回 [(start, end, 已转义文本)]。
    """
    start = float((cue or {}).get('start', 0.0) or 0.0)
    end = float((cue or {}).get('end', 0.0) or 0.0)
    groups = [str(line or '').strip() for line in (lines or []) if str(line or '').strip()]
    # 兜底展示同样遵守两行硬上限:组数超限时合并回两行。
    fallback_text = r'\N'.join(
        _escape_ass_text_line(line) for line in _limit_wrapped_lines(groups, 2)
    )
    default = [(start, end, fallback_text)]
    if len(groups) < 2:
        return default
    escaped_lines = [_escape_ass_text_line(group) for group in groups]

    duration = end - start
    if duration < _ASS_OVERFLOW_SPLIT_MIN_DURATION:
        return default
    units = [_estimate_subtitle_text_units(group) for group in groups]
    total_units = sum(units)
    if total_units <= 0:
        return default

    n = len(groups)
    cuts = []
    cumulative = 0.0
    for units_value in units[:-1]:
        cumulative += units_value
        cuts.append(start + duration * (cumulative / total_units))
    for idx, cut in enumerate(cuts):
        if cut - start < _ASS_OVERFLOW_SPLIT_MIN_PART * (idx + 1):
            return default
        if end - cut < _ASS_OVERFLOW_SPLIT_MIN_PART * (n - 1 - idx):
            return default
    prev_cut = start
    for cut in cuts:
        if cut <= prev_cut:
            return default
        prev_cut = cut

    events = []
    segment_start = start
    for cut, escaped_text in zip(cuts, escaped_lines[:-1]):
        events.append((segment_start, cut, escaped_text))
        segment_start = cut
    events.append((segment_start, end, escaped_lines[-1]))
    return events

def _build_default_ass_document(
    cues,
    font_family,
    video_width,
    video_height,
    *,
    prefer_single_line=True,
):
    style, force_style = _build_subtitle_style_description(
        font_family,
        video_width,
        video_height,
    )
    font_name = force_style['FontName']
    ass_header = (
        "[Script Info]\n"
        "Title: Streaming Subtitle\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {style['PlayResX']}\n"
        f"PlayResY: {style['PlayResY']}\n"
        # WrapStyle=2: 禁止 libass 自动换行。断行完全由 _wrap_subtitle_text_for_ass
        # 决定(最多 2 行),渲染器不得把已定稿的行再折出第三/四行。
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "Collisions: Normal\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,"
        f"{font_name},"
        f"{force_style['FontSize']},"
        f"{style['PrimaryColour']},"
        f"{style['SecondaryColour']},"
        f"{style['OutlineColour']},"
        f"{style['BackColour']},"
        f"{style['Bold']},0,0,0,100,100,0,0,"
        f"{style['BorderStyle']},"
        f"{force_style['Outline']},"
        f"{force_style['Shadow']},"
        f"{force_style['Alignment']},"
        f"{force_style['MarginL']},"
        f"{force_style['MarginR']},"
        f"{force_style['MarginV']},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    ass_lines = []
    forced_wrap_count = 0
    font_override_count = 0
    overflow_warning_count = 0
    for cue in cues or []:
        cue_dict = cue if isinstance(cue, dict) else {}
        wrapped_result = _wrap_subtitle_text_for_ass(
            cue_dict.get('text', ''),
            video_width,
            video_height,
            return_meta=True,
            prefer_single_line=prefer_single_line,
        )
        # `return_meta=True` is expected to return a tuple, but keep a safe fallback
        # to satisfy static analysis and guard unexpected call-path changes.
        if isinstance(wrapped_result, tuple):
            text, wrap_meta = wrapped_result
        else:
            text = wrapped_result
            wrap_meta = {}
        if not text:
            continue
        if wrap_meta.get('forced_wrap'):
            forced_wrap_count += 1
        if wrap_meta.get('font_override'):
            font_override_count += 1
        if wrap_meta.get('overflow_warning'):
            overflow_warning_count += 1
        # 2026-09-30 阅读习惯: 句子组超过两行的 cue 按句子分时显示(每条
        # 完整句子独占一条 cue);两行仍溢出的 cue 沿用两行分时拆分。
        sentence_groups = [
            group for group in (wrap_meta.get('sentence_split_lines') or [])
            if str(group or '').strip()
        ]
        if len(sentence_groups) >= 3:
            sub_cues = _split_ass_cue_in_time(cue_dict, sentence_groups)
        elif wrap_meta.get('overflow_warning'):
            wrap_lines = [
                line for line in (wrap_meta.get('wrap_lines') or [])
                if str(line or '').strip()
            ]
            sub_cues = _split_ass_cue_in_time(cue_dict, wrap_lines)
        else:
            sub_cues = [(
                float(cue_dict.get('start', 0.0) or 0.0),
                float(cue_dict.get('end', 0.0) or 0.0),
                text,
            )]
        for sub_start, sub_end, sub_text in sub_cues:
            ass_lines.append(
                "Dialogue: 0,"
                f"{_seconds_to_ass_timestamp(sub_start)},"
                f"{_seconds_to_ass_timestamp(sub_end)},"
                "Default,,0,0,0,,"
                f"{sub_text}"
            )

    body = '\n'.join(ass_lines)
    if body:
        body += '\n'
    if forced_wrap_count or font_override_count or overflow_warning_count:
        logger.debug(
            "ASS overflow guard summary: forced_wrap=%s, font_override=%s, overflow_warning=%s, cues=%s",
            forced_wrap_count,
            font_override_count,
            overflow_warning_count,
            len(ass_lines),
        )
    if overflow_warning_count:
        logger.warning(
            "ASS overflow guard detected %s cue(s) still too wide after constant-font wrapping",
            overflow_warning_count,
        )
    return ass_header + body
