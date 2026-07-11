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
    """返回 dets: [{t, box, score}, ...]。全画面。兼容 PaddleOCR 2.x/3.x。"""
    from paddleocr import PaddleOCR

    use_gpu = str(device).lower() in ("gpu", "cuda", "true", "1")
    device_arg = "gpu" if use_gpu else "cpu"
    print(f"[ocr] 加载 PaddleOCR lang={lang} device={device_arg}", flush=True)

    ocr = None
    # 3.x: device= ; 2.x: use_gpu= / show_log= 等
    init_attempts = [
        {"lang": lang, "device": device_arg},
        {"lang": lang, "device": device_arg, "use_textline_orientation": True},
        {"lang": lang, "use_gpu": use_gpu},
        {"lang": lang, "use_angle_cls": True, "use_gpu": use_gpu},
        {"lang": lang},
    ]
    last_err = None
    for kwargs in init_attempts:
        try:
            ocr = PaddleOCR(**kwargs)
            print(f"[ocr] PaddleOCR init ok kwargs={list(kwargs.keys())}", flush=True)
            break
        except Exception as e:
            last_err = e
            continue
    if ocr is None:
        raise RuntimeError(f"PaddleOCR 初始化失败: {last_err}")

    dets = []
    for i, (t, path) in enumerate(frames):
        if i % 20 == 0:
            print(f"[ocr] 帧 {i+1}/{len(frames)} t={t:.2f}", flush=True)
        try:
            page_items = _ocr_predict_pages(ocr, path)
        except Exception as e:
            print(f"[ocr] 帧失败 t={t}: {e}", flush=True)
            continue
        for box_pts, score in page_items:
            norm = pixel_box_to_norm(box_pts, width, height)
            if not filter_box(norm):
                continue
            dets.append({"t": float(t), "box": norm, "score": float(score)})
    return dets


def _ocr_predict_pages(ocr, path: str) -> list:
    """统一 2.x/3.x 输出为 [(box_pts, score), ...]。

    3.x predict()/ocr(): list[OCRResult|dict] with dt_polys/rec_polys + rec_scores
    2.x ocr(): list[list[[box,(text,score)], ...]] per image
    """
    result = None
    if hasattr(ocr, "predict"):
        try:
            result = ocr.predict(path)
        except Exception:
            result = None
    if result is None:
        try:
            result = ocr.ocr(path, cls=True)
        except TypeError:
            result = ocr.ocr(path)

    if not result:
        return []

    items = []
    first = result[0] if isinstance(result, list) and result else result
    if _is_ocr_result_page(first):
        pages = result if isinstance(result, list) else [result]
        for page in pages:
            items.extend(_parse_ocr_result_page(page))
        return items

    # 2.x style
    lines = first if isinstance(first, list) else result
    if not lines:
        return []
    for item in lines:
        if not item or len(item) < 2:
            continue
        box_pts, meta = item[0], item[1]
        score = float(meta[1]) if meta and len(meta) > 1 else 0.0
        items.append((box_pts, score))
    return items


def _is_ocr_result_page(obj) -> bool:
    if obj is None:
        return False
    if isinstance(obj, dict):
        return "dt_polys" in obj or "rec_polys" in obj or "rec_scores" in obj
    try:
        return hasattr(obj, "keys") and (
            "dt_polys" in obj or "rec_polys" in obj or hasattr(obj, "get")
        )
    except Exception:
        return False


def _parse_ocr_result_page(page) -> list:
    def _get(key, default=None):
        if isinstance(page, dict):
            return page.get(key, default)
        try:
            return page[key]
        except Exception:
            return getattr(page, key, default)

    polys = _get("rec_polys") or _get("dt_polys") or []
    scores = _get("rec_scores") or []
    out = []
    for i, poly in enumerate(polys):
        try:
            pts = poly.tolist() if hasattr(poly, "tolist") else list(poly)
        except Exception:
            pts = poly
        score = float(scores[i]) if i < len(scores) else 0.0
        out.append((pts, score))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--sample-interval", type=float, default=0.5)
    ap.add_argument("--lang", default="ch")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--iou-threshold", type=float, default=0.5)
    ap.add_argument("--bottom-band-min-y", type=float, default=0.6,
                    help="只保留 y>=此值的检测(底栏优先);0=全画面")
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
    from modules.ocr_cluster import cluster_detections, filter_bottom_band

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

        try:
            bottom_min_y = float(args.bottom_band_min_y)
        except (TypeError, ValueError, AttributeError):
            bottom_min_y = 0.6
        if bottom_min_y < 0:
            bottom_min_y = 0.0
        filtered = filter_bottom_band(dets, min_y_ratio=bottom_min_y)
        if bottom_min_y > 0:
            print(
                f"[ocr] 底栏过滤 min_y={bottom_min_y}: {len(dets)} → {len(filtered)}",
                flush=True,
            )
            # 若底栏过滤后全空,回退全画面(避免无字幕视频以外的误杀)
            if filtered:
                dets = filtered
            else:
                print("[ocr] 底栏过滤结果为空,回退全画面检测", flush=True)

        max_gap = 1.5 * interval
        segments = cluster_detections(
            dets,
            iou_threshold=float(args.iou_threshold or 0.5),
            max_gap_sec=max_gap,
            sample_interval=interval,
        )
        payload = {
            "video": os.path.abspath(args.video),
            "width": meta["width"],
            "height": meta["height"],
            "duration_sec": meta["duration_sec"],
            "sample_interval_sec": interval,
            "roi": f"bottom_y>={bottom_min_y}" if bottom_min_y > 0 else "full",
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
