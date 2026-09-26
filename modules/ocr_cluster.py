# modules/ocr_cluster.py
"""OCR 检测框时间聚类纯逻辑(无 I/O)。

box: 归一化 [x, y, w, h]，原点左上，相对画面宽高。

聚类策略(多轨):
  - 每帧可能检出多个框(字幕+标题+水印);单链聚类会把不同轨互相打断。
  - 多轨:每个检测找最佳匹配的活跃段(IoU>=threshold 且 gap<=max_gap),否则开新段;
    超过 max_gap 未续上的活跃段关闭落盘。
  - 点段扩展:单帧检测代表"此刻字幕可见",实际持续到下一采样点;
    若提供 sample_interval,段 end 扩展一个 interval(便于 delogo enable 覆盖整段)。
"""
from typing import Dict, List, Optional


def iou_box(a, b) -> float:
    """a,b: [x,y,w,h] 归一化。"""
    if not a or not b or len(a) < 4 or len(b) < 4:
        return 0.0
    ax1, ay1, aw, ah = float(a[0]), float(a[1]), float(a[2]), float(a[3])
    bx1, by1, bw, bh = float(b[0]), float(b[1]), float(b[2]), float(b[3])
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx2, by2 = bx1 + bw, by1 + bh
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    if union <= 0:
        return 0.0
    return inter / union


def merge_box(a, b) -> List[float]:
    """像素无关：归一化框并集。"""
    ax1, ay1 = float(a[0]), float(a[1])
    ax2, ay2 = ax1 + float(a[2]), ay1 + float(a[3])
    bx1, by1 = float(b[0]), float(b[1])
    bx2, by2 = bx1 + float(b[2]), by1 + float(b[3])
    x1, y1 = min(ax1, bx1), min(ay1, by1)
    x2, y2 = max(ax2, bx2), max(ay2, by2)
    return [x1, y1, max(0.0, x2 - x1), max(0.0, y2 - y1)]


def filter_bottom_band(dets: List[Dict], min_y_ratio: float = 0.6) -> List[Dict]:
    """保留底栏区域检测:box 顶部 y >= min_y_ratio。

    min_y_ratio<=0 时不过滤(返回原列表)。用于优先保留烧录字幕(通常在画面下部),
    过滤标题/水印/大块误检。
    """
    if not dets or min_y_ratio is None or min_y_ratio <= 0:
        return list(dets)
    out = []
    for d in dets:
        box = d.get("box") if isinstance(d, dict) else None
        if not box or len(box) < 4:
            continue
        if float(box[1]) >= min_y_ratio:
            out.append(d)
    return out


def cluster_detections(
    dets: List[Dict],
    iou_threshold: float = 0.5,
    max_gap_sec: float = 0.75,
    sample_interval: Optional[float] = None,
) -> List[Dict]:
    """多轨聚类 → [{start, end, box, score}, ...]。

    若提供 sample_interval(>0),每段 end 扩展一个 interval(点段覆盖到下一采样点),
    便于下游 delogo enable 覆盖整段可见时间。
    """
    if not dets:
        return []
    ordered = sorted(
        (d for d in dets if d and "t" in d and "box" in d),
        key=lambda d: float(d["t"]),
    )
    if not ordered:
        return []

    def _new_seg(d):
        box = list(map(float, d["box"][:4]))
        return {
            "start": float(d["t"]),
            "end": float(d["t"]),
            "box": box,
            "score_sum": float(d.get("score") or 0.0),
            "score_n": 1,
            "last_box": box,
            "last_t": float(d["t"]),
            "texts": [],
        }

    def _push_text(seg, d):
        text = str(d.get("text") or "").strip()
        if text and text not in seg["texts"] and len(seg["texts"]) < 8:
            seg["texts"].append(text)

    def _finalize(seg):
        out = {
            "start": seg["start"],
            "end": seg["end"],
            "box": seg["box"],
            "score": seg["score_sum"] / max(1, seg["score_n"]),
        }
        if seg.get("texts"):
            out["texts"] = seg["texts"]
        return out

    first_seg = _new_seg(ordered[0])
    _push_text(first_seg, ordered[0])
    active: List[Dict] = [first_seg]
    results: List[Dict] = []

    for d in ordered[1:]:
        t = float(d["t"])
        box = list(map(float, d["box"][:4]))
        score = float(d.get("score") or 0.0)

        # 关闭超 gap 的活跃段
        still_active = []
        for seg in active:
            if t - seg["last_t"] <= max_gap_sec:
                still_active.append(seg)
            else:
                results.append(_finalize(seg))
        active = still_active

        # 找最佳匹配(IoU 最大且 >= threshold)
        best_idx, best_iou = -1, iou_threshold
        for i, seg in enumerate(active):
            iou = iou_box(seg["last_box"], box)
            if iou > best_iou:
                best_iou = iou
                best_idx = i

        if best_idx >= 0:
            seg = active[best_idx]
            seg["end"] = t
            seg["box"] = merge_box(seg["box"], box)
            seg["last_box"] = box
            seg["last_t"] = t
            seg["score_sum"] += score
            seg["score_n"] += 1
            _push_text(seg, d)
        else:
            seg = _new_seg(d)
            _push_text(seg, d)
            active.append(seg)

    for seg in active:
        results.append(_finalize(seg))

    # 按 start 排序,便于下游稳定
    results.sort(key=lambda s: (s["start"], s["end"]))

    # 点段扩展:单帧检测持续到下一采样点
    if sample_interval and sample_interval > 0:
        for seg in results:
            if seg["end"] <= seg["start"]:
                seg["end"] = seg["start"] + sample_interval
            else:
                # 段尾也补一个 interval,覆盖最后一个采样点到下一次(若有)
                seg["end"] = seg["end"] + sample_interval
    return results
