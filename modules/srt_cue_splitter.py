"""SRT 长 cue 拆分器（规则版，瞬时）。

背景：whisperx 产出段级 SRT，连续语流会合成 20-40 秒整段 cue（一段话）。
AI 分段模块需要字级时间戳才能工作（段级输入会基线回退不拆分，且 22 分钟/条太重）。
本模块用确定性规则拆分：
  1) 按句末标点(. ! ? 。！？；;)切句；句仍超时长再按逗号(，,)切
  2) 时长按视觉字符数比例分配
  3) 保留原 cue 顺序与编号
"""
import re
from typing import List, Tuple

_TIME_RE = re.compile(
    r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})')


def _tc_to_ms(h, m, s, ms):
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def _ms_to_tc(ms):
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms2 = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms2:03d}"


def _visual_len(text: str) -> float:
    # \u4e2d\u65e5\u97e9\u5bbd\u5b57\u7b26\u6309 2 \u8ba1
    return sum(2.0 if ord(c) > 0x2E7F else 1.0 for c in text)


_ABBREV = ('U.S.', 'U.K.', 'D.C.', 'Mr.', 'Mrs.', 'Ms.', 'Dr.', 'Sen.',
            'Rep.', 'Gov.', 'Prof.', 'vs.', 'etc.', 'Inc.', 'Co.', 'Jr.',
            'Sr.', 'St.', 'No.', 'Feb.', 'Sept.', 'a.m.', 'p.m.')


def _sent_split_positions(text: str) -> list:
    """合法句末切点：排除小数点(0.4)、缩写(U.S.)，要求后接空白+大写。"""
    positions = []
    for m in re.finditer(r'[.!?]', text):
        i = m.end()
        prev = text[i - 2] if i >= 2 else ''
        nxt = text[i] if i < len(text) else ''
        # 小数点: 前后都是数字 (0.4%, 3.4)
        if prev.isdigit() and nxt.isdigit():
            continue
        # 缩写词尾 (U.S. / Sen. ...)
        if any(text[max(0, i - len(a)):i] == a for a in _ABBREV):
            continue
        # 缩写字母点 (X.Y. 形态, 如 U.S. 的第二个点已由上面覆盖; 防单字母缩写)
        if prev.isalpha() and prev.isupper() and (i >= 3 and text[i - 3] == '.'):
            continue
        rest = text[i:]
        if rest and not re.match(r"\s+[\"'\u201c\u2018A-Z]", rest):
            continue
        positions.append(i)
    return positions


def _split_by_positions(text: str, positions: list) -> list:
    parts = []
    prev = 0
    for pos in positions:
        seg = text[prev:pos].strip()
        if seg:
            parts.append(seg)
        prev = pos
    tail = text[prev:].strip()
    if tail:
        parts.append(tail)
    return parts


def _split_text(text: str, max_units: float) -> List[str]:
    """先按句切(小数/缩写保护)，仍超长再按子句切，最后按词界硬切；碎片合并。"""
    pieces = []
    for sent in _split_by_positions(text, _sent_split_positions(text)):
        if _visual_len(sent) <= max_units:
            pieces.append(sent)
            continue
        buf = ''
        for part in re.split(r'(?<=[，,])\s*', sent):
            if buf and _visual_len(buf + part) > max_units:
                pieces.append(buf.strip())
                buf = part
            else:
                buf = (buf + part) if buf else part
        if buf.strip():
            pieces.append(buf.strip())
    # 硬切仍超长子句: 按词界(空格)回退切, 绝不拆单词
    final = []
    for p in pieces:
        while _visual_len(p) > max_units * 1.6:
            cut = max(4, int(max_units * 0.8))
            acc, idx = 0.0, -1
            for i, c in enumerate(p):
                acc += 2.0 if ord(c) > 0x2E7F else 1.0
                if acc >= cut:
                    idx = i + 1
                    break
            if idx <= 0:
                break
            # 回退到最近的空格(词界); 中文无空格则按字符
            back = p.rfind(' ', 0, idx)
            if back > int(cut * 0.3):
                idx = back + 1
            if idx >= len(p):
                break
            final.append(p[:idx].strip())
            p = p[idx:].strip()
        if p:
            final.append(p)
    # 碎片合并: 极短片段(≤6视觉单位)并入前片
    merged = []
    for p in final:
        if merged and _visual_len(p) <= 6 and _visual_len(merged[-1] + ' ' + p) <= max_units * 1.2:
            merged[-1] = merged[-1] + ' ' + p
        else:
            merged.append(p)
    return [p for p in merged if p]


def split_long_cues(srt_path: str, max_duration_s: float = 5.5,
                    max_units: float = 48.0, logger=None) -> Tuple[bool, int, int]:
    """原地拆分 SRT 中的长 cue。返回 (是否修改, 原cue数, 新cue数)。"""
    raw = open(srt_path, encoding='utf-8-sig').read()
    blocks = [b for b in re.split(r'\n\s*\n', raw) if b.strip()]
    out_blocks = []
    orig_n = new_n = 0
    for block in blocks:
        lines = [l for l in block.splitlines() if l.strip()]
        m = None
        for i, l in enumerate(lines):
            m = _TIME_RE.search(l)
            if m:
                text = ' '.join(lines[i + 1:]).strip()
                break
        if not m or not text:
            out_blocks.append(block)
            orig_n += 1
            new_n += 1
            continue
        orig_n += 1
        start_ms = _tc_to_ms(m.group(1), m.group(2), m.group(3), m.group(4))
        end_ms = _tc_to_ms(m.group(5), m.group(6), m.group(7), m.group(8))
        dur_ms = end_ms - start_ms
        units = _visual_len(text)
        if dur_ms <= max_duration_s * 1000 and units <= max_units:
            out_blocks.append(block)
            new_n += 1
            continue
        pieces = _split_text(text, max_units)
        if len(pieces) <= 1:
            out_blocks.append(block)
            new_n += 1
            continue
        total_units = sum(_visual_len(p) for p in pieces) or 1.0
        # 先给每个 piece 分配比例时长
        cursor = start_ms
        timed = []
        for j, piece in enumerate(pieces):
            frac = _visual_len(piece) / total_units
            seg_dur = int(dur_ms * frac)
            seg_start = cursor
            seg_end = end_ms if j == len(pieces) - 1 else min(end_ms, cursor + seg_dur)
            if seg_end <= seg_start:
                seg_end = seg_start + 300
            timed.append((seg_start, seg_end, piece))
            cursor = seg_end
        # 打包成双行 cue：同句相邻短段合并为上下两行同时展示，
        # 降低切换频率（单条总时长不超过 1.6 倍上限，两行各不超行宽）
        packed = []
        buf, buf_dur = [], 0
        for seg_start, seg_end, piece in timed:
            p_dur = seg_end - seg_start
            if buf and (len(buf) >= 2 or buf_dur + p_dur > max_duration_s * 1600):
                packed.append((buf[0][0], buf[-1][1], buf))
                buf, buf_dur = [], 0
            buf.append((seg_start, seg_end, piece))
            buf_dur += p_dur
        if buf:
            packed.append((buf[0][0], buf[-1][1], buf))
        for seg_start, seg_end, group in packed:
            lines_text = '\n'.join(g[2] for g in group)
            out_blocks.append(
                f"{new_n + 1}\n{_ms_to_tc(seg_start)} --> {_ms_to_tc(seg_end)}\n{lines_text}")
            new_n += 1
    if new_n == orig_n:
        return False, orig_n, new_n
    with open(srt_path, 'w', encoding='utf-8') as f:
        f.write('\n\n'.join(out_blocks) + '\n')
    if logger:
        logger.info(f"长cue拆分: {orig_n} → {new_n} 条")
    return True, orig_n, new_n
