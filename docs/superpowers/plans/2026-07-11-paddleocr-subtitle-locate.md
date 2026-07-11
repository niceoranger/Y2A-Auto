# PaddleOCR 字幕定位 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 AI 重制管线中新增 `remaster_ocr` stage：对本地视频抽帧跑 PaddleOCR，将烧录字幕文本框聚类为时间线 bbox 段，落盘 `ocr_subtitle_boxes_<task_id>.json`；默认关，软失败。

**Architecture:** 与 WhisperX/Demucs/配音同构——`asr-venv` 跑 `ocr_runner.py` CLI；`OcrLocator` subprocess adapter；聚类纯逻辑拆到 `ocr_cluster.py`；`task_manager._run_remaster_ocr` 软失败接线，与 ASR/Demucs 同一 `REMASTER_PIPELINE_ENABLED` 门控块（asr → demucs → ocr）。

**Tech Stack:** Python 3 / paddleocr + paddlepaddle (CPU) / ffmpeg / subprocess / unittest

**Test runner (项目事实):** pytest 未安装。统一用：
```bash
.venv/bin/python -m unittest <module.path> -v
# 或全量
.venv/bin/python -m unittest discover tests -v
```

---

## 参考文件（实现者应先读）

- Spec: `docs/superpowers/specs/2026-07-11-paddleocr-subtitle-locate-design.md`
- `modules/dub_generator.py` / `modules/demucs_separator.py` — adapter 骨架
- `modules/task_manager.py` — remaster 门控块（download 后 asr/demucs）、schema/迁移/ALLOWED_COLUMNS 中 `dub_warning_message` 三件套、`_run_remaster_demucs`
- `modules/config_manager.py` — DUB_* 块后插入 OCR_*
- `tests/test_dub_generator.py` / `test_dub_config.py` / `test_dub_warning_persistence.py` / `test_demucs_separator.py`

**关键契约：**
- adapter 返回 `(bool, dict | str)`
- runner stdout 末行 JSON；退出码 0/1/2
- 软失败：只写 `ocr_warning_message` + 还原 status
- 只落盘 JSON，不写 `video_path_*` / `subtitle_path_*`
- `box` 归一化 `[x,y,w,h]` 相对画面宽高，原点左上

**门控注意（以当前代码为准）：**  
remaster 块条件为 `PIPELINE_STAGE_REMASTER_ASR not in completed_stages`。块内顺序 asr → demucs → **ocr**，各自 try/except + `_mark_stage_done`。OCR 插在 demucs 的 `_mark_stage_done` 之后、字幕处理注释 `# 5. 字幕处理` 之前。

---

## File map

| 文件 | 职责 |
|------|------|
| `modules/ocr_cluster.py` | IoU / 并集 / 时间聚类纯逻辑 |
| `modules/ocr_runner.py` | asr-venv CLI：抽帧 + PaddleOCR + 聚类 + JSON |
| `modules/ocr_locator.py` | subprocess adapter |
| `modules/config_manager.py` | OCR_* 键 |
| `modules/task_manager.py` | stage/状态/DB/`_run_remaster_ocr`/门控 |
| `tests/test_ocr_*.py` | 单测 |

---

## Task 0: 环境准备（实现前）

**Files:** 无（环境操作）

- [ ] **Step 1: asr-venv 安装 paddlepaddle + paddleocr**

```bash
# Apple Silicon / CPU：按 paddle 官网当前 CPU wheel 指引；若失败记录错误再换镜像源
/Users/mac/asr-venv/bin/pip install -U paddlepaddle paddleocr
/Users/mac/asr-venv/bin/python -c "from paddleocr import PaddleOCR; print('PaddleOCR OK')"
```
Expected: import 成功。首次可能拉 det/rec 模型权重。

若 `paddlepaddle` 与现有 torch 严重冲突导致 import 失败：优先固定兼容版本（如文档推荐的 paddle 版本），**不要**为此改主项目 `.venv`。

- [ ] **Step 2: 冒烟探测（可选短图）**

```bash
/Users/mac/asr-venv/bin/python - <<'PY'
from paddleocr import PaddleOCR
ocr = PaddleOCR(use_angle_cls=True, lang='ch', show_log=False)
print("inited", type(ocr))
PY
```
Expected: 初始化成功（可能下载模型）。

---

## Task 1: config 新键 + 单测（TDD）

**Files:**
- Modify: `modules/config_manager.py`（DUB 块之后）
- Create: `tests/test_ocr_config.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_ocr_config.py`:
```python
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.config_manager import DEFAULT_CONFIG


class TestOcrConfig(unittest.TestCase):
    def test_defaults_present(self):
        for k in [
            "OCR_PYTHON", "OCR_RUNNER", "OCR_SAMPLE_INTERVAL_SEC",
            "OCR_LANG", "OCR_DEVICE", "OCR_IOU_THRESHOLD", "OCR_TIMEOUT_SECONDS",
        ]:
            self.assertIn(k, DEFAULT_CONFIG)
        self.assertEqual(DEFAULT_CONFIG["OCR_PYTHON"], "/Users/mac/asr-venv/bin/python")
        self.assertEqual(DEFAULT_CONFIG["OCR_RUNNER"], "modules/ocr_runner.py")
        self.assertEqual(DEFAULT_CONFIG["OCR_SAMPLE_INTERVAL_SEC"], 0.5)
        self.assertEqual(DEFAULT_CONFIG["OCR_LANG"], "ch")
        self.assertEqual(DEFAULT_CONFIG["OCR_DEVICE"], "cpu")
        self.assertEqual(DEFAULT_CONFIG["OCR_IOU_THRESHOLD"], 0.5)
        self.assertEqual(DEFAULT_CONFIG["OCR_TIMEOUT_SECONDS"], 3600)

    def test_remaster_switch_still_off(self):
        self.assertIn("REMASTER_PIPELINE_ENABLED", DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["REMASTER_PIPELINE_ENABLED"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/mac/Y2A-Auto && .venv/bin/python -m unittest tests.test_ocr_config -v
```
Expected: FAIL — `OCR_PYTHON` not in DEFAULT_CONFIG

- [ ] **Step 3: 加 config 键**

In `modules/config_manager.py`，在 DUB 块末尾：
```python
    "DUB_TIMEOUT_SECONDS": 14400,        # 4h;CPU 配音慢
```
**之后**插入：
```python
    # AI 重制管线——PaddleOCR 字幕定位(复用 REMASTER 总开关)
    "OCR_PYTHON": "/Users/mac/asr-venv/bin/python",
    "OCR_RUNNER": "modules/ocr_runner.py",
    "OCR_SAMPLE_INTERVAL_SEC": 0.5,
    "OCR_LANG": "ch",
    "OCR_DEVICE": "cpu",
    "OCR_IOU_THRESHOLD": 0.5,
    "OCR_TIMEOUT_SECONDS": 3600,
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/bin/python -m unittest tests.test_ocr_config -v
```
Expected: OK (2 tests)

- [ ] **Step 5: Commit**

```bash
git add modules/config_manager.py tests/test_ocr_config.py
git commit -m "feat(ocr): config 新增 OCR_* 键(复用 REMASTER 总开关,默认关)"
```

---

## Task 2: ocr_cluster 纯逻辑 + 单测（TDD）

**Files:**
- Create: `modules/ocr_cluster.py`
- Create: `tests/test_ocr_cluster.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_ocr_cluster.py`:
```python
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.ocr_cluster import cluster_detections, iou_box, merge_box


class TestOcrCluster(unittest.TestCase):
    def test_iou_identical(self):
        b = [0.1, 0.8, 0.8, 0.1]
        self.assertAlmostEqual(iou_box(b, b), 1.0, places=6)

    def test_iou_disjoint(self):
        a = [0.0, 0.0, 0.2, 0.2]
        b = [0.5, 0.5, 0.2, 0.2]
        self.assertEqual(iou_box(a, b), 0.0)

    def test_merge_box_union(self):
        a = [0.1, 0.1, 0.2, 0.2]  # x2=0.3,y2=0.3
        b = [0.2, 0.2, 0.2, 0.2]  # x2=0.4,y2=0.4
        m = merge_box(a, b)
        self.assertAlmostEqual(m[0], 0.1)
        self.assertAlmostEqual(m[1], 0.1)
        self.assertAlmostEqual(m[2], 0.3)  # 0.4-0.1
        self.assertAlmostEqual(m[3], 0.3)

    def test_cluster_merges_nearby_same_region(self):
        dets = [
            {"t": 0.0, "box": [0.1, 0.8, 0.8, 0.1], "score": 0.9},
            {"t": 0.5, "box": [0.12, 0.81, 0.78, 0.1], "score": 0.8},
            {"t": 1.0, "box": [0.11, 0.80, 0.79, 0.1], "score": 0.85},
        ]
        segs = cluster_detections(dets, iou_threshold=0.5, max_gap_sec=0.75)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["start"], 0.0)
        self.assertEqual(segs[0]["end"], 1.0)
        self.assertGreater(segs[0]["score"], 0.0)
        # box 归一化仍在 [0,1]
        x, y, w, h = segs[0]["box"]
        self.assertGreaterEqual(x, 0.0)
        self.assertLessEqual(x + w, 1.0 + 1e-6)

    def test_cluster_splits_on_gap(self):
        dets = [
            {"t": 0.0, "box": [0.1, 0.8, 0.8, 0.1], "score": 0.9},
            {"t": 5.0, "box": [0.1, 0.8, 0.8, 0.1], "score": 0.9},
        ]
        segs = cluster_detections(dets, iou_threshold=0.5, max_gap_sec=0.75)
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0]["end"], 0.0)
        self.assertEqual(segs[1]["start"], 5.0)

    def test_cluster_splits_on_low_iou(self):
        dets = [
            {"t": 0.0, "box": [0.1, 0.8, 0.3, 0.1], "score": 0.9},
            {"t": 0.5, "box": [0.6, 0.1, 0.3, 0.1], "score": 0.9},
        ]
        segs = cluster_detections(dets, iou_threshold=0.5, max_gap_sec=0.75)
        self.assertEqual(len(segs), 2)

    def test_cluster_empty(self):
        self.assertEqual(cluster_detections([], iou_threshold=0.5, max_gap_sec=0.75), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m unittest tests.test_ocr_cluster -v
```
Expected: FAIL — module not found

- [ ] **Step 3: 实现 `modules/ocr_cluster.py`**

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/bin/python -m unittest tests.test_ocr_cluster -v
```
Expected: OK (7 tests)

- [ ] **Step 5: Commit**

```bash
git add modules/ocr_cluster.py tests/test_ocr_cluster.py
git commit -m "feat(ocr): ocr_cluster IoU 时间聚类纯逻辑 + 单测"
```

---

## Task 3: ocr_runner.py CLI

**Files:**
- Create: `modules/ocr_runner.py`

> 真模型冒烟在 Task 6。本任务保证 CLI 结构、抽帧、聚类调用、JSON 契约、stdout/退出码。

- [ ] **Step 1: 写 runner**

Create `modules/ocr_runner.py`:

```python
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PaddleOCR 字幕定位 runner。用 /Users/mac/asr-venv/bin/python 跑。

契约:
  CLI: --video <p> --output <json> --task-id <id>
       [--sample-interval 0.5] [--lang ch] [--device cpu] [--iou-threshold 0.5]
  stdout: 进度行 + 末行 JSON {ok, boxes|error}
  退出码: 0 成功 / 1 运行失败 / 2 参数/依赖错
"""
import argparse
import json
import os
import shutil
import subprocess
import sys


def _emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def ffprobe_meta(video_path: str) -> dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-show_entries", "format=duration",
        "-of", "json", video_path,
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        detail = (e.output or b"").decode(errors="ignore")[-300:]
        raise RuntimeError(f"ffprobe 失败: {detail}") from None
    data = json.loads(out.decode("utf-8"))
    streams = data.get("streams") or [{}]
    fmt = data.get("format") or {}
    width = int(streams[0].get("width") or 0)
    height = int(streams[0].get("height") or 0)
    duration = float(fmt.get("duration") or 0.0)
    if width <= 0 or height <= 0:
        raise RuntimeError("无法解析视频宽高")
    return {"width": width, "height": height, "duration_sec": duration}


def extract_frames(video_path: str, workdir: str, interval: float) -> list:
    """抽帧，返回 [(t_sec, frame_path), ...]。"""
    pattern = os.path.join(workdir, "frame_%06d.jpg")
    # fps=1/interval
    fps = 1.0 / max(interval, 1e-3)
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"fps={fps}",
        "-q:v", "2", pattern,
    ]
    print(f"[ocr] 抽帧 interval={interval}s fps={fps:.4f}", flush=True)
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode(errors="ignore")[-400:]
        raise RuntimeError(f"ffmpeg 抽帧失败: {detail}") from None
    frames = sorted(
        f for f in os.listdir(workdir)
        if f.startswith("frame_") and f.endswith(".jpg")
    )
    out = []
    for i, name in enumerate(frames):
        t = i * interval
        out.append((t, os.path.join(workdir, name)))
    return out


def pixel_box_to_norm(box_pts, width: int, height: int) -> list:
    """Paddle 四点 [[x,y],...] 或 [x1,y1,x2,y2] → 归一化 [x,y,w,h]。"""
    if not box_pts:
        return [0.0, 0.0, 0.0, 0.0]
    # 四点
    if isinstance(box_pts[0], (list, tuple)):
        xs = [float(p[0]) for p in box_pts]
        ys = [float(p[1]) for p in box_pts]
        x1, x2 = min(xs), max(xs)
        y1, y2 = min(ys), max(ys)
    else:
        x1, y1, x2, y2 = map(float, box_pts[:4])
        if x2 < x1:
            x1, x2 = x2, x1
        if y2 < y1:
            y1, y2 = y2, y1
    w = max(0.0, (x2 - x1) / float(width))
    h = max(0.0, (y2 - y1) / float(height))
    x = max(0.0, min(1.0, x1 / float(width)))
    y = max(0.0, min(1.0, y1 / float(height)))
    # clamp w/h
    w = min(w, max(0.0, 1.0 - x))
    h = min(h, max(0.0, 1.0 - y))
    return [x, y, w, h]


def filter_box(norm_box, min_area=0.0005, max_aspect=40.0) -> bool:
    """True=保留。过滤极小框与极端宽高比。"""
    _, _, w, h = norm_box
    area = w * h
    if area < min_area:
        return False
    if h <= 1e-9:
        return False
    aspect = w / h
    if aspect > max_aspect or aspect < 1.0 / max_aspect:
        return False
    return True


def run_ocr_on_frames(frames, width, height, lang: str, device: str) -> list:
    """返回 dets: [{t, box, score}, ...]。全画面。"""
    from paddleocr import PaddleOCR

    use_gpu = str(device).lower() in ("gpu", "cuda", "true", "1")
    print(f"[ocr] 加载 PaddleOCR lang={lang} use_gpu={use_gpu}", flush=True)
    # show_log 在部分版本已弃用；兼容用 try
    try:
        ocr = PaddleOCR(use_angle_cls=True, lang=lang, use_gpu=use_gpu, show_log=False)
    except TypeError:
        ocr = PaddleOCR(use_angle_cls=True, lang=lang, use_gpu=use_gpu)

    dets = []
    for i, (t, path) in enumerate(frames):
        if i % 20 == 0:
            print(f"[ocr] 帧 {i+1}/{len(frames)} t={t:.2f}", flush=True)
        try:
            result = ocr.ocr(path, cls=True)
        except Exception as e:
            print(f"[ocr] 帧失败 t={t}: {e}", flush=True)
            continue
        # result: list per image; each [ [box, (text, score)], ... ] or None
        if not result:
            continue
        lines = result[0] if result else None
        if not lines:
            continue
        for item in lines:
            if not item or len(item) < 2:
                continue
            box_pts, meta = item[0], item[1]
            score = float(meta[1]) if meta and len(meta) > 1 else 0.0
            norm = pixel_box_to_norm(box_pts, width, height)
            if not filter_box(norm):
                continue
            dets.append({"t": float(t), "box": norm, "score": score})
    return dets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--sample-interval", type=float, default=0.5)
    ap.add_argument("--lang", default="ch")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--iou-threshold", type=float, default=0.5)
    args = ap.parse_args()

    if not os.path.isfile(args.video):
        _emit({"ok": False, "error": f"视频不存在: {args.video}"})
        return 2
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        _emit({"ok": False, "error": "ffmpeg/ffprobe 未安装或不在 PATH"})
        return 2
    try:
        from paddleocr import PaddleOCR  # noqa: F401
    except Exception as e:
        _emit({"ok": False, "error": f"import paddleocr 失败(检查 asr-venv): {e}"})
        return 2

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from modules.ocr_cluster import cluster_detections

    interval = float(args.sample_interval or 0.5)
    if interval <= 0:
        interval = 0.5
    workdir = os.path.join(
        os.path.dirname(os.path.abspath(args.output)),
        f".ocr_work_{args.task_id}",
    )
    try:
        if os.path.isdir(workdir):
            shutil.rmtree(workdir, ignore_errors=True)
        os.makedirs(workdir, exist_ok=True)
        out_dir = os.path.dirname(os.path.abspath(args.output))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        meta = ffprobe_meta(args.video)
        print(
            f"[ocr] meta {meta['width']}x{meta['height']} dur={meta['duration_sec']:.2f}",
            flush=True,
        )
        frames = extract_frames(args.video, workdir, interval)
        print(f"[ocr] 抽到 {len(frames)} 帧", flush=True)

        dets = run_ocr_on_frames(
            frames, meta["width"], meta["height"],
            lang=str(args.lang or "ch"),
            device=str(args.device or "cpu"),
        )
        print(f"[ocr] 有效检测 {len(dets)} 条", flush=True)

        max_gap = 1.5 * interval
        segments = cluster_detections(
            dets,
            iou_threshold=float(args.iou_threshold or 0.5),
            max_gap_sec=max_gap,
        )
        payload = {
            "video": os.path.abspath(args.video),
            "width": meta["width"],
            "height": meta["height"],
            "duration_sec": meta["duration_sec"],
            "sample_interval_sec": interval,
            "roi": "full",
            "lang": str(args.lang or "ch"),
            "segments": segments,
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        _emit({
            "ok": True,
            "boxes": args.output,
            "segments": len(segments),
            "frames": len(frames),
            "device_used": str(args.device or "cpu"),
        })
        return 0
    except Exception as e:
        import traceback
        _emit({"ok": False, "error": str(e), "trace": traceback.format_exc()[-600:]})
        return 1
    finally:
        if os.path.isdir(workdir):
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 参数错退出码（不跑真模型）**

```bash
cd /Users/mac/Y2A-Auto
/Users/mac/asr-venv/bin/python modules/ocr_runner.py \
  --video /nope.mp4 --output /tmp/o.json --task-id t1
echo "exit=$?"
```
Expected: 末行含 `"ok": false` 与视频不存在；`exit=2`

- [ ] **Step 3: Commit**

```bash
git add modules/ocr_runner.py
git commit -m "feat(ocr): ocr_runner CLI(抽帧+PaddleOCR+聚类→时间线 bbox JSON)"
```

---

## Task 4: OcrLocator adapter + 单测（TDD）

**Files:**
- Create: `modules/ocr_locator.py`
- Create: `tests/test_ocr_locator.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_ocr_locator.py`:
```python
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.ocr_locator import OcrLocator


def _write_stub_runner(path, exit_code=0, final_json=None, lines=None):
    body = ["#!/usr/bin/env bash", "echo '[stub-runner] invoked:' \"$@\" >&2"]
    for ln in (lines or ["[ocr] 抽帧", "[ocr] 检测"]):
        body.append(f"echo {json.dumps(ln, ensure_ascii=False)}")
    if final_json is not None:
        body.append(
            "echo " + json.dumps(json.dumps(final_json, ensure_ascii=False), ensure_ascii=False)
        )
    body.append(f"exit {exit_code}")
    with open(path, "w") as f:
        f.write("\n".join(body) + "\n")
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TestOcrLocator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.video = os.path.join(self.tmp, "v.mp4")
        open(self.video, "w").close()
        self.out = os.path.join(self.tmp, "boxes.json")
        self.stub = os.path.join(self.tmp, "fake-runner")

    def test_missing_python_bin(self):
        _write_stub_runner(self.stub, exit_code=0, final_json={"ok": True})
        loc = OcrLocator(python_bin="/nonexistent/python", runner_path=self.stub)
        ok, res = loc.locate(video_path=self.video, output_json=self.out, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("python", str(res).lower())

    def test_missing_runner(self):
        loc = OcrLocator(python_bin="/bin/bash", runner_path="/nonexistent/runner.py")
        ok, res = loc.locate(video_path=self.video, output_json=self.out, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("runner", str(res).lower())

    def test_missing_video(self):
        _write_stub_runner(self.stub, exit_code=0, final_json={"ok": True})
        loc = OcrLocator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = loc.locate(video_path="/nope/x.mp4", output_json=self.out, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("不存在", str(res))

    def test_success_parses_json(self):
        _write_stub_runner(
            self.stub, exit_code=0,
            final_json={"ok": True, "boxes": self.out, "segments": 3, "frames": 10, "device_used": "cpu"},
            lines=["[ocr] 抽帧"],
        )
        loc = OcrLocator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = loc.locate(video_path=self.video, output_json=self.out, task_id="t1")
        self.assertTrue(ok, msg=str(res))
        self.assertEqual(res["boxes"], self.out)
        self.assertEqual(res["segments"], 3)

    def test_nonzero_exit(self):
        _write_stub_runner(
            self.stub, exit_code=1,
            final_json={"ok": False, "error": "paddleocr 失败"},
        )
        loc = OcrLocator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = loc.locate(video_path=self.video, output_json=self.out, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("失败", str(res))

    def test_progress_callback(self):
        _write_stub_runner(
            self.stub, exit_code=0,
            final_json={"ok": True, "boxes": self.out},
            lines=["抽帧中", "检测中", "聚类"],
        )
        events = []
        loc = OcrLocator(python_bin="/bin/bash", runner_path=self.stub)
        loc.locate(
            video_path=self.video, output_json=self.out, task_id="t1",
            progress_callback=events.append,
        )
        self.assertTrue(any("检测" in e or "聚类" in e for e in events))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m unittest tests.test_ocr_locator -v
```
Expected: FAIL — module not found

- [ ] **Step 3: 实现 adapter**

Create `modules/ocr_locator.py`（照抄 DubGenerator/DemucsSeparator 骨架）:

```python
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OcrLocator —— subprocess 适配器,调 modules/ocr_runner.py(用 asr-venv python)。

与 DemucsSeparator/DubGenerator 同构:逐行 stdout 进度 + 末行 JSON。
"""
import json
import logging
import os
import subprocess


class OcrLocator:
    def __init__(self, python_bin: str, runner_path: str):
        self.python_bin = python_bin
        self.runner_path = runner_path
        self.logger = None
        self.task_id = None

    def _log(self, msg):
        if self.logger:
            self.logger.info(msg)
        else:
            logging.getLogger("ocr_locator").info(msg)

    def locate(self, *, video_path, output_json, task_id,
               sample_interval_sec=0.5, lang="ch", device="cpu",
               iou_threshold=0.5, progress_callback=None, timeout=3600):
        """返回 (True, {ok, boxes, segments, ...}) 或 (False, error_msg)。"""
        self.task_id = task_id
        if not self.python_bin or not os.path.isfile(self.python_bin):
            return False, f"asr-venv python 未安装或路径无效: {self.python_bin}"
        if not self.runner_path or not os.path.isfile(self.runner_path):
            return False, f"ocr runner 不存在: {self.runner_path}"
        if not video_path or not os.path.exists(video_path):
            return False, f"视频不存在: {video_path}"

        cmd = [
            self.python_bin, self.runner_path,
            "--video", video_path,
            "--output", output_json,
            "--task-id", str(task_id),
            "--sample-interval", str(sample_interval_sec if sample_interval_sec is not None else 0.5),
            "--lang", str(lang or "ch"),
            "--device", str(device or "cpu"),
            "--iou-threshold", str(iou_threshold if iou_threshold is not None else 0.5),
        ]
        self._log(f"调用 ocr runner: {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=os.environ.copy(),
            )
        except FileNotFoundError as e:
            return False, f"启动 runner 失败: {e}"

        last_json = None
        tail = []
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line:
                    tail.append(line)
                    if progress_callback:
                        try:
                            progress_callback(line)
                        except Exception:
                            pass
                    stripped = line.strip()
                    if stripped.startswith("{") and stripped.endswith("}"):
                        try:
                            last_json = json.loads(stripped)
                        except ValueError:
                            pass
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()
            return False, f"ocr 超时({timeout}s)"
        except Exception as e:
            proc.kill(); proc.wait()
            return False, f"runner 子进程异常: {e}"
        finally:
            if proc.stdout:
                proc.stdout.close()

        if proc.returncode != 0:
            summary = "\n".join(tail[-8:])
            return False, f"runner 退出码 {proc.returncode};末尾输出:\n{summary}"

        if isinstance(last_json, dict) and last_json.get("ok"):
            return True, last_json
        err = (last_json or {}).get("error") if isinstance(last_json, dict) else None
        return False, err or f"runner 未返回成功 JSON;末尾:\n{''.join(tail[-6:])}"
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/bin/python -m unittest tests.test_ocr_locator -v
```
Expected: OK (6 tests)

- [ ] **Step 5: Commit**

```bash
git add modules/ocr_locator.py tests/test_ocr_locator.py
git commit -m "feat(ocr): OcrLocator subprocess adapter + 单测"
```

---

## Task 5: task_manager 接线

**Files:**
- Modify: `modules/task_manager.py`
- Create: `tests/test_ocr_warning_persistence.py`

- [ ] **Step 1: TASK_STATES + PROCESSING_STATES**

在 `DUBBING` 行附近（或 `AUDIO_SEPARATING` 后）加：
```python
    'OCR_LOCATING': 'ocr_locating',          # 字幕区域 OCR 定位中
```

在 `PROCESSING_STATES` 中 `AUDIO_SEPARATING` 后加：
```python
    TASK_STATES['OCR_LOCATING'],
```

- [ ] **Step 2: pipeline stage + ORDER**

在：
```python
PIPELINE_STAGE_REMASTER_DEMUCS = 'remaster_demucs'
PIPELINE_STAGE_TRANSLATE_SUBTITLE = 'translate_subtitle'
```
之间插入：
```python
PIPELINE_STAGE_REMASTER_OCR = 'remaster_ocr'
```

在 `PIPELINE_STAGE_ORDER` 中 demucs 与 translate_subtitle 之间插入：
```python
    PIPELINE_STAGE_REMASTER_OCR,
```

- [ ] **Step 3: DB 三件套 `ocr_warning_message`**

Schema：在 `dub_warning_message` 行加逗号并追加：
```python
        dub_warning_message TEXT,  -- 配音阶段的非致命警告，不影响上传流程
        ocr_warning_message TEXT  -- OCR 字幕定位阶段的非致命警告，不影响上传流程
```

迁移：在 `dub_warning_message` 迁移块后、ASR 数据迁移注释前追加：
```python
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'ocr_warning_message' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN ocr_warning_message TEXT")
            logger.info("数据库升级：添加ocr_warning_message字段")
            conn.commit()
```

ALLOWED_COLUMNS：在 dub 项后加：
```python
        'ocr_warning_message': 'ocr_warning_message = ?',
```

- [ ] **Step 4: warning 持久化测试**

Create `tests/test_ocr_warning_persistence.py`:
```python
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import modules.task_manager as tm


class TestOcrWarningPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_db_path = tm.DB_PATH
        tm.DB_PATH = os.path.join(self.tmpdir, "tasks.db")
        tm.init_db()

    def tearDown(self):
        tm.DB_PATH = self._orig_db_path
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ocr_warning_message_round_trip(self):
        task_id = tm.add_task("https://youtu.be/ocr_test")
        tm.update_task(task_id, ocr_warning_message="ocr: paddle failed")
        task = tm.get_task(task_id)
        self.assertEqual(task["ocr_warning_message"], "ocr: paddle failed")

    def test_ocr_warning_message_cleared_on_success(self):
        task_id = tm.add_task("https://youtu.be/ocr_test2")
        tm.update_task(task_id, ocr_warning_message="ocr: temp")
        tm.update_task(task_id, ocr_warning_message=None)
        task = tm.get_task(task_id)
        self.assertIsNone(task["ocr_warning_message"])


if __name__ == "__main__":
    unittest.main()
```

跑：
```bash
.venv/bin/python -m unittest tests.test_ocr_warning_persistence -v
```
Expected: 2 OK（三件套落地后）

- [ ] **Step 5: `_run_remaster_ocr`**

紧接 `_run_remaster_demucs` 之后插入（在 `_run_remaster_dub` / `_translate_subtitle` 之前均可，方法顺序不限）：

```python
    def _run_remaster_ocr(self, task_id, task_logger):
        """AI 重制管线:PaddleOCR 字幕区域定位,产出时间线 bbox JSON。

        只产中间产物落盘,不写任何下游业务字段;供未来 FFmpeg 擦除消费。
        软失败:失败不阻断上传。
        """
        from modules.ocr_locator import OcrLocator

        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return False
        video_path = task.get("video_path_local", "")
        if not video_path or not os.path.exists(video_path):
            task_logger.warning("视频文件缺失,跳过 OCR 定位")
            return False

        python_bin = str(self.config.get("OCR_PYTHON", "") or "").strip()
        runner_path = str(self.config.get("OCR_RUNNER", "modules/ocr_runner.py") or "").strip()
        if not runner_path or not os.path.isabs(runner_path):
            runner_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                runner_path,
            )

        task_dir = os.path.join(DOWNLOADS_DIR, task_id)
        os.makedirs(task_dir, exist_ok=True)
        out_json = os.path.join(task_dir, f"ocr_subtitle_boxes_{task_id}.json")

        try:
            sample_interval = float(self.config.get("OCR_SAMPLE_INTERVAL_SEC", 0.5) or 0.5)
            if sample_interval <= 0:
                sample_interval = 0.5
        except (TypeError, ValueError):
            sample_interval = 0.5
        try:
            iou_threshold = float(self.config.get("OCR_IOU_THRESHOLD", 0.5) or 0.5)
        except (TypeError, ValueError):
            iou_threshold = 0.5
        lang = str(self.config.get("OCR_LANG", "ch") or "ch")
        device = str(self.config.get("OCR_DEVICE", "cpu") or "cpu")
        timeout = _as_int(self.config.get("OCR_TIMEOUT_SECONDS", 3600), 3600, minimum=60)

        prev_status = task.get("status")
        update_task(task_id, status=TASK_STATES["OCR_LOCATING"])
        task_logger.info(
            f"重制管线:调用 OCR 定位(lang={lang}/interval={sample_interval}s),输出 {out_json}"
        )

        loc = OcrLocator(python_bin=python_bin, runner_path=runner_path)
        loc.logger = task_logger
        ok, res = loc.locate(
            video_path=video_path,
            output_json=out_json,
            task_id=task_id,
            sample_interval_sec=sample_interval,
            lang=lang,
            device=device,
            iou_threshold=iou_threshold,
            progress_callback=lambda t: task_logger.info(f"[ocr] {t}"),
            timeout=timeout,
        )
        if ok:
            update_task(task_id, ocr_warning_message=None, status=prev_status)
            task_logger.info(f"OCR 定位完成: {res}")
            return True
        task_logger.error(f"OCR 定位失败: {res}")
        update_task(task_id, ocr_warning_message=f"ocr: {res}", status=prev_status)
        return False
```

- [ ] **Step 6: process_task 门控（同一 remaster 块）**

当前块在 demucs 后结束于：
```python
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_REMASTER_DEMUCS)

            # 5. 字幕处理（翻译或烧录启用时）
```

改为：
```python
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_REMASTER_DEMUCS)
                try:
                    self._run_remaster_ocr(task_id, task_logger)
                except Exception as e:
                    task_logger.error(f"重制 OCR 异常: {e}")
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_REMASTER_OCR)

            # 5. 字幕处理（翻译或烧录启用时）
```

- [ ] **Step 7: 断言 + 全测试**

```bash
cd /Users/mac/Y2A-Auto && .venv/bin/python -c "
import modules.task_manager as tm
assert tm.TASK_STATES['OCR_LOCATING'] == 'ocr_locating'
assert tm.PIPELINE_STAGE_REMASTER_OCR == 'remaster_ocr'
order = tm.PIPELINE_STAGE_ORDER
assert order.index(tm.PIPELINE_STAGE_REMASTER_DEMUCS) < order.index(tm.PIPELINE_STAGE_REMASTER_OCR)
assert order.index(tm.PIPELINE_STAGE_REMASTER_OCR) < order.index(tm.PIPELINE_STAGE_TRANSLATE_SUBTITLE)
assert tm.TASK_STATES['OCR_LOCATING'] in tm.PROCESSING_STATES
assert hasattr(tm.TaskProcessor, '_run_remaster_ocr')
print('OK')
"
.venv/bin/python -m unittest tests.test_ocr_warning_persistence tests.test_ocr_config tests.test_ocr_cluster tests.test_ocr_locator -v
.venv/bin/python -m unittest discover tests -v 2>&1 | tail -6
```
Expected: OK；OCR 相关测试全绿；全套绿。

- [ ] **Step 8: Commit**

```bash
git add modules/task_manager.py tests/test_ocr_warning_persistence.py
git commit -m "feat(ocr): task_manager remaster_ocr stage + ocr_warning 三件套 + 软失败接线"
```

---

## Task 6: 真实冒烟 + 复审 + 合并

**Files:** 无（验证 + 收尾）

- [ ] **Step 1: 准备含文字的短视频（可合成）**

```bash
# 生成 3s 视频 + 底部烧录白字（drawtext 需 libfreetype；若失败改用任一带字幕样片）
ffmpeg -y -f lavfi -i color=c=black:s=640x360:d=3 \
  -vf "drawtext=text='Sample Subtitle OCR':fontsize=28:fontcolor=white:x=(w-text_w)/2:y=h-60" \
  -c:v libx264 -pix_fmt yuv420p /tmp/ocr_smoke.mp4
```

若 `drawtext` 不可用：用项目 `downloads/` 里已有带字幕视频。

- [ ] **Step 2: 跑 runner**

```bash
cd /Users/mac/Y2A-Auto
mkdir -p /tmp/ocr_out
/Users/mac/asr-venv/bin/python modules/ocr_runner.py \
  --video /tmp/ocr_smoke.mp4 \
  --output /tmp/ocr_out/ocr_subtitle_boxes_smoke.json \
  --task-id smoke \
  --sample-interval 0.5 \
  --lang ch \
  --device cpu
echo "exit=$?"
cat /tmp/ocr_out/ocr_subtitle_boxes_smoke.json | head -40
```
Expected: exit 0；JSON 含 `segments`（有字时 ideally >0）；`box` 各分量在 [0,1]。

- [ ] **Step 3: 清理**

```bash
rm -rf /tmp/ocr_smoke.mp4 /tmp/ocr_out
```

- [ ] **Step 4: 全分支复审（opus）**

重点：
- 软失败 / 不写下游字段
- `ocr_warning_message` 三件套
- stage 在 demucs 与 translate 之间
- REMASTER 关时不调用
- 聚类与 runner 坐标约定一致
- 临时帧 finally 清理

修完后重跑全测试。

- [ ] **Step 5: squash 合并 main**

```bash
git checkout main
git merge --squash feat/paddleocr-subtitle-locate   # 或当前功能分支名
git commit -m "feat: AI 重制管线第五块 - PaddleOCR 字幕定位(时间线 bbox JSON)"
```

---

## Self-Review（写计划时）

**1. Spec coverage**
- §1 边界/软失败 → Task 5 ✓
- §2 决策（抽帧/全画面/时间线 bbox/asr-venv）→ Task 0/3 ✓
- §3 架构与 stage 顺序 → Task 5 ORDER + 门控 ✓
- §4 JSON 契约 → Task 3 payload ✓
- §5 runner 流程 → Task 3 ✓
- §6 聚类 → Task 2 ✓
- §7 adapter → Task 4 ✓
- §8 task_manager → Task 5 ✓
- §9 config → Task 1 ✓
- §10 测试 → Task 1/2/4/5 ✓
- §11 环境+冒烟 → Task 0/6 ✓

**2. Placeholder scan:** 无 TBD；代码完整 ✓

**3. Type consistency**
- `OcrLocator.locate(...)` Task 4 ↔ Task 5 ✓
- runner CLI ↔ adapter cmd ✓
- 产物 `ocr_subtitle_boxes_<task_id>.json` 全程一致 ✓
- `cluster_detections` 字段 `start/end/box/score` Task 2↔3 ✓
- `OCR_*` 键 Task 1↔5 ✓
- 状态 `OCR_LOCATING` / stage `remaster_ocr` / `ocr_warning_message` 一致 ✓
