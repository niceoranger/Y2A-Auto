# modules/dub_timing.py
"""配音时长拟合纯逻辑(无 I/O,无 TTS)。

RubberBand -t <time_ratio>: 输出时长 = 输入时长 * time_ratio
max_tempo: 最大加速倍率(默认 1.5) → min_time_ratio = 1/max_tempo
"""
from typing import Dict, Optional


def fit_segment_audio(
    natural_sec: float,
    target_sec: float,
    max_tempo: float = 1.5,
) -> Dict[str, Optional[float]]:
    """决定一段 TTS 如何对齐目标时长。

    Returns dict:
      mode: 'pad' | 'stretch' | 'stretch_and_truncate' | 'skip'
      time_ratio: RubberBand -t 参数(1.0=不变)
      pad_to_sec: pad 模式下的目标总时长;否则 None
      truncate_to_sec: 截断模式下的硬截时长;否则 None
    """
    if natural_sec is None or target_sec is None:
        return {"mode": "skip", "time_ratio": 1.0, "pad_to_sec": None, "truncate_to_sec": None}
    try:
        natural_sec = float(natural_sec)
        target_sec = float(target_sec)
        max_tempo = float(max_tempo) if max_tempo else 1.5
    except (TypeError, ValueError):
        return {"mode": "skip", "time_ratio": 1.0, "pad_to_sec": None, "truncate_to_sec": None}

    if natural_sec <= 0 or target_sec <= 0 or max_tempo <= 0:
        return {"mode": "skip", "time_ratio": 1.0, "pad_to_sec": None, "truncate_to_sec": None}

    if natural_sec <= target_sec:
        # 偏短:静音 pad 到目标(不拉长,更自然)
        return {
            "mode": "pad",
            "time_ratio": 1.0,
            "pad_to_sec": target_sec,
            "truncate_to_sec": None,
        }

    # 偏长:需要加速 → time_ratio = target/natural < 1
    desired_ratio = target_sec / natural_sec
    min_ratio = 1.0 / max_tempo
    if desired_ratio >= min_ratio:
        return {
            "mode": "stretch",
            "time_ratio": desired_ratio,
            "pad_to_sec": None,
            "truncate_to_sec": None,
        }

    # 即使最大加速仍超长:先加速到上限,再硬截到 target
    return {
        "mode": "stretch_and_truncate",
        "time_ratio": min_ratio,
        "pad_to_sec": None,
        "truncate_to_sec": target_sec,
    }
