#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""XTTSv2 + RubberBand 配音 runner。用 /Users/mac/asr-venv/bin/python 跑。

契约:
  CLI: --translated-srt <p> --no-vocals <wav> --output <wav> --task-id <id>
       [--speaker Ana Florence] [--language zh] [--device cpu] [--max-tempo 1.5]
       [--model tts_models/multilingual/multi-dataset/xtts_v2]
  stdout: 进度行 + 末行 JSON {ok, dubbed|error}
  退出码: 0 成功 / 1 运行失败 / 2 参数/依赖错
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import wave


def _emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _which(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def parse_srt(path: str):
    """极简 SRT 解析 → list[(start_sec, end_sec, text)]。"""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    blocks = re.split(r"\n\s*\n", content.strip())
    segs = []
    ts_re = re.compile(
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
    )

    def to_sec(h, m, s, ms):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        # 第一行可能是序号
        idx = 0
        if lines[0].isdigit():
            idx = 1
        if idx >= len(lines):
            continue
        m = ts_re.search(lines[idx])
        if not m:
            continue
        start = to_sec(*m.groups()[:4])
        end = to_sec(*m.groups()[4:])
        text = " ".join(lines[idx + 1 :]).strip()
        if text and end > start:
            segs.append((start, end, text))
    return segs


def wav_duration_sec(path: str) -> float:
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def rubberband_stretch(in_wav: str, out_wav: str, time_ratio: float) -> None:
    # -t: 输出时长 = 输入 * time_ratio; -c 5: crispness 默认档(语音清晰)
    # 注意: -c 是 crispness 不是 formant; formant 旗标是 -F(变调时才有意义)
    cmd = ["rubberband", "-t", str(time_ratio), "-c", "5", in_wav, out_wav]
    print(f"[dub] rubberband: {' '.join(cmd)}", flush=True)
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode(errors="ignore")[-400:]
        raise RuntimeError(f"rubberband 失败: {detail}") from None


def normalize_wav_44k_stereo(in_wav: str, out_wav: str) -> None:
    """统一到 44.1kHz 立体声,避免 amix 采样率/声道不一致。"""
    cmd = [
        "ffmpeg", "-y", "-i", in_wav,
        "-ar", "44100", "-ac", "2", out_wav,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def pad_or_truncate_wav(in_wav: str, out_wav: str, target_sec: float, mode: str) -> None:
    """mode=pad: 右侧静音 pad 到 target; mode=truncate: 硬截到 target。用 ffmpeg。"""
    if mode == "pad":
        # apad + atrim
        filt = f"apad,atrim=0:{target_sec}"
    else:
        filt = f"atrim=0:{target_sec}"
    cmd = [
        "ffmpeg", "-y", "-i", in_wav, "-af", filt,
        "-ar", "44100", "-ac", "2", out_wav,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def mix_onto_background(bg_wav: str, clips, output_wav: str) -> None:
    """clips: list[(start_sec, clip_wav_path)]。用 ffmpeg adelay+amix。"""
    if not clips:
        shutil.copy2(bg_wav, output_wav)
        return
    # 构造 filter_complex: [0]背景; 各 clip adelay 后 amix
    inputs = ["-i", bg_wav]
    filter_parts = []
    labels = ["[0:a]"]
    for i, (start, path) in enumerate(clips):
        inputs += ["-i", path]
        delay_ms = max(0, int(round(start * 1000)))
        # adelay 对每个声道都要设; aformat 兜底采样率/声道
        filter_parts.append(
            f"[{i+1}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
            f"adelay={delay_ms}|{delay_ms},apad[a{i}]"
        )
        labels.append(f"[a{i}]")
    n = 1 + len(clips)
    filter_parts.insert(
        0, "[0:a]aformat=sample_rates=44100:channel_layouts=stereo[bg]"
    )
    labels[0] = "[bg]"
    filter_parts.append(
        f"{''.join(labels)}amix=inputs={n}:duration=first:dropout_transition=0:normalize=0[out]"
    )
    fc = ";".join(filter_parts)
    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", fc, "-map", "[out]",
           "-ar", "44100", "-ac", "2", output_wav]
    print(f"[dub] mix {len(clips)} clips onto background", flush=True)
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode(errors="ignore")[-500:]
        raise RuntimeError(f"ffmpeg 混音失败: {detail}") from None


def _load_xtts(model_name: str, device: str):
    """优先本地权重目录,避免 TTS(model_name) 按 hash 重下 1.8G。强制 CPU。"""
    from TTS.api import TTS
    os.environ.setdefault("COQUI_TOS_AGREED", "1")
    local_dir = os.path.expanduser(
        "~/.local/share/tts/tts_models--multilingual--multi-dataset--xtts_v2"
    )
    local_pth = os.path.join(local_dir, "model.pth")
    local_cfg = os.path.join(local_dir, "config.json")
    if os.path.isfile(local_pth) and os.path.isfile(local_cfg):
        print(f"[dub] 使用本地 XTTS 权重: {local_dir}", flush=True)
        tts = TTS(model_path=local_dir, config_path=local_cfg)
    else:
        print(f"[dub] 本地权重缺失,按名加载(可能触发下载): {model_name}", flush=True)
        tts = TTS(model_name)
    # XTTS 自定义算子不支持 MPS;强制 CPU(忽略非 cpu 请求)
    try:
        tts.to("cpu")
    except Exception as e:
        print(f"[dub] tts.to('cpu') 跳过: {e}", flush=True)
    return tts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--translated-srt", required=True)
    ap.add_argument("--no-vocals", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--speaker", default="Ana Florence")
    ap.add_argument("--language", default="zh")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--max-tempo", type=float, default=1.5)
    ap.add_argument(
        "--model",
        default="tts_models/multilingual/multi-dataset/xtts_v2",
    )
    args = ap.parse_args()

    if not os.path.isfile(args.translated_srt):
        _emit({"ok": False, "error": f"字幕不存在: {args.translated_srt}"})
        return 2
    if not os.path.isfile(args.no_vocals):
        _emit({"ok": False, "error": f"背景音不存在: {args.no_vocals}"})
        return 2
    if not _which("rubberband"):
        _emit({"ok": False, "error": "rubberband 未安装或不在 PATH(brew install rubberband)"})
        return 2
    if not _which("ffmpeg"):
        _emit({"ok": False, "error": "ffmpeg 未安装或不在 PATH"})
        return 2

    try:
        from TTS.api import TTS  # noqa: F401
    except Exception as e:
        _emit({"ok": False, "error": f"import TTS 失败(检查 asr-venv/coqui-tts): {e}"})
        return 2

    # 延迟 import 纯逻辑,兼容 asr-venv 下以脚本方式运行
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from modules.dub_timing import fit_segment_audio

    workdir = os.path.join(os.path.dirname(os.path.abspath(args.output)),
                           f".dub_work_{args.task_id}")
    try:
        if os.path.isdir(workdir):
            shutil.rmtree(workdir, ignore_errors=True)
        os.makedirs(workdir, exist_ok=True)
        out_dir = os.path.dirname(os.path.abspath(args.output))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        segs = parse_srt(args.translated_srt)
        print(f"[dub] 解析到 {len(segs)} 段字幕", flush=True)
        if not segs:
            _emit({"ok": False, "error": "SRT 无有效字幕段"})
            return 1

        device_used = "cpu"  # XTTS 不支持 MPS;强制 CPU
        print(f"[dub] 加载 XTTS 模型 {args.model} ({device_used})", flush=True)
        tts = _load_xtts(args.model, device_used)
        # 若配置 speaker 不在列表,回退列表首项
        speaker = args.speaker
        if getattr(tts, "speakers", None):
            if speaker not in tts.speakers:
                print(f"[dub] speaker {speaker!r} 不在列表,改用 {tts.speakers[0]!r}", flush=True)
                speaker = tts.speakers[0]

        clips = []
        for i, (start, end, text) in enumerate(segs):
            target = end - start
            print(f"[dub] 段 {i+1}/{len(segs)} [{start:.2f}-{end:.2f}] {text[:40]}", flush=True)
            raw_wav = os.path.join(workdir, f"seg_{i:04d}_raw.wav")
            tts.tts_to_file(
                text=text, file_path=raw_wav,
                speaker=speaker, language=args.language,
            )
            natural = wav_duration_sec(raw_wav)
            plan = fit_segment_audio(natural, target, max_tempo=args.max_tempo)
            if plan["mode"] == "skip":
                print(f"[dub] 段 {i} skip (invalid timing)", flush=True)
                continue

            fitted = os.path.join(workdir, f"seg_{i:04d}_fit.wav")
            if plan["mode"] == "pad":
                pad_or_truncate_wav(raw_wav, fitted, plan["pad_to_sec"], "pad")
            elif plan["mode"] == "stretch":
                tmp = os.path.join(workdir, f"seg_{i:04d}_rb.wav")
                rubberband_stretch(raw_wav, tmp, plan["time_ratio"])
                # 关键:stretch 后必须归一到 44.1k/2ch,否则 amix 采样率冲突
                normalize_wav_44k_stereo(tmp, fitted)
            else:  # stretch_and_truncate
                tmp = os.path.join(workdir, f"seg_{i:04d}_rb.wav")
                rubberband_stretch(raw_wav, tmp, plan["time_ratio"])
                pad_or_truncate_wav(tmp, fitted, plan["truncate_to_sec"], "truncate")
            clips.append((start, fitted))

        mix_onto_background(args.no_vocals, clips, args.output)
        _emit({
            "ok": True,
            "dubbed": args.output,
            "segments": len(clips),
            "device_used": device_used,
            "speaker": speaker,
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
