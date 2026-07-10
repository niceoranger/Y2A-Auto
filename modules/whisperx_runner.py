#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WhisperX ASR + 字级对齐 runner。用 /Users/mac/asr-venv/bin/python 跑。

契约:
  CLI: --video <p> --output <srt> [--language auto] [--model large-v3]
       [--device cpu] [--compute-type int8] [--batch-size 16]
  stdout: 进度文本行 + 末行 JSON {ok, srt|error}
  退出码: 0 成功 / 非0 失败
"""
import argparse
import json
import os
import subprocess


def _format_ts(seconds: float) -> str:
    if seconds is None or seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    h = total_ms // 3600000
    m = (total_ms % 3600000) // 60000
    s = (total_ms % 60000) // 1000
    ms = total_ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def render_srt(segments) -> str:
    """WhisperX segments(list[dict] with start/end/text) -> SRT 文本。"""
    lines = []
    idx = 0
    for seg in (segments or []):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        idx += 1
        start = float(seg.get("start", 0.0) or 0.0)
        end = float(seg.get("end", start) or start)
        if end < start:
            end = start
        lines.append(str(idx))
        lines.append(f"{_format_ts(start)} --> {_format_ts(end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def extract_audio_wav(video_path: str) -> str:
    """ffmpeg 提取 16kHz mono wav(WhisperX 期望)。"""
    out = os.path.abspath(video_path) + ".asr16k.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1",
         "-ar", "16000", "-f", "wav", out],
        check=True, capture_output=True,
    )
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--language", default="auto")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--compute-type", default="int8")
    ap.add_argument("--batch-size", type=int, default=16)
    args = ap.parse_args()

    def _emit(obj):
        print(json.dumps(obj, ensure_ascii=False), flush=True)

    if not os.path.isfile(args.video):
        _emit({"ok": False, "error": f"视频不存在: {args.video}"})
        return 2

    try:
        import whisperx
    except Exception as e:
        _emit({"ok": False, "error": f"import whisperx 失败(检查 asr-venv): {e}"})
        return 2

    lang_arg = None if args.language == "auto" else args.language
    try:
        print(f"[whisperx] 加载模型 {args.model} ({args.device}/{args.compute_type})", flush=True)
        model = whisperx.load_model(args.model, device=args.device, compute_type=args.compute_type)
        print("[whisperx] 提取音频(16kHz mono wav)", flush=True)
        wav = extract_audio_wav(args.video)
        audio = whisperx.load_audio(wav)
        print("[whisperx] 转写(段级)", flush=True)
        result = model.transcribe(audio, batch_size=args.batch_size, language=lang_arg)
        segs = result.get("segments", []) or []
        lang = result.get("language") or args.language or "en"
        try:
            print(f"[whisperx] 对齐(字级, lang={lang})", flush=True)
            model_a, metadata = whisperx.load_align_model(language_code=lang, device=args.device)
            aligned = whisperx.align(segs, model_a, metadata, audio, device=args.device, return_char_alignments=False)
            segs = aligned.get("segments", segs) or segs
        except Exception as ae:
            print(f"[whisperx] 对齐失败,回退段级: {ae}", flush=True)
        srt_text = render_srt(segs)
        out_dir = os.path.dirname(os.path.abspath(args.output))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(srt_text)
        _emit({"ok": True, "srt": args.output, "segments": len(segs)})
        return 0
    except Exception as e:
        import traceback
        _emit({"ok": False, "error": str(e), "trace": traceback.format_exc()[-600:]})
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
