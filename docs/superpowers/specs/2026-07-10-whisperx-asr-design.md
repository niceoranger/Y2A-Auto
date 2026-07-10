# WhisperX ASR + 字级对齐 设计文档（AI 重制管线 ①）

- **日期**:2026-07-10
- **状态**:已通过设计评审,待实现
- **关联**:AI 视频重制管线第一子项目(后续 RAG 翻译/变速配音/OCR/字幕擦除合成各自独立 spec)

## 1. 背景

Y2A 现有 ASR 走 OpenAI 兼容 API(`/v1/audio/transcriptions`),但用户的本地 qwopus 是**文本 LLM**,做不了 ASR(需音频模型)。本子项目引入 **WhisperX**(Faster-Whisper + Wav2Vec2)做本地 ASR + **字级时间戳对齐**,作为"AI 重制管线"的第一块基础——后续的翻译对齐、配音时长同步都依赖字级时间戳。

**本子项目的完成定义**:WhisperX 能对本地视频产出字级时间戳 SRT + 重制管线骨架入口就位。**不改变最终上传的视频**(那要等后续翻译/配音/合成子项目),只产出字级原文 SRT 作为下游输入。

## 2. 范围

### IN
- 新增 `modules/whisperx_runner.py`(CLI,`import whisperx`,ASR + 字级对齐,输出 SRT)
- 新增 `modules/whisperx_asr.py`(Y2A subprocess 适配器)
- `task_manager` 新 pipeline stage `STAGE_REMASTER_ASR` + `_run_remaster_asr()`
- `config_manager` 新键(`WHISPERX_*`、`REMASTER_PIPELINE_ENABLED`)
- 独立 venv `/Users/mac/asr-venv` 装 whisperx
- stub 单测 + 真实短视频冒烟

### OUT
- 翻译 / 变速配音 / OCR / 字幕擦除合成(后续子项目,各自 spec)
- speaker diarization(单人搬运,YAGNI;多说话人按需后续)
- 替换现有 `speech_recognition.py`(保留,独立并行)

## 3. 架构

### 3.1 subprocess 隔离(与 SauPlatformUploader 一致)
whisperx 重依赖(torch + CTranslate2 + transformers)隔离在独立 `/Users/mac/asr-venv`,避免与 Y2A 现有 torch(silero-vad 用)冲突。Y2A 通过 subprocess 调 runner。

### 3.2 组件职责
- **`modules/whisperx_runner.py`**(Y2A 仓库内,用 `/Users/mac/asr-venv/bin/python` 跑):CLI 形态
  `python whisperx_runner.py --video <path> --language <auto|en|zh|...> --model large-v3 --output <srt_path> [--device cpu] [--compute-type int8] [--batch-size 16]`
  内部:ffmpeg 提音频(wav 16kHz mono)→ `whisperx.load_model` → `transcribe`(段级)→ `load_align_model`(按语言)→ `align`(段→字级)→ render 字级 SRT 写到 `--output`。对齐失败则回退段级输出。逐行打印进度到 stdout(JSON 末行报结果)。
- **`modules/whisperx_asr.py`**(Y2A venv):`WhisperXAsr` 适配器,subprocess 调 runner,解析 stdout 进度、超时/错误处理。接口 `transcribe(video_path, language, output_srt_path, progress_callback=None, timeout=None) -> (bool, srt_path|err_msg)`。与 `SauPlatformUploader` 同构风格。
- **`task_manager`**:新 stage `STAGE_REMASTER_ASR` + 方法 `_run_remaster_asr(task_id, task_logger)`,读 `REMASTER_PIPELINE_ENABLED` 触发,调 `WhisperXAsr.transcribe`,产出字级 SRT 存 task 目录(`asr_whisperx_<task_id>.srt`)。

### 3.3 模型
- ASR:`large-v3`(质量优先,用户选定)。`device=cpu`, `compute_type=int8`(CPU 量化优化)。
- 对齐:`wav2vec2`(`whisperx.load_align_model`,按源语言自动选对齐模型)。
- 首次运行自动从 HuggingFace 下载(~3GB:large-v3 约 1.5GB + 各语言 wav2vec2 约 1GB)。

## 4. 数据流
```
视频 → ffmpeg 提音频(wav 16kHz mono)
     → whisperx.load_model(large-v3, cpu, int8)
     → transcribe(段级 segments)
     → load_align_model(language) + align(段 → 字级)
     → 字级 segments
     → render SRT
     → downloads/<task_id>/asr_whisperx_<task_id>.srt
```

## 5. 配置
| 键 | 默认 | 说明 |
|----|------|------|
| `REMASTER_PIPELINE_ENABLED` | False | AI 重制管线总开关(默认关,不影响现有管线) |
| `WHISPERX_ASR_PYTHON` | `/Users/mac/asr-venv/bin/python` | runner 解释器 |
| `WHISPERX_RUNNER` | `modules/whisperx_runner.py` | runner 脚本路径 |
| `WHISPERX_MODEL_NAME` | `large-v3` | ASR 模型 |
| `WHISPERX_DEVICE` | `cpu` | Apple Silicon 走 CPU(CTranslate2 不支持 MPS) |
| `WHISPERX_COMPUTE_TYPE` | `int8` | CPU 量化 |
| `WHISPERX_BATCH_SIZE` | `16` | 对齐 batch |
| `WHISPERX_TIMEOUT_SECONDS` | `7200` | 长视频 CPU 跑慢,超时放宽到 2h |
| 沿用 `SUBTITLE_SOURCE_LANGUAGE` | | `auto`/指定(en/zh/ja…) |

## 6. 错误处理
- **模型缺失**:首次运行自动从 HF 下载;下载失败(国内网络)→ 明确报错 + 提示走 hf-mirror 或代理
- **CPU 超时**:长视频(>1h)large-v3 CPU 约 2-3h,`WHISPERX_TIMEOUT_SECONDS=7200`;超时 kill 进程 + 任务标记失败(日志建议降 `medium`)
- **对齐失败**:回退段级时间戳(whisperx transcribe 的段级输出,跳过 align),不阻断
- **OOM**:large-v3 int8 约 2-3GB RAM,M5 Max 128GB 无虞

## 7. 测试
- **单元**:stub runner(返回固定 SRT/退出码)测 `WhisperXAsr` 的进度解析/超时/成功失败映射(仿 `tests/test_sau_uploader.py`)
- **集成**:`_run_remaster_asr` 在 task 上跑通(mock runner)
- **冒烟(手动)**:真实短视频 `videos/demo.mp4` 跑 large-v3,验证字级 SRT 产出 + 时间戳精度

## 8. 与现有管线关系(完全独立并行)
- **现有**:`speech_recognition.py`(OpenAI 兼容 ASR) + `_translate_subtitle` + `_prepare_subtitle_for_upload` 保留,给"轻字幕翻译"场景。`REMASTER_PIPELINE_ENABLED=False`(默认)时走这条,行为不变。
- **重制管线**:`REMASTER_PIPELINE_ENABLED=True` 时,在 DOWNLOAD_VIDEO 之后插入 `STAGE_REMASTER_ASR` → `WhisperXAsr`,产出**字级 SRT**。后续子项目(翻译/配音/合成)在重制管线里逐步加 stage。
- 两条管线不共享 ASR 调用路径,互不干扰。重制管线完整成型前(合成子项目完成前),它只产出中间产物(字级 SRT),最终上传的视频仍走现有管线。

## 9. 实现顺序
1. 装 `/Users/mac/asr-venv`(`pip install whisperx`,Tsinghua mirror);首次下载 large-v3 + wav2vec2 模型
2. 写 `modules/whisperx_runner.py`(CLI + whisperx 调用 + 字级对齐)
3. 写 `modules/whisperx_asr.py`(subprocess 适配器)
4. stub 单测 `tests/test_whisperx_asr.py`
5. `config_manager` 新键 + `task_manager` stage + `_run_remaster_asr`
6. 真实短视频冒烟(验证字级 SRT)
7. 接进 Y2A 任务流(`REMASTER_PIPELINE_ENABLED` 触发)

## 10. 待决 / 未来
- large-v3 CPU 速度实测后若不可接受 → `WHISPERX_MODEL_NAME` 可降 `medium`,或转向 B 方案 mlx-whisper(性能换封装)
- speaker diarization(多说话人)按需后续加
- 后续子项目:RAG 翻译 / 变速配音 / PaddleOCR 字幕定位 / 字幕擦除与合成
