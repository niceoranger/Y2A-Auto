# WhisperX ASR + 字级对齐 实现计划（AI 重制管线 ①）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Y2A 内加一条独立的 AI 重制管线,第一步用本地 WhisperX(large-v3, CPU int8)对视频做 ASR + wav2vec2 字级对齐,产出字级时间戳 SRT。

**Architecture:** subprocess 隔离——`whisperx_runner.py` 在独立 `/Users/mac/asr-venv`(装 whisperx 重依赖)跑,Y2A 通过 `WhisperXAsr` 适配器 subprocess 调它。与现有 `speech_recognition.py` 管线独立并行,由 `REMASTER_PIPELINE_ENABLED` 开关触发,插在 `DOWNLOAD_VIDEO` 之后、`TRANSLATE_SUBTITLE` 之前。

**Tech Stack:** Python 3.12、WhisperX(Faster-Whisper + Wav2Vec2)、ffmpeg、unittest(项目测试风格)、subprocess。

**参考 spec:** `docs/superpowers/specs/2026-07-10-whisperx-asr-design.md`

**开始前:** 从 `main` 切出实现分支 `feat/whisperx-asr`。所有任务在该分支提交。

**测试运行约定:** 项目用 `unittest`(无 pytest、无 conftest)。新测试文件自带 `if __name__ == '__main__': unittest.main()` 并在文件头把项目根加入 `sys.path`。统一运行:
```bash
cd /Users/mac/Y2A-Auto && .venv/bin/python tests/<test_file>.py -v
```
注意:Y2A 的 `.venv` **不装 whisperx**;whisperx 装在独立 `/Users/mac/asr-venv`,只有 `whisperx_runner.py` 用 asr-venv 跑。

---

## 文件结构总览

| 文件 | 责任 | 类型 |
|------|------|------|
| `/Users/mac/asr-venv/` | 独立 venv 装 whisperx(torch/CTranslate2/transformers) | 新增(环境) |
| `modules/whisperx_runner.py` | CLI:`import whisperx`,ASR + 字级对齐 + 渲染 SRT | 新增 |
| `modules/whisperx_asr.py` | `WhisperXAsr` subprocess 适配器 | 新增 |
| `tests/test_whisperx_asr.py` | 适配器 stub 单测 | 新增 |
| `modules/config_manager.py` | `REMASTER_PIPELINE_ENABLED` + `WHISPERX_*` 默认键 | 改 |
| `modules/task_manager.py` | `STAGE_REMASTER_ASR` 常量 + `_run_remaster_asr()` + pipeline 接入 | 改 |

**runner ↔ 适配器 契约**(适配器依赖这个,必须先定):
```
python whisperx_runner.py --video <p> --output <srt> [--language auto] [--model large-v3] [--device cpu] [--compute-type int8] [--batch-size 16]
```
- stdout:进度文本行 + **末行 JSON** `{"ok": true, "srt": "<path>"}` 或 `{"ok": false, "error": "..."}`
- 退出码:0 成功 / 非 0 失败

---

## Task 1: 装 asr-venv + whisperx(环境)

**Files:** `/Users/mac/asr-venv/`(环境,非代码)

- [ ] **Step 1: 创建独立 venv**
```bash
/opt/homebrew/bin/python3.12 -m venv /Users/mac/asr-venv
/Users/mac/asr-venv/bin/python -m pip install --upgrade pip -q
```

- [ ] **Step 2: 装 whisperx(国内用清华镜像)**

> whisperx 依赖 torch + ctranslate2 + transformers + tokenizers 等,体积大。用 Tsinghua mirror(直连 pypi 之前在装 mlx-lm 时超时过)。
```bash
/Users/mac/asr-venv/bin/pip install whisperx -i https://pypi.tuna.tsinghua.edu.cn/simple
```
Expected: `Successfully installed whisperx ...` + 一堆依赖。若 ctranslate2 wheel 在 arm64 报错,改装 `ctranslate2` 单独指定版本后再装 whisperx。

- [ ] **Step 3: 验证 import**
```bash
/Users/mac/asr-venv/bin/python -c "import whisperx; print('whisperx OK')"
```
Expected: `whisperx OK`(模型此时尚未下载,import 成功即可)。

- [ ] **Step 4: 不提交(环境,非代码)**

---

## Task 2: whisperx_runner.py(CLI + ASR + 字级对齐 + SRT 渲染)

**Files:**
- Create: `modules/whisperx_runner.py`

- [ ] **Step 1: 写 runner**

Create `modules/whisperx_runner.py`:
```python
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
```

> **实现注记:** WhisperX 的 `load_model/transcribe/load_align_model/align` 签名以**装好的 whisperx 实际版本为准**。Task 1 装完后用 `/Users/mac/asr-venv/bin/python -c "import whisperx, inspect; print(inspect.signature(whisperx.load_model))"` 核对参数名;若不符(如 `compute_type` 改名),微调 runner 后再进 Task 6 冒烟校准。

- [ ] **Step 2: 语法校验**
```bash
cd /Users/mac/Y2A-Auto && .venv/bin/python -c "import ast; ast.parse(open('modules/whisperx_runner.py').read()); print('syntax OK')"
```
Expected: `syntax OK`

- [ ] **Step 3: CLI --help(用 asr-venv python,验证 argparse + import 链)**
```bash
/Users/mac/asr-venv/bin/python /Users/mac/Y2A-Auto/modules/whisperx_runner.py --help
```
Expected: 打印 usage(含 --video/--output/--language/--model 等)。

- [ ] **Step 4: 提交**
```bash
git -C /Users/mac/Y2A-Auto add modules/whisperx_runner.py
git -C /Users/mac/Y2A-Auto commit -m "feat(remaster): whisperx_runner CLI(ASR+字级对齐+SRT 渲染)"
```

---

## Task 3: WhisperXAsr 适配器 + stub 单测

**Files:**
- Create: `modules/whisperx_asr.py`
- Test: `tests/test_whisperx_asr.py`

- [ ] **Step 1: 写失败测试(stub runner)**

Create `tests/test_whisperx_asr.py`:
```python
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.whisperx_asr import WhisperXAsr


def _write_stub_runner(path, exit_code=0, final_json=None, lines=None):
    body = ["#!/usr/bin/env bash", "echo '[stub-runner] invoked:' \"$@\" >&2"]
    for ln in (lines or ["[whisperx] 加载模型", "[whisperx] 转写"]):
        body.append(f"echo {json.dumps(ln)}")
    if final_json is not None:
        body.append("echo " + json.dumps(json.dumps(final_json)))
    body.append(f"exit {exit_code}")
    with open(path, "w") as f:
        f.write("\n".join(body) + "\n")
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TestWhisperXAsr(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.video = os.path.join(self.tmp, "v.mp4"); open(self.video, "w").close()
        self.out = os.path.join(self.tmp, "out.srt")
        self.stub = os.path.join(self.tmp, "fake-runner")

    def test_missing_runner(self):
        asr = WhisperXAsr(python_bin="/nonexistent/python", runner_path="/nonexistent/runner.py")
        ok, res = asr.transcribe(video_path=self.video, output_srt_path=self.out)
        self.assertFalse(ok)
        self.assertIn("runner", str(res).lower())

    def test_success_parses_json(self):
        _write_stub_runner(self.stub, exit_code=0,
                           final_json={"ok": True, "srt": self.out, "segments": 12},
                           lines=["[whisperx] 加载模型"])
        with open(self.out, "w") as f:
            f.write("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        asr = WhisperXAsr(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = asr.transcribe(video_path=self.video, output_srt_path=self.out)
        self.assertTrue(ok, msg=str(res))
        self.assertEqual(res["srt"], self.out)
        self.assertEqual(res["segments"], 12)

    def test_nonzero_exit(self):
        _write_stub_runner(self.stub, exit_code=1,
                           final_json={"ok": False, "error": "model missing"})
        asr = WhisperXAsr(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = asr.transcribe(video_path=self.video, output_srt_path=self.out)
        self.assertFalse(ok)
        self.assertIn("model missing", str(res))

    def test_missing_video(self):
        _write_stub_runner(self.stub, exit_code=0, final_json={"ok": True, "srt": self.out})
        asr = WhisperXAsr(python_bin="/bin/bash", runner_path=self.stub)
        ok, res = asr.transcribe(video_path="/nope/x.mp4", output_srt_path=self.out)
        self.assertFalse(ok)
        self.assertIn("不存在", str(res))

    def test_progress_callback(self):
        _write_stub_runner(self.stub, exit_code=0,
                           final_json={"ok": True, "srt": self.out},
                           lines=["加载模型", "转写中", "对齐"])
        events = []
        asr = WhisperXAsr(python_bin="/bin/bash", runner_path=self.stub)
        asr.transcribe(video_path=self.video, output_srt_path=self.out,
                       progress_callback=events.append)
        self.assertTrue(any("转写" in e or "对齐" in e for e in events))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**
```bash
cd /Users/mac/Y2A-Auto && .venv/bin/python tests/test_whisperx_asr.py -v
```
Expected: FAIL `ModuleNotFoundError: No module named 'modules.whisperx_asr'`

- [ ] **Step 3: 写实现**

Create `modules/whisperx_asr.py`:
```python
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WhisperXAsr —— subprocess 适配器,调 modules/whisperx_runner.py(用 asr-venv python)。

与 SauPlatformUploader 同构:逐行解析 stdout 进度 + 末行 JSON 结果。
"""
import json
import logging
import os
import subprocess


class WhisperXAsr:
    def __init__(self, python_bin: str, runner_path: str):
        self.python_bin = python_bin
        self.runner_path = runner_path
        self.logger = None
        self.task_id = None

    def _log(self, msg):
        if self.logger:
            self.logger.info(msg)
        else:
            logging.getLogger("whisperx_asr").info(msg)

    def transcribe(self, *, video_file_path, output_srt_path, language="auto",
                   model="large-v3", device="cpu", compute_type="int8",
                   batch_size=16, task_id=None, progress_callback=None, timeout=7200):
        """返回 (True, {ok, srt, segments, ...}) 或 (False, error_msg)。"""
        self.task_id = task_id
        if not self.python_bin or not os.path.isfile(self.python_bin):
            return False, f"asr-venv python 未安装或路径无效: {self.python_bin}"
        if not self.runner_path or not os.path.isfile(self.runner_path):
            return False, f"whisperx runner 不存在: {self.runner_path}"
        if not video_file_path or not os.path.exists(video_file_path):
            return False, f"视频文件不存在: {video_file_path}"

        cmd = [self.python_bin, self.runner_path,
               "--video", video_file_path,
               "--output", output_srt_path,
               "--language", str(language or "auto"),
               "--model", str(model or "large-v3"),
               "--device", str(device or "cpu"),
               "--compute-type", str(compute_type or "int8"),
               "--batch-size", str(batch_size or 16)]
        self._log(f"调用 whisperx runner: {' '.join(cmd)}")
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
            return False, f"whisperx 超时({timeout}s)"
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
cd /Users/mac/Y2A-Auto && .venv/bin/python tests/test_whisperx_asr.py -v
```
Expected: PASS(5 tests)

- [ ] **Step 5: 提交**
```bash
git -C /Users/mac/Y2A-Auto add modules/whisperx_asr.py tests/test_whisperx_asr.py
git -C /Users/mac/Y2A-Auto commit -m "feat(remaster): WhisperXAsr subprocess 适配器 + stub 单测"
```

---

## Task 4: config_manager 新键

**Files:**
- Modify: `modules/config_manager.py`(DEFAULT_CONFIG 字典)
- Test: `tests/test_whisperx_config.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_whisperx_config.py`:
```python
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.config_manager import DEFAULT_CONFIG


class TestWhisperXConfig(unittest.TestCase):
    def test_defaults_present(self):
        self.assertIn("REMASTER_PIPELINE_ENABLED", DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["REMASTER_PIPELINE_ENABLED"])
        for k in ["WHISPERX_ASR_PYTHON", "WHISPERX_RUNNER", "WHISPERX_MODEL_NAME",
                  "WHISPERX_DEVICE", "WHISPERX_COMPUTE_TYPE", "WHISPERX_BATCH_SIZE",
                  "WHISPERX_TIMEOUT_SECONDS"]:
            self.assertIn(k, DEFAULT_CONFIG)
        self.assertEqual(DEFAULT_CONFIG["WHISPERX_MODEL_NAME"], "large-v3")
        self.assertEqual(DEFAULT_CONFIG["WHISPERX_DEVICE"], "cpu")
        self.assertEqual(DEFAULT_CONFIG["WHISPERX_COMPUTE_TYPE"], "int8")
        self.assertEqual(DEFAULT_CONFIG["WHISPERX_ASR_PYTHON"], "/Users/mac/asr-venv/bin/python")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**
```bash
cd /Users/mac/Y2A-Auto && .venv/bin/python tests/test_whisperx_config.py -v
```
Expected: FAIL(`REMASTER_PIPELINE_ENABLED` not in DEFAULT_CONFIG)

- [ ] **Step 3: 加默认键**

In `modules/config_manager.py`, find the line:
```python
    "UPLOAD_TARGET_DEFAULT": "acfun",  # [已废弃,仅向后兼容] 旧单枚举:acfun|bilibili|both
```
Immediately AFTER it(在 UPLOAD_TARGETS/sau 键那一块附近),add:
```python
    # AI 重制管线(Phase 3)——WhisperX ASR 子系统
    "REMASTER_PIPELINE_ENABLED": False,  # 重制管线总开关,默认关
    "WHISPERX_ASR_PYTHON": "/Users/mac/asr-venv/bin/python",  # runner 解释器(独立 venv)
    "WHISPERX_RUNNER": "modules/whisperx_runner.py",          # runner 脚本
    "WHISPERX_MODEL_NAME": "large-v3",       # ASR 模型(质量优先)
    "WHISPERX_DEVICE": "cpu",                # Apple Silicon 走 CPU(CTranslate2 不支持 MPS)
    "WHISPERX_COMPUTE_TYPE": "int8",         # CPU 量化
    "WHISPERX_BATCH_SIZE": 16,               # 转写/对齐 batch
    "WHISPERX_TIMEOUT_SECONDS": 7200,        # 长视频 CPU 跑慢,超时 2h
```

- [ ] **Step 4: 跑测试确认通过 + 配置加载不报错**
```bash
cd /Users/mac/Y2A-Auto && .venv/bin/python tests/test_whisperx_config.py -v
cd /Users/mac/Y2A-Auto && .venv/bin/python -c "from modules.config_manager import load_config; c=load_config(); print(c['REMASTER_PIPELINE_ENABLED'], c['WHISPERX_MODEL_NAME'])"
```
Expected: 测试 PASS;第二行打印 `False large-v3`,无异常。

- [ ] **Step 5: 提交**
```bash
git -C /Users/mac/Y2A-Auto add modules/config_manager.py tests/test_whisperx_config.py
git -C /Users/mac/Y2A-Auto commit -m "feat(config): 新增 REMASTER_PIPELINE_ENABLED + WHISPERX_* 默认键"
```

---

## Task 5: task_manager stage + _run_remaster_asr + pipeline 接入

**Files:**
- Modify: `modules/task_manager.py`(stage 常量 L382-393、process_task L2442-2460)

- [ ] **Step 1: 加 stage 常量 + ORDER**

Find(L382 附近):
```python
PIPELINE_STAGE_DOWNLOAD_VIDEO = 'download_video'
```
After it add:
```python
PIPELINE_STAGE_REMASTER_ASR = 'remaster_asr'
```

Find `PIPELINE_STAGE_ORDER`(L386)里的:
```python
    PIPELINE_STAGE_DOWNLOAD_VIDEO,
    PIPELINE_STAGE_TRANSLATE_SUBTITLE,
```
Replace with(在两者之间插入重制 ASR stage):
```python
    PIPELINE_STAGE_DOWNLOAD_VIDEO,
    PIPELINE_STAGE_REMASTER_ASR,
    PIPELINE_STAGE_TRANSLATE_SUBTITLE,
```

- [ ] **Step 2: 加 `_run_remaster_asr` 方法**

在 `_translate_subtitle` 方法之前(约 L3043),插入新方法(类内缩进):
```python
    def _run_remaster_asr(self, task_id, task_logger):
        """AI 重制管线第一步:WhisperX 本地 ASR + 字级对齐,产出字级 SRT。"""
        from modules.whisperx_asr import WhisperXAsr

        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return False
        video_path = task.get('video_path_local', '')
        if not video_path or not os.path.exists(video_path):
            task_logger.warning("视频文件缺失,跳过重制 ASR")
            return False

        python_bin = str(self.config.get('WHISPERX_ASR_PYTHON', '') or '').strip()
        runner_path = str(self.config.get('WHISPERX_RUNNER', 'modules/whisperx_runner.py') or '').strip()
        if not runner_path or not os.path.isabs(runner_path):
            runner_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), runner_path)

        task_dir = os.path.join(DOWNLOADS_DIR, task_id)
        os.makedirs(task_dir, exist_ok=True)
        out_srt = os.path.join(task_dir, f"asr_whisperx_{task_id}.srt")

        update_task(task_id, status=TASK_STATES.get('ASR_TRANSCRIBING', TASK_STATES['PROCESSING']))
        task_logger.info(f"重制管线:调用 WhisperX(large-v3/CPU),输出 {out_srt}")

        asr = WhisperXAsr(python_bin=python_bin, runner_path=runner_path)
        asr.logger = task_logger
        ok, res = asr.transcribe(
            video_file_path=video_path, output_srt_path=out_srt,
            language=str(self.config.get('SUBTITLE_SOURCE_LANGUAGE', 'auto') or 'auto'),
            model=str(self.config.get('WHISPERX_MODEL_NAME', 'large-v3') or 'large-v3'),
            device=str(self.config.get('WHISPERX_DEVICE', 'cpu') or 'cpu'),
            compute_type=str(self.config.get('WHISPERX_COMPUTE_TYPE', 'int8') or 'int8'),
            batch_size=int(self.config.get('WHISPERX_BATCH_SIZE', 16) or 16),
            task_id=task_id,
            progress_callback=lambda t: task_logger.info(f"[whisperx] {t}"),
            timeout=int(self.config.get('WHISPERX_TIMEOUT_SECONDS', 7200) or 7200),
        )
        if ok:
            update_task(task_id, subtitle_path_original=out_srt,
                        subtitle_language_detected=None, asr_warning_message=None)
            task_logger.info(f"重制 ASR 完成: {res}")
            return True
        task_logger.error(f"重制 ASR 失败: {res}")
        update_task(task_id, asr_warning_message=f"whisperx: {res}")
        return False
```

- [ ] **Step 3: 接入 process_task 主调度**

In `process_task`(L2442-2451 附近,DOWNLOAD_VIDEO 完成后)。Find:
```python
            if PIPELINE_STAGE_DOWNLOAD_VIDEO in completed_stages:
                ...
                self._download_video_file(task_id, task['youtube_url'], task_logger)
                ...
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_DOWNLOAD_VIDEO)
```
Immediately AFTER that block(DOWNLOAD_VIDEO 标记完成之后、`_translate_subtitle` 调用 L2460 之前),insert:
```python
            if PIPELINE_STAGE_DOWNLOAD_VIDEO in completed_stages and \
                    PIPELINE_STAGE_REMASTER_ASR not in completed_stages and \
                    _as_bool(self.config.get('REMASTER_PIPELINE_ENABLED', False)):
                try:
                    self._run_remaster_asr(task_id, task_logger)
                except Exception as e:
                    task_logger.error(f"重制 ASR 异常: {e}")
                completed_stages = _mark_stage_done(task_id, completed_stages, PIPELINE_STAGE_REMASTER_ASR)
```
> 读 process_task 实际代码确认 anchor(download_video 完成块与 translate_subtitle 调用之间)。若 anchor 不匹配,报 NEEDS_CONTEXT。

- [ ] **Step 4: 语法 + 导入校验**
```bash
cd /Users/mac/Y2A-Auto && .venv/bin/python -c "import modules.task_manager; print('import OK')"
```
Expected: `import OK`

- [ ] **Step 5: 回归**
```bash
cd /Users/mac/Y2A-Auto && .venv/bin/python tests/test_task_manager_delete_task_files.py -v 2>&1 | tail -3
```
Expected: PASS(3 tests)。

- [ ] **Step 6: 提交**
```bash
git -C /Users/mac/Y2A-Auto add modules/task_manager.py
git -C /Users/mac/Y2A-Auto commit -m "feat(remaster): STAGE_REMASTER_ASR + _run_remaster_asr + pipeline 接入"
```

---

## Task 6: 真实短视频冒烟

**Files:** 无代码改动;部署验证。

- [ ] **Step 1: 用 demo 视频跑 runner(直接 CLI,先验证 runner 本身)**
```bash
/Users/mac/asr-venv/bin/python /Users/mac/Y2A-Auto/modules/whisperx_runner.py \
  --video /Users/mac/social-auto-upload/videos/demo.mp4 \
  --output /tmp/whisperx_smoke.srt \
  --language auto --model large-v3 --device cpu --compute-type int8 --batch-size 16
```
Expected: 首次运行下载 large-v3 + wav2vec2 模型(~3GB,国内可能要走 HF mirror `HF_ENDPOINT=https://hf-mirror.com`);最终打印末行 `{"ok": true, "srt": "/tmp/whisperx_smoke.srt", "segments": N}`;`/tmp/whisperx_smoke.srt` 含字级时间戳。
> 若 WhisperX API 签名不符(load_model/transcribe/align 参数名变了),回到 Task 2 Step 1 按实际签名微调 runner,重跑。

- [ ] **Step 2: 抽查 SRT 时间戳精度**
```bash
head -20 /tmp/whisperx_smoke.srt
```
Expected: 合法 SRT(序号 + `HH:MM:SS,mmm --> HH:MM:SS,mmm` + 文本)。字级对齐成功的话,单条字幕时长应较短(几秒内,对应一句/一段)。

- [ ] **Step 3: 验证 Y2A 适配器能调通(可选,通过 stub 已覆盖;真实端到端在应用里)**
```bash
cd /Users/mac/Y2A-Auto && .venv/bin/python -c "
from modules.whisperx_asr import WhisperXAsr
asr = WhisperXAsr('/Users/mac/asr-venv/bin/python', '/Users/mac/Y2A-Auto/modules/whisperx_runner.py')
ok, res = asr.transcribe(video_file_path='/Users/mac/social-auto-upload/videos/demo.mp4', output_srt_path='/tmp/whisperx_smoke2.srt', progress_callback=print)
print('OK' if ok else 'FAIL', res)
"
```
Expected: `OK {...}`(复用 Step 1 已下载的模型,较快)。

- [ ] **Step 4: 合并回 main**
```bash
git -C /Users/mac/Y2A-Auto checkout main
git -C /Users/mac/Y2A-Auto merge --no-ff feat/whisperx-asr
```

---

## 自审清单(写计划后)

- **Spec 覆盖**:
  - §3.2 runner → Task 2 ✅;适配器 → Task 3 ✅
  - §3.3 模型 large-v3/cpu/int8 → Task 4 config + Task 2 runner ✅
  - §5 配置键 → Task 4 ✅(7 个 WHISPERX_* + REMASTER_PIPELINE_ENABLED 全覆盖)
  - §6 错误处理(模型缺失/超时/对齐失败回退) → runner(Task 2)+ 适配器超时(Task 3)✅
  - §7 测试(stub 单测 + 冒烟) → Task 3 + Task 6 ✅
  - §8 与现有管线独立 → Task 5(REMASTER_PIPELINE_ENABLED 门控,默认关,不影响现有)✅
  - asr-venv 隔离 → Task 1 ✅
- **占位扫描**:Task 2 的"WhisperX API 签名以实际版本为准"是显式校准动作(非模糊需求),Task 6 Step 1 闭环校准;无 TBD/TODO。
- **类型/命名一致**:`WhisperXAsr.transcribe(*, video_file_path, output_srt_path, ...)` 与 Task 3 测试、Task 5 `_run_remaster_asr` 调用一致;runner CLI 参数(`--video/--output/--language/--model/--device/--compute-type/--batch-size`)与适配器构造的 cmd 一致;JSON 末行协议 `{"ok", "srt", "segments"}` 贯穿。
- **已知风险**:WhisperX 的 `load_align_model` 对部分语言(如 ja/ko)可能无对齐模型 → runner 已 try/except 回退段级(Task 2 Step 1);large-v3 CPU 速度若不可接受 → 改 `WHISPERX_MODEL_NAME=medium`(Task 4 已可配)。

---

## 执行交接

计划已保存到 `docs/superpowers/plans/2026-07-10-whisperx-asr.md`。两种执行方式:

1. **Subagent-Driven(推荐)** — 每任务派发独立 subagent,任务间两阶段复核。
2. **Inline Execution** — 本会话批量执行 + 检查点复核。

选哪种?
