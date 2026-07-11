# RubberBand + XTTSv2 配音 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 AI 重制管线中新增 `remaster_dub` stage：用 XTTSv2 固定声线把中文字幕逐段合成语音，RubberBand 变速对齐原段时长，叠到 Demucs no_vocals 背景上，产出 `dubbed_audio_<task_id>.wav` 中间产物；默认关，软失败。

**Architecture:** 与 WhisperX/Demucs 同构——`asr-venv` 跑 `dub_runner.py` CLI；`DubGenerator` subprocess adapter；`task_manager._run_remaster_dub` 软失败接线。时长策略纯逻辑拆到 `dub_timing.py` 便于单测。gate 独立挂在 `translate_subtitle` 之后。

**Tech Stack:** Python 3 / coqui-tts (XTTSv2) / RubberBand CLI / torch (asr-venv) / ffmpeg / subprocess / unittest

**Test runner (项目事实):** pytest 未安装。统一用：
```bash
.venv/bin/python -m unittest <module.path> -v
# 或全量
.venv/bin/python -m unittest discover tests -v
```
无裸 `python` 时用 `.venv/bin/python`。

---

## 参考文件（实现者应先读）

- `modules/demucs_separator.py` — adapter 骨架
- `modules/demucs_runner.py` — runner CLI 骨架
- `modules/task_manager.py` — `_run_remaster_demucs`（~3118-3170）、process_task 里 translate_subtitle 块（~2482-2495）、TASK_STATES/PIPELINE_STAGE_*、schema/迁移/ALLOWED_COLUMNS 中 demucs_warning 三件套
- `modules/config_manager.py` — DEMUCS_* 块后插入 DUB_*
- `tests/test_demucs_separator.py` / `test_demucs_config.py` / `test_demucs_warning_persistence.py` — 单测模板
- Spec: `docs/superpowers/specs/2026-07-11-xtts-rubberband-dubbing-design.md`

**关键契约（照抄，勿创新）：**
- adapter 返回 `(bool, dict | str)`
- runner stdout 末行 JSON；退出码 0/1/2
- 软失败：只写 `dub_warning_message` + 还原 status，不 raise
- 只落盘中间产物，不写 `video_path_*` / `subtitle_path_*`

**RubberBand 时间比约定：**
- CLI：`rubberband -t <time_ratio> -c 1 in.wav out.wav`
- `time_ratio` = 输出时长 / 输入时长（`<1` 加速缩短，`>1` 拉长）
- `max_tempo`（配置，默认 1.5）= **最大加速倍率** → `min_time_ratio = 1/max_tempo`

---

## File map

| 文件 | 职责 |
|------|------|
| `modules/dub_timing.py` | 纯逻辑：pad/stretch/截断决策 |
| `modules/dub_runner.py` | asr-venv CLI：XTTS + RubberBand + 混音 |
| `modules/dub_generator.py` | subprocess adapter |
| `modules/config_manager.py` | DUB_* 键 |
| `modules/task_manager.py` | stage/状态/DB 三件套/`_run_remaster_dub`/门控 |
| `tests/test_dub_*.py` | 单测 |

---

## Task 0: 环境准备（实现前）

**Files:** 无（环境操作）

- [ ] **Step 1: 安装 RubberBand**

```bash
brew install rubberband
which rubberband
rubberband --help | head -20
```
Expected: `rubberband` 在 PATH；help 含 `-t` / `--time` 与 formant/crisp 相关选项。

- [ ] **Step 2: asr-venv 安装 coqui-tts**

```bash
/Users/mac/asr-venv/bin/pip install -U coqui-tts pypinyin
# 若包名不可用，试：pip install -U TTS
/Users/mac/asr-venv/bin/python -c "from TTS.api import TTS; print('TTS OK')"
```
Expected: import 成功；torch 仍为 2.8.x 不被严重降级。

**注意 `pypinyin` 是中文硬依赖**：XTTS 中文合成走 `chinese_transliterate`，缺 `pypinyin` 会在 `tts_to_file(..., language="zh")` 时 `ImportError: Chinese requires: pypinyin`。冒烟实测踩过，必须一并装。

**注意 CPML 非交互接受**：XTTSv2 下载/加载前需 `export COQUI_TOS_AGREED=1`，否则会 `input()` 卡死。runner 内已 `os.environ.setdefault("COQUI_TOS_AGREED", "1")`，环境准备脚本同样要 export。

- [ ] **Step 3: 触发 XTTSv2 权重下载（~2GB）**

```bash
/Users/mac/asr-venv/bin/python - <<'PY'
from TTS.api import TTS
tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
print("speakers sample:", list(tts.speakers)[:8] if tts.speakers else "n/a")
tts.tts_to_file(text="你好，这是配音测试。", file_path="/tmp/_xtts_probe.wav",
                speaker=tts.speakers[0] if tts.speakers else "Ana Florence",
                language="zh")
print("wrote /tmp/_xtts_probe.wav")
PY
ls -lh /tmp/_xtts_probe.wav
```
Expected: 模型下载完成；探测 wav 生成。若 HF 卡：按项目既有镜像策略处理。

记录可用 speaker 名；默认 `Ana Florence` 若不在列表则改用列表首项，并在 config 注释里写清。

- [ ] **Step 4: 清理探针**

```bash
rm -f /tmp/_xtts_probe.wav
```

---

## Task 1: config 新键 + 单测（TDD）

**Files:**
- Modify: `modules/config_manager.py`（DEMUCS 块之后）
- Create: `tests/test_dub_config.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_dub_config.py`:
```python
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.config_manager import DEFAULT_CONFIG


class TestDubConfig(unittest.TestCase):
    def test_defaults_present(self):
        for k in [
            "DUB_PYTHON", "DUB_RUNNER", "DUB_XTTS_MODEL", "DUB_SPEAKER",
            "DUB_LANGUAGE", "DUB_DEVICE", "DUB_MAX_TEMPO", "DUB_TIMEOUT_SECONDS",
        ]:
            self.assertIn(k, DEFAULT_CONFIG)
        self.assertEqual(DEFAULT_CONFIG["DUB_PYTHON"], "/Users/mac/asr-venv/bin/python")
        self.assertEqual(
            DEFAULT_CONFIG["DUB_XTTS_MODEL"],
            "tts_models/multilingual/multi-dataset/xtts_v2",
        )
        self.assertEqual(DEFAULT_CONFIG["DUB_SPEAKER"], "Ana Florence")
        self.assertEqual(DEFAULT_CONFIG["DUB_LANGUAGE"], "zh")
        self.assertEqual(DEFAULT_CONFIG["DUB_DEVICE"], "cpu")
        self.assertEqual(DEFAULT_CONFIG["DUB_MAX_TEMPO"], 1.5)
        self.assertEqual(DEFAULT_CONFIG["DUB_TIMEOUT_SECONDS"], 14400)

    def test_remaster_switch_still_off(self):
        self.assertIn("REMASTER_PIPELINE_ENABLED", DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["REMASTER_PIPELINE_ENABLED"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/mac/Y2A-Auto && .venv/bin/python -m unittest tests.test_dub_config -v
```
Expected: FAIL — `DUB_PYTHON` not in DEFAULT_CONFIG

- [ ] **Step 3: 加 config 键**

In `modules/config_manager.py`，在 DEMUCS 块末尾：
```python
    "DEMUCS_TIMEOUT_SECONDS": 7200,       # 长视频分离超时 2h
```
**之后**插入：
```python
    # AI 重制管线——XTTSv2 + RubberBand 配音(复用 REMASTER 总开关)
    "DUB_PYTHON": "/Users/mac/asr-venv/bin/python",
    "DUB_RUNNER": "modules/dub_runner.py",
    "DUB_XTTS_MODEL": "tts_models/multilingual/multi-dataset/xtts_v2",
    "DUB_SPEAKER": "Ana Florence",       # XTTS 内置声线;Task0 若列表无此名则改列表首项
    "DUB_LANGUAGE": "zh",
    "DUB_DEVICE": "cpu",                 # XTTS 自定义算子不支持 MPS
    "DUB_MAX_TEMPO": 1.5,                # RubberBand 最大加速倍率
    "DUB_TIMEOUT_SECONDS": 14400,        # 4h;CPU 配音慢
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/bin/python -m unittest tests.test_dub_config -v
```
Expected: OK (2 tests)

- [ ] **Step 5: Commit**

```bash
git add modules/config_manager.py tests/test_dub_config.py
git commit -m "feat(dub): config 新增 DUB_* 键(复用 REMASTER 总开关,默认关)"
```

---

## Task 2: dub_timing 纯逻辑 + 单测（TDD）

**Files:**
- Create: `modules/dub_timing.py`
- Create: `tests/test_dub_timing.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_dub_timing.py`:
```python
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.dub_timing import fit_segment_audio


class TestDubTiming(unittest.TestCase):
    def test_pad_when_shorter(self):
        # 自然 1.0s, 目标 2.0s → pad, time_ratio=1.0, pad_to=2.0
        r = fit_segment_audio(natural_sec=1.0, target_sec=2.0, max_tempo=1.5)
        self.assertEqual(r["mode"], "pad")
        self.assertEqual(r["time_ratio"], 1.0)
        self.assertEqual(r["pad_to_sec"], 2.0)
        self.assertIsNone(r["truncate_to_sec"])

    def test_stretch_within_max_tempo(self):
        # 自然 3.0s, 目标 2.0s → 需加速 1.5x → time_ratio=2/3
        r = fit_segment_audio(natural_sec=3.0, target_sec=2.0, max_tempo=1.5)
        self.assertEqual(r["mode"], "stretch")
        self.assertAlmostEqual(r["time_ratio"], 2.0 / 3.0, places=6)
        self.assertIsNone(r["pad_to_sec"])
        self.assertIsNone(r["truncate_to_sec"])

    def test_stretch_and_truncate_when_exceeds_max_tempo(self):
        # 自然 4.0s, 目标 2.0s → 需 2x 加速, 但 max_tempo=1.5 → time_ratio=1/1.5, truncate 到 2.0
        r = fit_segment_audio(natural_sec=4.0, target_sec=2.0, max_tempo=1.5)
        self.assertEqual(r["mode"], "stretch_and_truncate")
        self.assertAlmostEqual(r["time_ratio"], 1.0 / 1.5, places=6)
        self.assertEqual(r["truncate_to_sec"], 2.0)
        self.assertIsNone(r["pad_to_sec"])

    def test_exact_match(self):
        r = fit_segment_audio(natural_sec=2.0, target_sec=2.0, max_tempo=1.5)
        self.assertEqual(r["mode"], "pad")
        self.assertEqual(r["time_ratio"], 1.0)
        self.assertEqual(r["pad_to_sec"], 2.0)

    def test_invalid_non_positive(self):
        r = fit_segment_audio(natural_sec=0.0, target_sec=1.0, max_tempo=1.5)
        self.assertEqual(r["mode"], "skip")
        r2 = fit_segment_audio(natural_sec=1.0, target_sec=0.0, max_tempo=1.5)
        self.assertEqual(r2["mode"], "skip")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m unittest tests.test_dub_timing -v
```
Expected: FAIL — module not found

- [ ] **Step 3: 实现 `modules/dub_timing.py`**

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/bin/python -m unittest tests.test_dub_timing -v
```
Expected: OK (5 tests)

- [ ] **Step 5: Commit**

```bash
git add modules/dub_timing.py tests/test_dub_timing.py
git commit -m "feat(dub): dub_timing 时长拟合纯逻辑(pad/stretch/截断)+单测"
```

---

## Task 3: dub_runner.py CLI

**Files:**
- Create: `modules/dub_runner.py`

> 真模型冒烟在 Task 6。本任务保证 CLI 结构、时长策略调用、RubberBand 调用、混音骨架、stdout JSON、退出码。

- [ ] **Step 1: 写 runner**

Create `modules/dub_runner.py`：

```python
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
    cmd = ["rubberband", "-t", str(time_ratio), "-c", "1", in_wav, out_wav]
    print(f"[dub] rubberband: {' '.join(cmd)}", flush=True)
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode(errors="ignore")[-400:]
        raise RuntimeError(f"rubberband 失败: {detail}") from None


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


def mix_onto_background(bg_wav: str, clips, output_wav: str, workdir: str) -> None:
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
        # adelay 对每个声道都要设
        filter_parts.append(
            f"[{i+1}:a]adelay={delay_ms}|{delay_ms},apad[a{i}]"
        )
        labels.append(f"[a{i}]")
    n = 1 + len(clips)
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

        print(f"[dub] 加载 XTTS 模型 {args.model} ({args.device})", flush=True)
        from TTS.api import TTS
        tts = TTS(args.model)
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
                rubberband_stretch(raw_wav, fitted, plan["time_ratio"])
            else:  # stretch_and_truncate
                tmp = os.path.join(workdir, f"seg_{i:04d}_rb.wav")
                rubberband_stretch(raw_wav, tmp, plan["time_ratio"])
                pad_or_truncate_wav(tmp, fitted, plan["truncate_to_sec"], "truncate")
            clips.append((start, fitted))

        mix_onto_background(args.no_vocals, clips, args.output, workdir)
        _emit({
            "ok": True,
            "dubbed": args.output,
            "segments": len(clips),
            "device_used": args.device,
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
```

- [ ] **Step 2: 参数错退出码（不跑真模型）**

```bash
cd /Users/mac/Y2A-Auto
/Users/mac/asr-venv/bin/python modules/dub_runner.py \
  --translated-srt /nope.srt --no-vocals /nope.wav --output /tmp/x.wav --task-id t1
echo "exit=$?"
```
Expected: 末行含 `"ok": false` 与字幕不存在；`exit=2`

- [ ] **Step 3: Commit**

```bash
git add modules/dub_runner.py
git commit -m "feat(dub): dub_runner CLI(XTTS 逐段合成+RubberBand 对齐+混音)"
```

---

## Task 4: DubGenerator adapter + 单测（TDD）

**Files:**
- Create: `modules/dub_generator.py`
- Create: `tests/test_dub_generator.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_dub_generator.py`（照抄 demucs stub 风格）:

```python
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.dub_generator import DubGenerator


def _write_stub_runner(path, exit_code=0, final_json=None, lines=None):
    body = ["#!/usr/bin/env bash", "echo '[stub-runner] invoked:' \"$@\" >&2"]
    for ln in (lines or ["[dub] 加载 XTTS", "[dub] 段 1/1"]):
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


class TestDubGenerator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.srt = os.path.join(self.tmp, "zh.srt")
        open(self.srt, "w").write("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        self.bg = os.path.join(self.tmp, "bg.wav")
        open(self.bg, "wb").write(b"RIFF")  # 仅存在性
        self.out = os.path.join(self.tmp, "dub.wav")
        self.stub = os.path.join(self.tmp, "fake-runner")

    def test_missing_python_bin(self):
        _write_stub_runner(self.stub, exit_code=0, final_json={"ok": True})
        g = DubGenerator(python_bin="/nonexistent/python", runner_path=self.stub)
        ok, res = g.generate(
            translated_srt_path=self.srt, no_vocals_wav=self.bg,
            output_path=self.out, task_id="t1",
        )
        self.assertFalse(ok)
        self.assertIn("python", str(res).lower())

    def test_missing_runner(self):
        g = DubGenerator(python_bin="/bin/bash", runner_path="/nonexistent/runner.py")
        ok, res = g.generate(
            translated_srt_path=self.srt, no_vocals_wav=self.bg,
            output_path=self.out, task_id="t1",
        )
        self.assertFalse(ok)
        self.assertIn("runner", str(res).lower())

    def test_missing_srt(self):
        _write_stub_runner(self.stub, exit_code=0, final_json={"ok": True})
        g = DubGenerator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = g.generate(
            translated_srt_path="/nope.srt", no_vocals_wav=self.bg,
            output_path=self.out, task_id="t1",
        )
        self.assertFalse(ok)
        self.assertIn("不存在", str(res))

    def test_missing_no_vocals(self):
        _write_stub_runner(self.stub, exit_code=0, final_json={"ok": True})
        g = DubGenerator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = g.generate(
            translated_srt_path=self.srt, no_vocals_wav="/nope.wav",
            output_path=self.out, task_id="t1",
        )
        self.assertFalse(ok)
        self.assertIn("不存在", str(res))

    def test_success_parses_json(self):
        _write_stub_runner(
            self.stub, exit_code=0,
            final_json={"ok": True, "dubbed": self.out, "segments": 3, "device_used": "cpu"},
            lines=["[dub] 加载 XTTS"],
        )
        g = DubGenerator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = g.generate(
            translated_srt_path=self.srt, no_vocals_wav=self.bg,
            output_path=self.out, task_id="t1",
        )
        self.assertTrue(ok, msg=str(res))
        self.assertEqual(res["dubbed"], self.out)
        self.assertEqual(res["segments"], 3)

    def test_nonzero_exit(self):
        _write_stub_runner(
            self.stub, exit_code=1,
            final_json={"ok": False, "error": "xtts 合成失败"},
        )
        g = DubGenerator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = g.generate(
            translated_srt_path=self.srt, no_vocals_wav=self.bg,
            output_path=self.out, task_id="t1",
        )
        self.assertFalse(ok)
        self.assertIn("合成失败", str(res))

    def test_progress_callback(self):
        _write_stub_runner(
            self.stub, exit_code=0,
            final_json={"ok": True, "dubbed": self.out},
            lines=["加载 XTTS", "段 1/2", "混音"],
        )
        events = []
        g = DubGenerator(python_bin="/bin/bash", runner_path=self.stub)
        g.generate(
            translated_srt_path=self.srt, no_vocals_wav=self.bg,
            output_path=self.out, task_id="t1",
            progress_callback=events.append,
        )
        self.assertTrue(any("段" in e or "混音" in e for e in events))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m unittest tests.test_dub_generator -v
```
Expected: FAIL — module not found

- [ ] **Step 3: 实现 adapter**

Create `modules/dub_generator.py`（逐字照抄 DemucsSeparator 骨架，改参数/文案）:

```python
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DubGenerator —— subprocess 适配器,调 modules/dub_runner.py(用 asr-venv python)。

与 DemucsSeparator/WhisperXAsr 同构:逐行 stdout 进度 + 末行 JSON。
"""
import json
import logging
import os
import subprocess


class DubGenerator:
    def __init__(self, python_bin: str, runner_path: str):
        self.python_bin = python_bin
        self.runner_path = runner_path
        self.logger = None
        self.task_id = None

    def _log(self, msg):
        if self.logger:
            self.logger.info(msg)
        else:
            logging.getLogger("dub_generator").info(msg)

    def generate(self, *, translated_srt_path, no_vocals_wav, output_path, task_id,
                 speaker="Ana Florence", language="zh", device="cpu",
                 max_tempo=1.5, model="tts_models/multilingual/multi-dataset/xtts_v2",
                 progress_callback=None, timeout=14400):
        """返回 (True, {ok, dubbed, ...}) 或 (False, error_msg)。"""
        self.task_id = task_id
        if not self.python_bin or not os.path.isfile(self.python_bin):
            return False, f"asr-venv python 未安装或路径无效: {self.python_bin}"
        if not self.runner_path or not os.path.isfile(self.runner_path):
            return False, f"dub runner 不存在: {self.runner_path}"
        if not translated_srt_path or not os.path.exists(translated_srt_path):
            return False, f"翻译字幕不存在: {translated_srt_path}"
        if not no_vocals_wav or not os.path.exists(no_vocals_wav):
            return False, f"背景音不存在: {no_vocals_wav}"

        cmd = [
            self.python_bin, self.runner_path,
            "--translated-srt", translated_srt_path,
            "--no-vocals", no_vocals_wav,
            "--output", output_path,
            "--task-id", str(task_id),
            "--speaker", str(speaker or "Ana Florence"),
            "--language", str(language or "zh"),
            "--device", str(device or "cpu"),
            "--max-tempo", str(max_tempo if max_tempo is not None else 1.5),
            "--model", str(model or "tts_models/multilingual/multi-dataset/xtts_v2"),
        ]
        self._log(f"调用 dub runner: {' '.join(cmd)}")
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
            return False, f"dub 超时({timeout}s)"
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
.venv/bin/python -m unittest tests.test_dub_generator -v
```
Expected: OK (7 tests)

- [ ] **Step 5: Commit**

```bash
git add modules/dub_generator.py tests/test_dub_generator.py
git commit -m "feat(dub): DubGenerator subprocess adapter + 单测"
```

---

## Task 5: task_manager 接线（状态 + stage + DB 三件套 + 方法 + 门控）

**Files:**
- Modify: `modules/task_manager.py`
- Create: `tests/test_dub_warning_persistence.py`

- [ ] **Step 1: TASK_STATES + PROCESSING_STATES**

在 `AUDIO_SEPARATING` 行后加：
```python
    'DUBBING': 'dubbing',                    # 配音合成中(XTTS+RubberBand)
```

在 `PROCESSING_STATES` 的 `AUDIO_SEPARATING` 后加：
```python
    TASK_STATES['DUBBING'],
```

- [ ] **Step 2: pipeline stage 常量 + ORDER**

在：
```python
PIPELINE_STAGE_TRANSLATE_SUBTITLE = 'translate_subtitle'
PIPELINE_STAGE_UPLOAD_TO_ACFUN = 'upload_to_acfun'
```
之间插入：
```python
PIPELINE_STAGE_REMASTER_DUB = 'remaster_dub'
```

在 `PIPELINE_STAGE_ORDER` 中：
```python
    PIPELINE_STAGE_TRANSLATE_SUBTITLE,
    PIPELINE_STAGE_UPLOAD_TO_ACFUN,
```
之间插入：
```python
    PIPELINE_STAGE_REMASTER_DUB,
```

- [ ] **Step 3: DB schema + 迁移 + ALLOWED_COLUMNS（三件套）**

Schema：在 `demucs_warning_message` 行后改逗号并追加：
```python
        demucs_warning_message TEXT,  -- Demucs 音轨分离阶段的非致命警告，不影响上传流程
        dub_warning_message TEXT  -- 配音阶段的非致命警告，不影响上传流程
```
（若 `demucs_warning_message` 当前是最后一列无逗号，先给它加逗号。）

迁移：在 demucs 迁移块后追加：
```python
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'dub_warning_message' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN dub_warning_message TEXT")
            logger.info("数据库升级：添加dub_warning_message字段")
            conn.commit()
```

ALLOWED_COLUMNS：在 demucs 项后加：
```python
        'dub_warning_message': 'dub_warning_message = ?',
```

- [ ] **Step 4: 写 warning 持久化回归测试（先失败再绿）**

Create `tests/test_dub_warning_persistence.py`（照抄 demucs 版，字段改名）：
```python
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import modules.task_manager as tm


class TestDubWarningPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_db_path = tm.DB_PATH
        tm.DB_PATH = os.path.join(self.tmpdir, "tasks.db")
        tm.init_db()

    def tearDown(self):
        tm.DB_PATH = self._orig_db_path
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_dub_warning_message_round_trip(self):
        task_id = tm.add_task("https://youtu.be/dub_test")
        tm.update_task(task_id, dub_warning_message="dub: xtts timeout")
        task = tm.get_task(task_id)
        self.assertEqual(task["dub_warning_message"], "dub: xtts timeout")

    def test_dub_warning_message_cleared_on_success(self):
        task_id = tm.add_task("https://youtu.be/dub_test2")
        tm.update_task(task_id, dub_warning_message="dub: temp")
        tm.update_task(task_id, dub_warning_message=None)
        task = tm.get_task(task_id)
        self.assertIsNone(task["dub_warning_message"])


if __name__ == "__main__":
    unittest.main()
```

跑：
```bash
.venv/bin/python -m unittest tests.test_dub_warning_persistence -v
```
Expected: 在三件套落地前 FAIL；落地后 OK。

- [ ] **Step 5: 写 `_run_remaster_dub`**

紧接 `_run_remaster_demucs` 之后、`_translate_subtitle` 之前插入：

```python
    def _run_remaster_dub(self, task_id, task_logger):
        """AI 重制管线:XTTSv2 固定声线配音 + RubberBand 对齐,产出 dubbed_audio wav。

        只产中间产物落盘,不写任何下游业务字段;供未来 FFmpeg 合成子项目消费。
        软失败:失败不阻断上传。
        """
        from modules.dub_generator import DubGenerator

        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return False

        srt_path = str(task.get("subtitle_path_translated") or "").strip()
        if not srt_path or not os.path.exists(srt_path):
            task_logger.warning("翻译字幕缺失,跳过配音")
            return False

        task_dir = os.path.join(DOWNLOADS_DIR, task_id)
        no_vocals = os.path.join(task_dir, f"demucs_no_vocals_{task_id}.wav")
        if not os.path.exists(no_vocals):
            task_logger.warning(f"Demucs 背景音缺失({no_vocals}),跳过配音")
            return False

        python_bin = str(self.config.get("DUB_PYTHON", "") or "").strip()
        runner_path = str(self.config.get("DUB_RUNNER", "modules/dub_runner.py") or "").strip()
        if not runner_path or not os.path.isabs(runner_path):
            runner_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                runner_path,
            )

        os.makedirs(task_dir, exist_ok=True)
        out_wav = os.path.join(task_dir, f"dubbed_audio_{task_id}.wav")

        prev_status = task.get("status")
        update_task(task_id, status=TASK_STATES["DUBBING"])
        task_logger.info(
            f"重制管线:调用配音(XTTS/{self.config.get('DUB_SPEAKER', 'Ana Florence')}/"
            f"{self.config.get('DUB_DEVICE', 'cpu')}),输出 {out_wav}"
        )

        gen = DubGenerator(python_bin=python_bin, runner_path=runner_path)
        gen.logger = task_logger
        ok, res = gen.generate(
            translated_srt_path=srt_path,
            no_vocals_wav=no_vocals,
            output_path=out_wav,
            task_id=task_id,
            speaker=str(self.config.get("DUB_SPEAKER", "Ana Florence") or "Ana Florence"),
            language=str(self.config.get("DUB_LANGUAGE", "zh") or "zh"),
            device=str(self.config.get("DUB_DEVICE", "cpu") or "cpu"),
            max_tempo=float(self.config.get("DUB_MAX_TEMPO", 1.5) or 1.5),
            model=str(
                self.config.get(
                    "DUB_XTTS_MODEL",
                    "tts_models/multilingual/multi-dataset/xtts_v2",
                )
                or "tts_models/multilingual/multi-dataset/xtts_v2"
            ),
            progress_callback=lambda t: task_logger.info(f"[dub] {t}"),
            timeout=_as_int(self.config.get("DUB_TIMEOUT_SECONDS", 14400), 14400, minimum=60),
        )
        if ok:
            # 只产中间产物,不写视频/字幕路径。见 spec §1。
            update_task(task_id, dub_warning_message=None, status=prev_status)
            task_logger.info(f"配音完成: {res}")
            return True
        task_logger.error(f"配音失败: {res}")
        update_task(task_id, dub_warning_message=f"dub: {res}", status=prev_status)
        return False
```

- [ ] **Step 6: process_task 独立门控块**

在 translate_subtitle 块之后、上传块之前插入：

```python
            if PIPELINE_STAGE_TRANSLATE_SUBTITLE in completed_stages and \
                    PIPELINE_STAGE_REMASTER_DUB not in completed_stages and \
                    _as_bool(self.config.get('REMASTER_PIPELINE_ENABLED', False)):
                try:
                    self._run_remaster_dub(task_id, task_logger)
                except Exception as e:
                    task_logger.error(f"重制配音异常: {e}")
                completed_stages = _mark_stage_done(
                    task_id, completed_stages, PIPELINE_STAGE_REMASTER_DUB
                )
```

- [ ] **Step 7: 常量/顺序断言 + 全测试**

```bash
cd /Users/mac/Y2A-Auto && .venv/bin/python -c "
import modules.task_manager as tm
assert tm.TASK_STATES['DUBBING'] == 'dubbing'
assert tm.PIPELINE_STAGE_REMASTER_DUB == 'remaster_dub'
order = tm.PIPELINE_STAGE_ORDER
assert order.index(tm.PIPELINE_STAGE_TRANSLATE_SUBTITLE) < order.index(tm.PIPELINE_STAGE_REMASTER_DUB)
assert order.index(tm.PIPELINE_STAGE_REMASTER_DUB) < order.index(tm.PIPELINE_STAGE_UPLOAD_TO_ACFUN)
assert tm.TASK_STATES['DUBBING'] in tm.PROCESSING_STATES
assert hasattr(tm.TaskProcessor, '_run_remaster_dub') or hasattr(tm, 'TaskProcessor')
cls = getattr(tm, 'TaskProcessor', None) or getattr(tm, 'TaskManager', None)
assert hasattr(cls, '_run_remaster_dub')
print('OK')
"
.venv/bin/python -m unittest discover tests -v 2>&1 | tail -8
```
Expected: `OK`；全套测试绿。

- [ ] **Step 8: Commit**

```bash
git add modules/task_manager.py tests/test_dub_warning_persistence.py
git commit -m "feat(dub): task_manager remaster_dub stage + dub_warning 三件套 + 软失败接线"
```

---

## Task 6: 真实冒烟 + 复审 + 合并

**Files:** 无（验证 + 收尾）

- [ ] **Step 1: 准备短中文 SRT + no_vocals**

若有既有 Demucs 产物可用则复用；否则用 ffmpeg 造 5s 静音立体声 44.1k 当背景 + 手写 2 段中文 SRT：

```bash
ffmpeg -y -f lavfi -i "sine=frequency=200:duration=5" -ac 2 -ar 44100 /tmp/dub_bg.wav
cat > /tmp/dub_zh.srt <<'SRT'
1
00:00:00,000 --> 00:00:02,500
你好，这是配音测试。

2
00:00:02,500 --> 00:00:05,000
Y2A Auto 使用本地语音合成。
SRT
```

- [ ] **Step 2: 直接跑 runner**

```bash
cd /Users/mac/Y2A-Auto
mkdir -p /tmp/dub_out
/Users/mac/asr-venv/bin/python modules/dub_runner.py \
  --translated-srt /tmp/dub_zh.srt \
  --no-vocals /tmp/dub_bg.wav \
  --output /tmp/dub_out/dubbed_audio_smoke.wav \
  --task-id smoke \
  --device cpu
echo "exit=$?"
ls -lh /tmp/dub_out/dubbed_audio_smoke.wav
ffprobe -v error -show_entries stream=sample_rate,channels -show_entries format=duration \
  -of default=noprint_wrappers=1 /tmp/dub_out/dubbed_audio_smoke.wav
```
Expected: 末行 `{"ok": true, ...}`；exit 0；duration≈5s；44100/2ch（或与 bg 一致）。

- [ ] **Step 3: 清理冒烟产物**

```bash
rm -rf /tmp/dub_bg.wav /tmp/dub_zh.srt /tmp/dub_out
```

- [ ] **Step 4: 全分支复审（opus）**

重点：
- 软失败 / 不写下游字段
- `dub_warning_message` 三件套齐全
- gate 在 translate_subtitle 之后
- REMASTER 关时不调用
- RubberBand 缺失时 exit 2 明确报错
- `fit_segment_audio` 与 runner 使用一致

修复审问题后重跑全测试。

- [ ] **Step 5: squash 合并 main**

```bash
git checkout main
git merge --squash feat/xtts-rubberband-dubbing   # 或当前功能分支名
git commit -m "feat: AI 重制管线第四块 - XTTSv2 固定声线配音 + RubberBand 对齐"
```

---

## Self-Review（写计划时）

**1. Spec coverage**
- §1 边界 / 软失败 → Task 5 `_run_remaster_dub` + 门控 ✓
- §2 决策（XTTS / 固定声线 / RubberBand / 复用开关）→ Task 0/1/3 ✓
- §3 架构与 stage 顺序 → Task 5 ORDER + 门控位置 ✓
- §4 runner 流程 / 时长策略 / RubberBand CLI → Task 2+3 ✓
- §5 adapter → Task 4 ✓
- §6 DB 三件套 + 接线 → Task 5 ✓
- §7 config → Task 1 ✓
- §8 测试 → Task 1/2/4/5 ✓
- §9 环境 + 冒烟 → Task 0/6 ✓

**2. Placeholder scan:** 无 TBD；代码完整；命令含期望输出 ✓

**3. Type consistency**
- `DubGenerator.generate(...)` 签名 Task 4 定义 ↔ Task 5 调用 ✓
- runner CLI 参数 Task 3 ↔ adapter cmd Task 4 ✓
- 产物名 `dubbed_audio_<task_id>.wav` 全程一致 ✓
- `fit_segment_audio` 字段 `mode/time_ratio/pad_to_sec/truncate_to_sec` Task 2↔3 ✓
- `DUB_*` 键 Task 1↔5 ✓
- 状态 `DUBBING` / stage `remaster_dub` / 警告字段 `dub_warning_message` 一致 ✓
