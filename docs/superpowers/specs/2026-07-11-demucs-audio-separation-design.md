# Demucs 音轨分离 设计文档

> AI 重制管线第 3 个子项目。从视频中分离人声 / 背景音，为将来 TTS 配音铺路。全本地优先。

**日期**：2026-07-11
**状态**：设计已批准，待写实现计划
**前置**：WhisperX ASR（已合并）、RAG 术语翻译（已合并）

---

## 1. 目标与边界

从任务视频中用 Demucs 分离出**人声（vocals）**与**背景音（no_vocals）**两条音轨，各自落盘为 44.1kHz 立体声 wav，供将来的 TTS 配音子项目消费（配音时人声轨作对齐/替换参考，背景音轨混回）。

**边界（关键）**：本子项目**只产中间产物落盘，不接下游、不写任何 task 业务字段**。管线当前没有配音子项目消费这两个 wav，所以 Demucs 的职责就是"生成好、落盘、等着"。这与 WhisperX 子项目的边界一致（WhisperX 也只落 SRT、不写 `subtitle_path_original`）。

**独立于 ASR**：分离**不喂 ASR**。WhisperX 自带 VAD，吃原始视频音轨即可，不需要 Demucs 预清洗。两者互不依赖，Demucs 失败不影响 ASR / 翻译 / 上传（软失败）。

## 2. 架构

与 WhisperX 完全同构：独立 venv 跑 runner CLI + subprocess adapter。

```
task_manager._run_remaster_demucs()      ← 新 stage, gated by REMASTER_PIPELINE_ENABLED
        │  (Popen + 逐行 stdout 进度 + 末行 JSON 结果 + 超时 kill)
        ▼
modules/demucs_separator.py  (DemucsSeparator adapter, 复用 WhisperXAsr 骨架)
        │  subprocess: asr-venv/bin/python
        ▼
modules/demucs_runner.py  (CLI: ffmpeg 抽 44.1kHz 立体声 wav → demucs htdemucs_ft 2-stem → 落盘)
        │
        ▼
downloads/<task_id>/
    demucs_vocals_<task_id>.wav       ← 人声
    demucs_no_vocals_<task_id>.wav    ← 背景音
```

## 3. demucs_runner.py（CLI，跑在 asr-venv）

跟 `whisperx_runner.py` 同结构。

**参数**：
- `--video`：输入（视频或音频均可）
- `--output-dir`：落盘目录（`downloads/<task_id>/`）
- `--task-id`
- `--model`：默认 `htdemucs_ft`
- `--device`：默认 `mps`
- `--stems`：默认 `2`（2-stem → 传 `--two-stems=vocals`）

**流程**：
1. **ffmpeg 抽音轨** → 44.1kHz 立体声 wav 到临时文件：`ffmpeg -i <video> -vn -ac 2 -ar 44100 <tmp.wav>`。ffmpeg stderr 并入异常（对齐 whisperx_runner）。
2. **调 Demucs**：subprocess `python -m demucs`（官方稳定入口，参数纯字符串，比 import 内部 API 抗版本漂移）。命令：
   `python -m demucs --two-stems=vocals -n htdemucs_ft -d mps --out <workdir> <tmp.wav>`
3. **收产物**：Demucs 输出到 `<workdir>/htdemucs_ft/<tmp_basename>/{vocals,no_vocals}.wav`，移动/重命名到 `downloads/<task_id>/demucs_vocals_<task_id>.wav` 和 `demucs_no_vocals_<task_id>.wav`。
4. **清理** tmp.wav 与 Demucs 中间目录。

**MPS 兜底**：Demucs 在 MPS 上偶发算子不支持。runner 捕获 MPS 失败 → 自动 fallback 到 `-d cpu` 重跑一次，日志标注降级。保证"MPS 加速"不会变成"MPS 崩 → 整个失败"。

**stdout 协议**（对齐 whisperx_runner）：进度行直接打印（Demucs 自带百分比）；末行输出 JSON：
```json
{"ok": true, "vocals": "<path>.wav", "no_vocals": "<path>.wav", "duration_sec": 123.4, "device_used": "mps"}
```
失败：`{"ok": false, "error": "..."}`。

**退出码**：`0` 成功 / `1` 运行失败 / `2` 参数错（对齐 whisperx_runner）。

## 4. DemucsSeparator adapter（modules/demucs_separator.py）

照抄 `WhisperXAsr` 骨架。

```python
class DemucsSeparator:
    def __init__(self, python_bin, runner_path): ...
    def separate(self, *, video_path, output_dir, task_id,
                 model="htdemucs_ft", device="mps", stems=2,
                 progress_callback=None, timeout=7200):
        # 校验 python_bin / runner_path / video_path 存在,各自 (False, msg)
        # Popen(cmd, stdout=PIPE, stderr=STDOUT, text=True, bufsize=1, env=os.environ.copy())
        # 逐行:tail 收集 + progress_callback + 尝试解析末行 JSON
        # wait(timeout) → TimeoutExpired 则 kill()+wait() → (False, 超时)
        # finally: proc.stdout.close()
        # returncode != 0 → (False, 末尾输出)
        # last_json.ok → (True, last_json) 否则 (False, error)
        return (True, {...}) or (False, err_str)
```

接口与 `WhisperXAsr.transcribe` 同构，返回 `(bool, dict | str)`。

## 5. task_manager 接线

**新常量**：
- `PIPELINE_STAGE_REMASTER_DEMUCS = 'remaster_demucs'`，插入 `PIPELINE_STAGE_ORDER`（`remaster_asr` 之后、`translate_subtitle` 之前）
- `TASK_STATES['AUDIO_SEPARATING']`

**`_run_remaster_demucs(task_id, task_logger)`** — 照抄 `_run_remaster_asr`：
- 取 `video_path_local`；缺失 warn 跳过、`return False`（不阻断）
- 读 config：`DEMUCS_PYTHON` / `DEMUCS_RUNNER` / `DEMUCS_MODEL_NAME` / `DEMUCS_DEVICE` / `DEMUCS_STEMS` / `DEMUCS_TIMEOUT_SECONDS`；runner_path 非绝对则拼项目根
- 落盘目录 `downloads/<task_id>/`
- `update_task(status=TASK_STATES['AUDIO_SEPARATING'])`，跑完还原 `status=prev_status`
- **成功不写任何下游字段**，仅 `demucs_warning_message=None`
- 失败：`demucs_warning_message=...`、`status=prev_status`、`return False`（软失败）

**process_task 接线**（`_run_remaster_asr` 之后、`_translate_subtitle` 之前）：
```python
if _as_bool(self.config.get('REMASTER_PIPELINE_ENABLED', False)):
    self._run_remaster_asr(task_id, task_logger)
    self._run_remaster_demucs(task_id, task_logger)   # 新增
```

## 6. config 新键（config_manager.py，DEMUCS_* 块，放 WHISPERX 块之后）

```python
"DEMUCS_PYTHON": "/Users/mac/asr-venv/bin/python",  # 复用 asr-venv
"DEMUCS_RUNNER": "modules/demucs_runner.py",
"DEMUCS_MODEL_NAME": "htdemucs_ft",   # 最高精度(4x 慢于 htdemucs)
"DEMUCS_DEVICE": "mps",               # Apple Silicon 加速, runner 内 CPU 兜底
"DEMUCS_STEMS": 2,                    # 2-stem: vocals / no_vocals
"DEMUCS_TIMEOUT_SECONDS": 7200,
```

总开关**复用 `REMASTER_PIPELINE_ENABLED`**，不新增开关。

## 7. 测试

纯单元测试，不跑真模型（对齐 WhisperX / glossary 风格）：

| 文件 | 覆盖 |
|------|------|
| `tests/test_demucs_config.py` | 6 个 DEMUCS_* 键存在 + 默认值（`htdemucs_ft` / `mps` / `stems=2`） |
| `tests/test_demucs_separator.py` | adapter：python_bin / runner / video 缺失各返回 `(False, ...)`；mock Popen 成功末行 JSON → `(True, dict)`；returncode≠0 → `(False, ...)`；超时 → kill + `(False, ...)` |

runner 真实分离靠第 8 节真实冒烟验证（CLI 逻辑单测价值低）。

## 8. 环境准备 + 真实冒烟 + 收尾

1. **实现前先手动下权重**：`asr-venv` 装 demucs，手动触发 htdemucs_ft 权重下载（4 个 bag 模型）。若 HF 卡就照 WhisperX 老路 curl 镜像（hf-mirror.com）。此步在写代码前完成，避免踩镜像坑。
2. 拿真实短视频跑 `demucs_runner.py`：确认两个 wav 落盘、时长对、MPS 真的用上（或正确降级 CPU）。
3. 全测试过 + opus 全分支复审 → 合并 main。

**验收标准**：
- `REMASTER_PIPELINE_ENABLED=True` 时任务额外产出 `demucs_vocals_<task_id>.wav` / `demucs_no_vocals_<task_id>.wav`，且不影响 ASR / 翻译 / 上传。
- `REMASTER_PIPELINE_ENABLED=False` 时行为字节级不变。

## 9. AI 重制管线进度

- ✅ WhisperX ASR + 字级对齐（已合并）
- ✅ RAG 术语翻译（已合并）
- ⬜ **Demucs 音轨分离（本设计）**
- ⬜ RubberBand 变速 + TTS 配音（将消费本子项目的 vocals/no_vocals）
- ⬜ PaddleOCR 字幕定位
- ⬜ FFmpeg 字幕擦除 + 合成
