#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Demucs 音轨分离 runner。用 /Users/mac/asr-venv/bin/python 跑。

契约:
  CLI: --video <p> --output-dir <dir> --task-id <id>
       [--model htdemucs_ft] [--device mps] [--stems 2]
  stdout: 进度文本行 + 末行 JSON {ok, vocals|no_vocals|error}
  退出码: 0 成功 / 1 运行失败 / 2 参数错
"""
import argparse
import json
import os
import shutil
import subprocess
import sys


def extract_audio_wav(video_path: str) -> str:
    """ffmpeg 提取 44.1kHz 立体声 wav。失败时把 ffmpeg stderr 带进异常便于排查。"""
    out = os.path.abspath(video_path) + ".demucs441.wav"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "2",
             "-ar", "44100", "-f", "wav", out],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode(errors="ignore")[-400:]
        raise RuntimeError(f"ffmpeg 提取音频失败: {detail}") from None
    return out


def run_demucs(wav_path: str, workdir: str, model: str, device: str) -> None:
    """调 python -m demucs 做 2-stem 分离。失败把 stderr 带进异常。"""
    cmd = [sys.executable, "-m", "demucs", "--two-stems=vocals",
           "-n", model, "-d", device, "--out", workdir, wav_path]
    print(f"[demucs] 分离({model}/{device}): {' '.join(cmd)}", flush=True)
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode(errors="ignore")[-500:]
        raise RuntimeError(f"demucs 分离失败({device}): {detail}") from None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--model", default="htdemucs_ft")
    ap.add_argument("--device", default="mps")
    ap.add_argument("--stems", type=int, default=2)
    args = ap.parse_args()

    def _emit(obj):
        print(json.dumps(obj, ensure_ascii=False), flush=True)

    if not os.path.isfile(args.video):
        _emit({"ok": False, "error": f"视频不存在: {args.video}"})
        return 2

    try:
        import demucs  # noqa: F401
    except Exception as e:
        _emit({"ok": False, "error": f"import demucs 失败(检查 asr-venv): {e}"})
        return 2

    wav = None
    workdir = os.path.join(args.output_dir, f".demucs_work_{args.task_id}")
    try:
        os.makedirs(args.output_dir, exist_ok=True)
        print("[demucs] 提取音频(44.1kHz 立体声 wav)", flush=True)
        wav = extract_audio_wav(args.video)

        device_used = args.device
        try:
            run_demucs(wav, workdir, args.model, args.device)
        except RuntimeError as e:
            # MPS 偶发算子不支持 → 自动降级 CPU 重跑一次
            if args.device != "cpu":
                print(f"[demucs] {args.device} 失败,降级 CPU 重跑: {e}", flush=True)
                device_used = "cpu"
                run_demucs(wav, workdir, args.model, "cpu")
            else:
                raise

        # demucs 输出到 <workdir>/<model>/<wav_basename>/{vocals,no_vocals}.wav
        wav_base = os.path.splitext(os.path.basename(wav))[0]
        sep_dir = os.path.join(workdir, args.model, wav_base)
        src_vocals = os.path.join(sep_dir, "vocals.wav")
        src_no_vocals = os.path.join(sep_dir, "no_vocals.wav")
        if not (os.path.isfile(src_vocals) and os.path.isfile(src_no_vocals)):
            raise RuntimeError(f"分离产物缺失: 期望 {src_vocals} / {src_no_vocals}")

        dst_vocals = os.path.join(args.output_dir, f"demucs_vocals_{args.task_id}.wav")
        dst_no_vocals = os.path.join(args.output_dir, f"demucs_no_vocals_{args.task_id}.wav")
        shutil.move(src_vocals, dst_vocals)
        shutil.move(src_no_vocals, dst_no_vocals)

        _emit({"ok": True, "vocals": dst_vocals, "no_vocals": dst_no_vocals,
               "device_used": device_used})
        return 0
    except Exception as e:
        import traceback
        _emit({"ok": False, "error": str(e), "trace": traceback.format_exc()[-600:]})
        return 1
    finally:
        if wav and os.path.isfile(wav):
            try:
                os.remove(wav)
            except OSError:
                pass
        if os.path.isdir(workdir):
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    import sys
    sys.exit(main())
