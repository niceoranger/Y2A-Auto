#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OCR 字幕定位 runner。用 /Users/mac/asr-venv/bin/python 跑。

RapidOCR(onnxruntime)推理,自带 PP-OCRv4 模型、离线可用;
替换原 PaddleOCR 方案(paddle 3.3.1 线程池在本机存在无法规避的段错误)。

契约:
  CLI: --video <p> --output <json> --task-id <id>
       [--sample-interval 0.5] [--lang ch] [--device cpu] [--iou-threshold 0.5]
       [--min-rec-score 0.6]
  stdout: 进度行 + 末行 JSON {ok, boxes|error}
  退出码: 0 成功 / 1 运行失败 / 2 参数/依赖错

误检防护:识别文本为空/过短或置信度低于阈值的检测在聚类前被丢弃
(衣服上的胸针/麦克风等高频误检源识别不出有效文本)。
"""
import argparse
import json
import os
import re
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


# 文字字符(拉丁/数字/中日韩),用于判定检测框里是否有"真文字"
_WORD_CHAR_RE = re.compile(
    r"[0-9A-Za-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]"
)


def count_word_chars(text) -> int:
    """识别文本中文字字符数(忽略空白与标点)。"""
    return len(_WORD_CHAR_RE.findall(str(text or "")))


def is_valid_text_det(text, min_word_chars: int = 2) -> bool:
    """True=检测框内是有效文字。空/单字符视为误检(衣物、麦克风等图形误检)。"""
    return count_word_chars(text) >= max(1, int(min_word_chars))


def filter_valid_text_dets(dets: list, min_score: float = 0.6) -> list:
    """聚类前的误检过滤:识别文本无效或置信度低于阈值的检测全部丢弃。"""
    threshold = max(0.0, float(min_score or 0.0))
    return [
        d for d in (dets or [])
        if is_valid_text_det(d.get("text"))
        and float(d.get("score") or 0.0) >= threshold
    ]



def _init_ocr(lang: str, device: str):
    """初始化 RapidOCR 实例。失败抛 RuntimeError。"""
    from rapidocr_onnxruntime import RapidOCR

    try:
        ocr = RapidOCR()
    except Exception as e:
        raise RuntimeError(f"RapidOCR 初始化失败: {e}") from None
    print(f"[ocr] RapidOCR init ok (lang={lang} device={device})", flush=True)
    return ocr


# 多进程 worker 的进程内 OCR 实例(spawn 子进程里由 initializer 填充)
_POOL_OCR = {}


def _pool_init(lang: str, device: str):
    try:
        _POOL_OCR["ocr"] = _init_ocr(lang, device)
    except Exception as e:
        print(f"[ocr] worker 初始化失败: {e}", flush=True)
        _POOL_OCR["ocr"] = None


def _pool_work(job):
    """job=(t, path) → (t, [(box_pts, score, text), ...] | None, err|None)。"""
    t, path = job
    ocr = _POOL_OCR.get("ocr")
    if ocr is None:
        return (t, None, "worker 无 OCR 实例")
    try:
        return (t, _ocr_predict_pages(ocr, path), None)
    except Exception as e:
        return (t, None, str(e))


def run_ocr_on_frames(frames, width, height, lang: str, device: str, workers: int = 1) -> list:
    """返回 dets: [{t, box, score}, ...]。全画面。兼容 PaddleOCR 2.x/3.x。

    workers>1 且帧数足够时用多进程并行(spawn,每 worker 独立加载模型),
    失败自动回退单进程顺序处理。
    """
    print(f"[ocr] 加载 PaddleOCR lang={lang} device={device} workers={workers}", flush=True)

    def _collect(t, page_items):
        dets = []
        for box_pts, score, text in page_items or []:
            norm = pixel_box_to_norm(box_pts, width, height)
            if not filter_box(norm):
                continue
            dets.append({
                "t": float(t), "box": norm,
                "score": float(score),
                "text": str(text or "").strip(),
            })
        return dets

    # 多进程并行:帧数太少时初始化开销(每 worker 加载模型)不划算
    if workers and workers > 1 and len(frames) >= 24:
        try:
            import multiprocessing as mp
            ctx = mp.get_context("spawn")
            jobs = [(t, path) for t, path in frames]
            dets = []
            done = 0
            with ctx.Pool(
                processes=min(int(workers), len(jobs)),
                initializer=_pool_init,
                initargs=(lang, device),
            ) as pool:
                for t, items, err in pool.imap_unordered(_pool_work, jobs, chunksize=1):
                    done += 1
                    if done % 20 == 0:
                        print(f"[ocr] 已完成 {done}/{len(jobs)} 帧(并行)", flush=True)
                    if err is not None:
                        print(f"[ocr] 帧失败 t={t}: {err}", flush=True)
                        continue
                    dets.extend(_collect(t, items))
            print(f"[ocr] 并行 OCR 完成,workers={workers}", flush=True)
            return dets
        except Exception as e:
            print(f"[ocr] 并行 OCR 失败,回退单进程: {e}", flush=True)

    ocr = _init_ocr(lang, device)
    dets = []
    for i, (t, path) in enumerate(frames):
        if i % 20 == 0:
            print(f"[ocr] 帧 {i+1}/{len(frames)} t={t:.2f}", flush=True)
        try:
            page_items = _ocr_predict_pages(ocr, path)
        except Exception as e:
            print(f"[ocr] 帧失败 t={t}: {e}", flush=True)
            continue
        dets.extend(_collect(t, page_items))
    return dets


def _ocr_predict_pages(ocr, path: str) -> list:
    """RapidOCR 输出 → [(box_pts, score, text), ...]。box 为四点坐标。"""
    result, _ = ocr(path)
    items = []
    for det in result or []:
        box_pts, text, score = det[0], det[1], det[2]
        items.append((box_pts, float(score or 0.0), str(text or "")))
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--sample-interval", type=float, default=2.0)
    ap.add_argument("--lang", default="ch")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--workers", type=int, default=1,
                    help="OCR 并行进程数;>1 且帧数足够时启用多进程")
    ap.add_argument("--iou-threshold", type=float, default=0.5)
    ap.add_argument("--min-rec-score", type=float, default=0.6,
                    help="识别置信度阈值,低于此值或文本无效的检测按误检丢弃")
    ap.add_argument("--bottom-band-min-y", type=float, default=0.6,
                    help="只保留 y>=此值的检测(底栏优先);0=全画面")
    args = ap.parse_args()

    # onnxruntime 遥测后台线程在本机代理环境下会 abort(须在 import 前设置)
    os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

    if not os.path.isfile(args.video):
        _emit({"ok": False, "error": f"视频不存在: {args.video}"})
        return 2
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        _emit({"ok": False, "error": "ffmpeg/ffprobe 未安装或不在 PATH"})
        return 2
    try:
        from rapidocr_onnxruntime import RapidOCR  # noqa: F401
    except Exception as e:
        _emit({"ok": False, "error": f"import rapidocr_onnxruntime 失败(检查 asr-venv): {e}"})
        return 2

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from modules.ocr_cluster import cluster_detections, filter_bottom_band

    interval = float(args.sample_interval or 2.0)
    if interval <= 0:
        interval = 2.0
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
            workers=max(1, int(args.workers or 1)),
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

        # 误检校验:识别不出有效文本(胸针/麦克风等图形)或置信度过低的检测,
        # 聚类前丢弃,否则会变成 delogo 马赛克块
        try:
            min_rec_score = float(args.min_rec_score)
        except (TypeError, ValueError):
            min_rec_score = 0.6
        if min_rec_score < 0:
            min_rec_score = 0.0
        before_validate = len(dets)
        dets = filter_valid_text_dets(dets, min_score=min_rec_score)
        print(
            f"[ocr] 文本/置信度校验(阈值 {min_rec_score}): "
            f"{before_validate} → {len(dets)}",
            flush=True,
        )

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
    code = main()
    # 结果已输出、产物已落盘;跳过解释器 teardown,规避原生库退出阶段问题
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code if isinstance(code, int) else 0)
