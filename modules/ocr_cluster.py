# modules/ocr_cluster.py
"""OCR 检测框时间聚类纯逻辑(无 I/O)。

box: 归一化 [x, y, w, h]，原点左上，相对画面宽高。
"""
from typing import Dict, List


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


def cluster_detections(
    dets: List[Dict],
    iou_threshold: float = 0.5,
    max_gap_sec: float = 0.75,
) -> List[Dict]:
    """将 [{t, box, score}, ...] 聚类为 [{start, end, box, score}, ...]。

    规则：按 t 排序；若与当前段 last 框 IoU>=threshold 且时间 gap<=max_gap_sec，
    则并入（box 并集，score 均值）；否则新开段。
    """
    if not dets:
        return []
    ordered = sorted(
        (d for d in dets if d and "t" in d and "box" in d),
        key=lambda d: float(d["t"]),
    )
    if not ordered:
        return []

    segments = []
    cur = {
        "start": float(ordered[0]["t"]),
        "end": float(ordered[0]["t"]),
        "box": list(map(float, ordered[0]["box"][:4])),
        "score_sum": float(ordered[0].get("score") or 0.0),
        "score_n": 1,
        "last_box": list(map(float, ordered[0]["box"][:4])),
        "last_t": float(ordered[0]["t"]),
    }

    for d in ordered[1:]:
        t = float(d["t"])
        box = list(map(float, d["box"][:4]))
        score = float(d.get("score") or 0.0)
        gap = t - cur["last_t"]
        if gap <= max_gap_sec and iou_box(cur["last_box"], box) >= iou_threshold:
            cur["end"] = t
            cur["box"] = merge_box(cur["box"], box)
            cur["last_box"] = box
            cur["last_t"] = t
            cur["score_sum"] += score
            cur["score_n"] += 1
        else:
            segments.append({
                "start": cur["start"],
                "end": cur["end"],
                "box": cur["box"],
                "score": cur["score_sum"] / max(1, cur["score_n"]),
            })
            cur = {
                "start": t,
                "end": t,
                "box": box,
                "score_sum": score,
                "score_n": 1,
                "last_box": box,
                "last_t": t,
            }

    segments.append({
        "start": cur["start"],
        "end": cur["end"],
        "box": cur["box"],
        "score": cur["score_sum"] / max(1, cur["score_n"]),
    })
    return segments
