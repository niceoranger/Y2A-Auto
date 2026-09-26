# Demucs 音轨分离 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 AI 重制管线里新增 Demucs 音轨分离 stage，从任务视频分离人声/背景音各落盘为 44.1kHz 立体声 wav，供未来 TTS 配音子项目消费；默认关，只落盘不接下游。

**Architecture:** 与 WhisperX 子项目完全同构——独立 `asr-venv` 跑 `demucs_runner.py` CLI（ffmpeg 抽 wav → `python -m demucs` 分离 → 收产物落盘），`DemucsSeparator` subprocess adapter 照抄 `WhisperXAsr` 骨架（Popen + 逐行 stdout 进度 + 末行 JSON + 超时 kill），`task_manager._run_remaster_demucs` 照抄 `_run_remaster_asr` 软失败接线，gated by `REMASTER_PIPELINE_ENABLED`。

**Tech Stack:** Python 3 / demucs (htdemucs_ft) / torch+torchaudio (asr-venv 已装) / ffmpeg 8.x / subprocess / unittest

---

## 参考文件（实现者应先读）

- `modules/whisperx_runner.py` — runner CLI 模板（ffmpeg 抽音 + stdout JSON 协议 + 退出码）
- `modules/whisperx_asr.py` — adapter 模板（`WhisperXAsr`，逐行读 + 末行 JSON + 超时 kill）
- `modules/task_manager.py:3054-3102` — `_run_remaster_asr` 接线模板
- `modules/task_manager.py:2455-2462` — process_task 里 REMASTER 门控调用点
- `modules/config_manager.py:79-87` — WHISPERX_* config 块（DEMUCS_* 紧随其后）
- `tests/test_whisperx_asr.py` — adapter 单测模板（stub bash runner）
- `tests/test_whisperx_config.py` — config 单测模板

**关键契约（照抄，勿创新）：**
- adapter 方法返回 `(bool, dict | str)`：成功 `(True, last_json_dict)`，失败 `(False, error_str)`
- runner stdout：进度行直接 print，**末行**一个 JSON；成功 `{"ok": true, ...}`，失败 `{"ok": false, "error": "..."}`
- runner 退出码：`0` 成功 / `1` 运行失败 / `2` 参数错（视频不存在、import 失败）
- 软失败：Demucs 失败只写 `demucs_warning_message`、还原 status、`return False`，绝不 raise、不阻断 ASR/翻译/上传

---

## Task 0: 环境准备（人工，先于写码）

> 这一步不是写代码，是 spec §8 要求的"实现前先手动下权重"，避免踩 HF 镜像坑。由控制者/人执行，完成后再进 Task 1。

**Files:** 无（环境操作）

- [ ] **Step 1: asr-venv 安装 demucs**

Run:
```bash
/Users/mac/asr-venv/bin/pip install -U demucs soundfile
```
Expected: 安装成功；`demucs` 及其依赖（`dora-search`/`julius`/`lameenc`/`openunmix` 等）就位。torch/torchaudio 已在 venv（2.8.0），不应被降级。

**注意 `soundfile` 是硬依赖**：torchaudio 2.9 移除了内置 wav 写后端，缺 `soundfile`(libsndfile) 时 Demucs 能算出分离但**保存 wav 时报 "Couldn't find appropriate backend to handle uri ...vocals.wav"**。真实冒烟实测踩过这坑，必须一并装。

- [ ] **Step 2: 验证 demucs 可导入 + CLI 可用**

Run:
```bash
/Users/mac/asr-venv/bin/python -m demucs --help
```
Expected: 打印 demucs 用法（含 `--two-stems`、`-n`、`-d`、`--out` 参数），退出码 0。

- [ ] **Step 3: 手动触发 htdemucs_ft 权重下载**

Run（拿任意一小段 wav，或临时造 3 秒静音 wav 触发下载）:
```bash
/Users/mac/asr-venv/bin/python -c "import torchaudio, torch; torchaudio.save('/tmp/_silence.wav', torch.zeros(2, 44100*3), 44100)"
/Users/mac/asr-venv/bin/python -m demucs --two-stems=vocals -n htdemucs_ft -d cpu --out /tmp/_demucs_probe /tmp/_silence.wav
```
Expected: htdemucs_ft 的 4 个 bag 模型权重下载到 `~/.cache/torch/hub/checkpoints/`（或 `~/.cache/demucs/`），分离完成，`/tmp/_demucs_probe/htdemucs_ft/_silence/{vocals,no_vocals}.wav` 生成。

若权重下载卡在 dora/HF 源：照 WhisperX 老路手动 curl `https://hf-mirror.com` 对应权重到 `~/.cache/torch/hub/checkpoints/`，文件名与报错里请求的一致。

- [ ] **Step 4: 清理探针产物**

Run:
```bash
rm -rf /tmp/_silence.wav /tmp/_demucs_probe
```
Expected: 清理完成。权重已缓存，后续无需再下。

---

## Task 1: config 新键 + 单测（TDD）

**Files:**
- Modify: `modules/config_manager.py`（DEFAULT_CONFIG，WHISPERX 块之后，约 L87 后）
- Test: `tests/test_demucs_config.py`（Create）

- [ ] **Step 1: Write the failing test**

Create `tests/test_demucs_config.py`:
```python
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.config_manager import DEFAULT_CONFIG


class TestDemucsConfig(unittest.TestCase):
    def test_defaults_present(self):
        for k in ["DEMUCS_PYTHON", "DEMUCS_RUNNER", "DEMUCS_MODEL_NAME",
                  "DEMUCS_DEVICE", "DEMUCS_STEMS", "DEMUCS_TIMEOUT_SECONDS"]:
            self.assertIn(k, DEFAULT_CONFIG)
        self.assertEqual(DEFAULT_CONFIG["DEMUCS_MODEL_NAME"], "htdemucs_ft")
        self.assertEqual(DEFAULT_CONFIG["DEMUCS_DEVICE"], "mps")
        self.assertEqual(DEFAULT_CONFIG["DEMUCS_STEMS"], 2)
        self.assertEqual(DEFAULT_CONFIG["DEMUCS_PYTHON"], "/Users/mac/asr-venv/bin/python")

    def test_remaster_switch_still_off(self):
        # Demucs 复用 REMASTER 总开关，不新增开关；默认仍关
        self.assertIn("REMASTER_PIPELINE_ENABLED", DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["REMASTER_PIPELINE_ENABLED"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mac/Y2A-Auto && python -m pytest tests/test_demucs_config.py -v`
Expected: FAIL — `AssertionError: 'DEMUCS_PYTHON' not found in DEFAULT_CONFIG`

- [ ] **Step 3: Add the config keys**

In `modules/config_manager.py`, find the WHISPERX block ending at:
```python
    "WHISPERX_TIMEOUT_SECONDS": 7200,        # 长视频 CPU 跑慢,超时 2h
```
Insert immediately after that line:
```python
    # AI 重制管线(Phase 3)——Demucs 音轨分离子系统(复用 REMASTER 总开关)
    "DEMUCS_PYTHON": "/Users/mac/asr-venv/bin/python",  # 复用 asr-venv
    "DEMUCS_RUNNER": "modules/demucs_runner.py",         # runner 脚本(相对项目根)
    "DEMUCS_MODEL_NAME": "htdemucs_ft",   # 最高精度(4x 慢于 htdemucs)
    "DEMUCS_DEVICE": "mps",               # Apple Silicon 加速, runner 内 CPU 兜底
    "DEMUCS_STEMS": 2,                    # 2-stem: vocals / no_vocals
    "DEMUCS_TIMEOUT_SECONDS": 7200,       # 长视频分离超时 2h
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mac/Y2A-Auto && python -m pytest tests/test_demucs_config.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
cd /Users/mac/Y2A-Auto
git add modules/config_manager.py tests/test_demucs_config.py
git commit -m "feat(demucs): config 新增 DEMUCS_* 键(复用 REMASTER 总开关,默认关)"
```

---

## Task 2: demucs_runner.py CLI

> runner 跑在 asr-venv。真实分离逻辑靠 Task 5 真实冒烟验证；本任务只保证 CLI 结构、参数解析、产物收集、MPS→CPU 兜底、stdout JSON 协议、退出码正确。

**Files:**
- Create: `modules/demucs_runner.py`

- [ ] **Step 1: Write the runner**

Create `modules/demucs_runner.py`:
```python
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
    import sys
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
```

- [ ] **Step 2: Verify arg-parse + 参数错退出码（不跑真模型）**

Run: `cd /Users/mac/Y2A-Auto && /Users/mac/asr-venv/bin/python modules/demucs_runner.py --video /nope/x.mp4 --output-dir /tmp/dz --task-id t1; echo "exit=$?"`
Expected: 末行 `{"ok": false, "error": "视频不存在: /nope/x.mp4"}`，`exit=2`

- [ ] **Step 3: Commit**

```bash
cd /Users/mac/Y2A-Auto
git add modules/demucs_runner.py
git commit -m "feat(demucs): runner CLI(ffmpeg 抽 44.1k wav + demucs 2-stem + MPS→CPU 兜底)"
```

---

## Task 3: DemucsSeparator adapter + 单测（TDD）

**Files:**
- Create: `modules/demucs_separator.py`
- Test: `tests/test_demucs_separator.py`（Create）

- [ ] **Step 1: Write the failing test**

Create `tests/test_demucs_separator.py`:
```python
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.demucs_separator import DemucsSeparator


def _write_stub_runner(path, exit_code=0, final_json=None, lines=None):
    body = ["#!/usr/bin/env bash", "echo '[stub-runner] invoked:' \"$@\" >&2"]
    for ln in (lines or ["[demucs] 提取音频", "[demucs] 分离"]):
        body.append(f"echo {json.dumps(ln, ensure_ascii=False)}")
    if final_json is not None:
        body.append("echo " + json.dumps(json.dumps(final_json)))
    body.append(f"exit {exit_code}")
    with open(path, "w") as f:
        f.write("\n".join(body) + "\n")
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TestDemucsSeparator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.video = os.path.join(self.tmp, "v.mp4"); open(self.video, "w").close()
        self.outdir = os.path.join(self.tmp, "out"); os.makedirs(self.outdir, exist_ok=True)
        self.stub = os.path.join(self.tmp, "fake-runner")

    def test_missing_python_bin(self):
        sep = DemucsSeparator(python_bin="/nonexistent/python", runner_path=self.stub)
        _write_stub_runner(self.stub, exit_code=0, final_json={"ok": True})
        ok, res = sep.separate(video_path=self.video, output_dir=self.outdir, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("python", str(res).lower())

    def test_missing_runner(self):
        sep = DemucsSeparator(python_bin="/bin/bash", runner_path="/nonexistent/runner.py")
        ok, res = sep.separate(video_path=self.video, output_dir=self.outdir, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("runner", str(res).lower())

    def test_missing_video(self):
        _write_stub_runner(self.stub, exit_code=0, final_json={"ok": True})
        sep = DemucsSeparator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = sep.separate(video_path="/nope/x.mp4", output_dir=self.outdir, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("不存在", str(res))

    def test_success_parses_json(self):
        v = os.path.join(self.outdir, "demucs_vocals_t1.wav")
        nv = os.path.join(self.outdir, "demucs_no_vocals_t1.wav")
        _write_stub_runner(self.stub, exit_code=0,
                           final_json={"ok": True, "vocals": v, "no_vocals": nv,
                                       "device_used": "mps"},
                           lines=["[demucs] 提取音频"])
        sep = DemucsSeparator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = sep.separate(video_path=self.video, output_dir=self.outdir, task_id="t1")
        self.assertTrue(ok, msg=str(res))
        self.assertEqual(res["vocals"], v)
        self.assertEqual(res["no_vocals"], nv)

    def test_nonzero_exit(self):
        _write_stub_runner(self.stub, exit_code=1,
                           final_json={"ok": False, "error": "demucs 分离失败(mps)"})
        sep = DemucsSeparator(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = sep.separate(video_path=self.video, output_dir=self.outdir, task_id="t1")
        self.assertFalse(ok)
        self.assertIn("分离失败", str(res))

    def test_progress_callback(self):
        _write_stub_runner(self.stub, exit_code=0,
                           final_json={"ok": True, "vocals": "a", "no_vocals": "b"},
                           lines=["提取音频", "分离中", "移动产物"])
        events = []
        sep = DemucsSeparator(python_bin="/bin/bash", runner_path=self.stub)
        sep.separate(video_path=self.video, output_dir=self.outdir, task_id="t1",
                     progress_callback=events.append)
        self.assertTrue(any("分离" in e for e in events))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mac/Y2A-Auto && python -m pytest tests/test_demucs_separator.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'modules.demucs_separator'`

- [ ] **Step 3: Write the adapter**

Create `modules/demucs_separator.py`:
```python
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DemucsSeparator —— subprocess 适配器,调 modules/demucs_runner.py(用 asr-venv python)。

与 WhisperXAsr 同构:逐行解析 stdout 进度 + 末行 JSON 结果。
"""
import json
import logging
import os
import subprocess


class DemucsSeparator:
    def __init__(self, python_bin: str, runner_path: str):
        self.python_bin = python_bin
        self.runner_path = runner_path
        self.logger = None
        self.task_id = None

    def _log(self, msg):
        if self.logger:
            self.logger.info(msg)
        else:
            logging.getLogger("demucs_separator").info(msg)

    def separate(self, *, video_path, output_dir, task_id,
                 model="htdemucs_ft", device="mps", stems=2,
                 progress_callback=None, timeout=7200):
        """返回 (True, {ok, vocals, no_vocals, ...}) 或 (False, error_msg)。"""
        self.task_id = task_id
        if not self.python_bin or not os.path.isfile(self.python_bin):
            return False, f"asr-venv python 未安装或路径无效: {self.python_bin}"
        if not self.runner_path or not os.path.isfile(self.runner_path):
            return False, f"demucs runner 不存在: {self.runner_path}"
        if not video_path or not os.path.exists(video_path):
            return False, f"视频文件不存在: {video_path}"

        cmd = [self.python_bin, self.runner_path,
               "--video", video_path,
               "--output-dir", output_dir,
               "--task-id", str(task_id),
               "--model", str(model or "htdemucs_ft"),
               "--device", str(device or "mps"),
               "--stems", str(stems or 2)]
        self._log(f"调用 demucs runner: {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1, env=os.environ.copy())
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
            return False, f"demucs 超时({timeout}s)"
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

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mac/Y2A-Auto && python -m pytest tests/test_demucs_separator.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
cd /Users/mac/Y2A-Auto
git add modules/demucs_separator.py tests/test_demucs_separator.py
git commit -m "feat(demucs): DemucsSeparator subprocess adapter(照抄 WhisperXAsr 骨架)+ 单测"
```

---

## Task 4: task_manager 接线（新 stage + 状态 + 软失败方法 + 门控调用）

**Files:**
- Modify: `modules/task_manager.py`
  - `TASK_STATES`（约 L338-354）新增 `AUDIO_SEPARATING`
  - `PROCESSING_STATES`（约 L357-369）新增该状态
  - `PIPELINE_STAGE_*` 常量（约 L383）+ `PIPELINE_STAGE_ORDER`（约 L387-396）
  - 新方法 `_run_remaster_demucs`（`_run_remaster_asr` 之后，约 L3103）
  - process_task 门控块（约 L2455-2462）追加调用

- [ ] **Step 1: 新增 TASK_STATES 与 PROCESSING_STATES 条目**

In `modules/task_manager.py`, in `TASK_STATES` dict, after the line:
```python
    'ASR_TRANSCRIBING': 'asr_transcribing',  # 语音转写中
```
add:
```python
    'AUDIO_SEPARATING': 'audio_separating',  # 音轨分离中(Demucs)
```

In `PROCESSING_STATES`, after the line:
```python
    TASK_STATES['ASR_TRANSCRIBING'],
```
add:
```python
    TASK_STATES['AUDIO_SEPARATING'],
```

- [ ] **Step 2: 新增 pipeline stage 常量 + 插入 ORDER**

Find:
```python
PIPELINE_STAGE_REMASTER_ASR = 'remaster_asr'
PIPELINE_STAGE_TRANSLATE_SUBTITLE = 'translate_subtitle'
```
Insert between them:
```python
PIPELINE_STAGE_REMASTER_DEMUCS = 'remaster_demucs'
```

Then in `PIPELINE_STAGE_ORDER`, find:
```python
    PIPELINE_STAGE_REMASTER_ASR,
    PIPELINE_STAGE_TRANSLATE_SUBTITLE,
```
Insert between them:
```python
    PIPELINE_STAGE_REMASTER_DEMUCS,
```

- [ ] **Step 3: 写 `_run_remaster_demucs` 方法**

In `modules/task_manager.py`, immediately after `_run_remaster_asr` returns (just before `def _translate_subtitle`), insert:
```python
    def _run_remaster_demucs(self, task_id, task_logger):
        """AI 重制管线:Demucs 本地音轨分离,产出 vocals/no_vocals 两条 wav 落盘。

        只产中间产物落盘,不写任何下游业务字段(与 _run_remaster_asr 边界一致);
        供未来 TTS 配音子项目消费。软失败:失败不阻断 ASR/翻译/上传。
        """
        from modules.demucs_separator import DemucsSeparator

        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return False
        video_path = task.get('video_path_local', '')
        if not video_path or not os.path.exists(video_path):
            task_logger.warning("视频文件缺失,跳过音轨分离")
            return False

        python_bin = str(self.config.get('DEMUCS_PYTHON', '') or '').strip()
        runner_path = str(self.config.get('DEMUCS_RUNNER', 'modules/demucs_runner.py') or '').strip()
        if not runner_path or not os.path.isabs(runner_path):
            runner_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), runner_path)

        task_dir = os.path.join(DOWNLOADS_DIR, task_id)
        os.makedirs(task_dir, exist_ok=True)

        prev_status = task.get('status')
        update_task(task_id, status=TASK_STATES['AUDIO_SEPARATING'])
        task_logger.info(f"重制管线:调用 Demucs({self.config.get('DEMUCS_MODEL_NAME', 'htdemucs_ft')}/{self.config.get('DEMUCS_DEVICE', 'mps')}),输出目录 {task_dir}")

        sep = DemucsSeparator(python_bin=python_bin, runner_path=runner_path)
        sep.logger = task_logger
        ok, res = sep.separate(
            video_path=video_path, output_dir=task_dir, task_id=task_id,
            model=str(self.config.get('DEMUCS_MODEL_NAME', 'htdemucs_ft') or 'htdemucs_ft'),
            device=str(self.config.get('DEMUCS_DEVICE', 'mps') or 'mps'),
            stems=_as_int(self.config.get('DEMUCS_STEMS', 2), 2, minimum=2),
            progress_callback=lambda t: task_logger.info(f"[demucs] {t}"),
            timeout=_as_int(self.config.get('DEMUCS_TIMEOUT_SECONDS', 7200), 7200, minimum=60),
        )
        if ok:
            # 只产中间产物(vocals/no_vocals wav 落盘在 task_dir),不写任何下游字段,
            # 供后续 TTS 配音子项目刻意接入。见 spec §1。
            update_task(task_id, demucs_warning_message=None, status=prev_status)
            task_logger.info(f"音轨分离完成: {res}")
            return True
        task_logger.error(f"音轨分离失败: {res}")
        update_task(task_id, demucs_warning_message=f"demucs: {res}", status=prev_status)
        return False
```

- [ ] **Step 4: process_task 门控块追加调用**

Find the REMASTER gate block (around L2455):
```python
                if _as_bool(self.config.get('REMASTER_PIPELINE_ENABLED', False)):
                    self._run_remaster_asr(task_id, task_logger)
```
Change to:
```python
                if _as_bool(self.config.get('REMASTER_PIPELINE_ENABLED', False)):
                    self._run_remaster_asr(task_id, task_logger)
                    self._run_remaster_demucs(task_id, task_logger)
```

- [ ] **Step 5: 验证 import + 常量/状态/顺序 无语法错**

Run:
```bash
cd /Users/mac/Y2A-Auto && python -c "
import modules.task_manager as tm
assert tm.TASK_STATES['AUDIO_SEPARATING'] == 'audio_separating'
assert tm.PIPELINE_STAGE_REMASTER_DEMUCS == 'remaster_demucs'
order = tm.PIPELINE_STAGE_ORDER
assert order.index(tm.PIPELINE_STAGE_REMASTER_ASR) < order.index(tm.PIPELINE_STAGE_REMASTER_DEMUCS) < order.index(tm.PIPELINE_STAGE_TRANSLATE_SUBTITLE)
assert tm.TASK_STATES['AUDIO_SEPARATING'] in tm.PROCESSING_STATES
assert hasattr(tm.TaskManager, '_run_remaster_demucs')
print('OK')
"
```
Expected: `OK`

- [ ] **Step 6: 跑全测试确认无回归**

Run: `cd /Users/mac/Y2A-Auto && python -m pytest tests/ -q`
Expected: 全绿（含既有 whisperx/glossary 测试 + 新增 demucs 测试）

- [ ] **Step 7: Commit**

```bash
cd /Users/mac/Y2A-Auto
git add modules/task_manager.py
git commit -m "feat(demucs): task_manager 新增 remaster_demucs stage + 软失败接线(门控 REMASTER)"
```

---

## Task 5: 真实冒烟 + 收尾

> Task 0 已把权重下好；本任务用真实短视频验证端到端分离，并做全分支复审。

**Files:** 无（验证 + 收尾）

- [ ] **Step 1: 找一段真实短视频**

用既有 downloads 里任一已下载视频，或临时造 5 秒带声测试片：
```bash
cd /Users/mac/Y2A-Auto
ffmpeg -y -f lavfi -i "sine=frequency=440:duration=5" -f lavfi -i "testsrc=size=320x240:rate=25:duration=5" -shortest -c:v libx264 -c:a aac /tmp/demucs_smoke.mp4
```
Expected: `/tmp/demucs_smoke.mp4` 生成。

- [ ] **Step 2: 直接跑 runner 验证端到端**

Run:
```bash
cd /Users/mac/Y2A-Auto && /Users/mac/asr-venv/bin/python modules/demucs_runner.py --video /tmp/demucs_smoke.mp4 --output-dir /tmp/demucs_out --task-id smoke --device mps; echo "exit=$?"
```
Expected: 末行 `{"ok": true, "vocals": "/tmp/demucs_out/demucs_vocals_smoke.wav", "no_vocals": "/tmp/demucs_out/demucs_no_vocals_smoke.wav", "device_used": "mps"}`（或 `"device_used": "cpu"` 若 MPS 降级），`exit=0`。两个 wav 存在。

- [ ] **Step 3: 校验产物时长与格式**

Run:
```bash
for f in /tmp/demucs_out/demucs_vocals_smoke.wav /tmp/demucs_out/demucs_no_vocals_smoke.wav; do
  ffprobe -v error -show_entries stream=sample_rate,channels -show_entries format=duration -of default=noprint_wrappers=1 "$f"
done
```
Expected: 每个 wav `duration≈5s`、`sample_rate=44100`、`channels=2`。

- [ ] **Step 4: 清理冒烟产物**

Run:
```bash
rm -rf /tmp/demucs_smoke.mp4 /tmp/demucs_out
```
Expected: 清理完成。

- [ ] **Step 5: 全分支复审（opus）**

Dispatch a code-review over the whole Demucs subproject diff (config keys, runner, adapter, task_manager 接线). 重点核对：
- 软失败：Demucs 失败绝不 raise、不改动除 `demucs_warning_message`/`status` 外任何字段
- 只落盘、不写下游字段（对齐 WhisperX 边界）
- `REMASTER_PIPELINE_ENABLED=False` 时行为字节级不变（门控块内才调用）
- runner MPS→CPU 兜底逻辑正确、workdir/tmp.wav 无论成败都清理（finally）
修掉复审发现的问题（若有），改完重跑 `python -m pytest tests/ -q` 确认全绿。

- [ ] **Step 6: 合并 main**

按项目既有合并方式（前两个子项目的 squash/merge 惯例）把分支并入 main：
```bash
cd /Users/mac/Y2A-Auto
git log --oneline -8   # 确认 demucs 提交齐整
# 按既有惯例合并到 main
```
Expected: Demucs 子项目并入 main，工作树干净。

---

## Self-Review 结果（写计划时自查）

**1. Spec coverage：**
- spec §1 边界（只落盘不接下游）→ Task 4 Step 3 注释 + 方法只写 `demucs_warning_message` ✓
- spec §2 架构（runner + adapter + task_manager 三层）→ Task 2/3/4 ✓
- spec §3 runner（ffmpeg 44.1k 立体声、`python -m demucs`、收产物、清理、MPS 兜底、stdout JSON、退出码）→ Task 2 ✓
- spec §4 adapter（照抄 WhisperXAsr、返回 `(bool, dict|str)`）→ Task 3 ✓
- spec §5 接线（新常量、新状态、软失败方法、process_task 门控）→ Task 4 ✓
- spec §6 config（6 键 + 默认值 + 复用总开关）→ Task 1 ✓
- spec §7 测试（config 单测 + adapter 单测）→ Task 1/3 ✓
- spec §8 环境准备 + 真实冒烟 + 验收 → Task 0/5 ✓

**2. Placeholder scan：** 无 TBD/TODO；每个代码步含完整代码；每个命令含期望输出 ✓

**3. Type consistency：**
- adapter 方法名 `separate`（Task 3 定义 → Task 4 调用一致）✓
- runner CLI 参数 `--video/--output-dir/--task-id/--model/--device/--stems`（Task 2 定义 → Task 3 adapter cmd 一致 → Task 5 冒烟一致）✓
- 产物文件名 `demucs_vocals_<task_id>.wav` / `demucs_no_vocals_<task_id>.wav`（Task 2 runner 落盘 → Task 3 测试断言 → Task 5 冒烟校验一致）✓
- config 键 `DEMUCS_PYTHON/DEMUCS_RUNNER/DEMUCS_MODEL_NAME/DEMUCS_DEVICE/DEMUCS_STEMS/DEMUCS_TIMEOUT_SECONDS`（Task 1 定义 → Task 4 读取一致）✓
- 状态 `AUDIO_SEPARATING`、stage `PIPELINE_STAGE_REMASTER_DEMUCS`（Task 4 内部一致）✓
- 依赖既有 helper `_as_int`、`_as_bool`、`DOWNLOADS_DIR`、`get_task`、`update_task`（均为 task_manager 现有，_run_remaster_asr 已在用）✓
