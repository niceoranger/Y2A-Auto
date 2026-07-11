# RubberBand + XTTSv2 配音 设计文档

> AI 重制管线第 4 个子项目。消费翻译后的中文字幕 + Demucs 背景音轨，用 XTTSv2 固定声线合成中文配音，RubberBand 变速对齐原段时长，混回背景，产出配音音轨。全本地优先。

**日期**：2026-07-11
**状态**：设计已批准，待写实现计划
**前置**：WhisperX ASR（已合并）、RAG 术语翻译（已合并）、Demucs 音轨分离（已合并）

---

## 1. 目标与边界

从任务的**翻译后中文字幕**与 **Demucs no_vocals 背景音**生成一条中文配音音轨：逐段 XTTSv2 合成 → RubberBand 对齐段时长 → overlay 到背景时间线。

**边界（关键）**：本子项目**只产中间产物落盘，不替换视频音轨、不写任何下游业务字段**。最终把配音合成进视频是 FFmpeg 合成子项目的职责。与 WhisperX / Demucs 边界一致。

**软失败**：失败只写 `dub_warning_message`、还原 status、`return False`，绝不 raise、不阻断翻译/上传。

**不在本子项目范围**：
- 声音克隆（用户明确选固定内置声线，不用 Demucs vocals 作参考）
- 字幕擦除 / 烧录 / 最终成片（FFmpeg 合成）
- 把 WhisperX SRT 自动接入翻译主链路（既有 `subtitle_path_translated` 即可；缺则软跳过）

## 2. 决策摘要

| 项 | 选择 | 理由 |
|----|------|------|
| TTS | XTTSv2（coqui-tts），CPU | 质量最高；用户明确选此 |
| 声线 | 固定内置 speaker（默认可配置，如 `Ana Florence`） | 简单可靠；不依赖原人声质量 |
| 变速 | RubberBand CLI（`brew install rubberband`） | 高质量时间拉伸（crispness 5）；用户质量优先 |
| 总开关 | 复用 `REMASTER_PIPELINE_ENABLED` | 与 ASR/Demucs 一致 |
| 产物 | `downloads/<task_id>/dubbed_audio_<task_id>.wav` | 中间产物，等 FFmpeg 合成消费 |

**XTTS 代价（已知）**：
- ~2GB 模型权重
- 自定义算子**不支持 MPS**，只跑 CPU；长视频配音可能接近或超过视频时长
- CPML 非商用协议（个人用可，商用受限）

## 3. 架构

```
task_manager._run_remaster_dub()
        │  (Popen + 逐行 stdout 进度 + 末行 JSON + 超时 kill)
        ▼
modules/dub_generator.py  (DubGenerator adapter)
        │  subprocess: asr-venv/bin/python
        ▼
modules/dub_runner.py
   读 translated SRT + demucs_no_vocals →
   逐段 XTTS → RubberBand → overlay 到背景 →
   写出 dubbed_audio_<task_id>.wav
```

**管线顺序**（`PIPELINE_STAGE_ORDER`）：

```
... → remaster_asr → remaster_demucs → translate_subtitle → remaster_dub → upload
```

`remaster_dub` **必须在 `translate_subtitle` 之后**（需要 `subtitle_path_translated`）。ASR/Demucs 仍在下载后、翻译前的既有 REMASTER 门控块；dub 用**独立门控块**挂在翻译之后。

## 4. dub_runner.py（CLI，跑在 asr-venv）

### 4.1 参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--translated-srt` | 必填 | 中文字幕 SRT |
| `--no-vocals` | 必填 | Demucs 背景 wav |
| `--output` | 必填 | 输出 dubbed wav 路径 |
| `--task-id` | 必填 | 任务 id（临时目录命名） |
| `--speaker` | 配置默认 | XTTS 内置声线名 |
| `--language` | `zh` | XTTS 语言码 |
| `--device` | `cpu` | 仅 cpu |
| `--max-tempo` | `1.5` | RubberBand 加速上限 |

### 4.2 流程

1. 校验输入文件存在；缺则 exit 2 + JSON error。
2. 校验 `rubberband` 在 PATH；缺失则明确报错（不静默退 atempo）。
3. 加载 XTTSv2 模型（`tts_models/multilingual/multi-dataset/xtts_v2`）。
4. 解析 SRT → `[(start, end, text), ...]`；空文本段跳过。
5. 对每一段：
   - XTTS 合成到临时 wav（自然语速）
   - `T = end - start`，`D = 自然时长`
   - **时长策略**：
     - `D ≤ T`：静音 pad 到 `T`（默认 pad，比拉长更自然）
     - `D > T`：RubberBand 加速到 `tempo = min(D/T, max_tempo)`；若加速后仍 > `T`，硬截到 `T`（避免压到下一段）
6. 以 `no_vocals` 为底轨，按段起点 overlay 所有 TTS 片段（numpy 叠加或 ffmpeg `adelay`+`amix`）。
7. 写出输出 wav（采样率/声道与 no_vocals 对齐，优先 44.1kHz 立体声）。
8. 清理临时目录；stdout 末行 JSON。

### 4.3 RubberBand 调用

```
rubberband -t <time_ratio> -c 5 <in.wav> <out.wav>
```

`-t <time_ratio>`：输出时长 = 输入时长 × time_ratio（`<1` 加速缩短）。
`-c 5`：crispness 默认档（语音清晰）。注意 `-c` 是 crispness 不是 formant；formant 旗标是 `-F`（变调时才有意义）。
本实现只做纯时间拉伸，不使用 `-F`。

### 4.4 stdout 协议 / 退出码

成功末行：

```json
{"ok": true, "dubbed": "<path>.wav", "segments": 12, "device_used": "cpu"}
```

失败：`{"ok": false, "error": "..."}`。

退出码：`0` 成功 / `1` 运行失败 / `2` 参数错或依赖缺失（对齐 whisperx/demucs runner）。

## 5. DubGenerator adapter（modules/dub_generator.py）

照抄 `WhisperXAsr` / `DemucsSeparator` 骨架：

```python
class DubGenerator:
    def __init__(self, python_bin, runner_path): ...
    def generate(self, *, translated_srt_path, no_vocals_wav, output_path, task_id,
                 speaker="Ana Florence", language="zh", device="cpu",
                 max_tempo=1.5, progress_callback=None, timeout=14400):
        # 校验 python_bin / runner_path / srt / no_vocals
        # Popen + 逐行进度 + 末行 JSON + 超时 kill + finally close stdout
        return (True, last_json) or (False, err_str)
```

## 6. task_manager 接线

### 6.1 常量

- `PIPELINE_STAGE_REMASTER_DUB = 'remaster_dub'`，插在 `translate_subtitle` 之后、`upload` 之前
- `TASK_STATES['DUBBING'] = 'dubbing'`，并入 `PROCESSING_STATES`

### 6.2 DB：`dub_warning_message` 三件套

照抄 demucs 最终审查的修法，三处一起加：

1. `CREATE TABLE` 列
2. `ALTER TABLE` 迁移
3. `ALLOWED_COLUMNS` 允许写入

缺任何一处 → 警告静默丢失。

### 6.3 `_run_remaster_dub(task_id, task_logger)`

1. `get_task`；无任务 → `return False`
2. 读 `subtitle_path_translated`；空/文件不存在 → warn 跳过、`return False`
3. `no_vocals = downloads/<task_id>/demucs_no_vocals_<task_id>.wav`；不存在 → warn 跳过（Demucs 未跑或失败）
4. 读 config：`DUB_PYTHON` / `DUB_RUNNER` / `DUB_SPEAKER` / `DUB_LANGUAGE` / `DUB_DEVICE` / `DUB_MAX_TEMPO` / `DUB_TIMEOUT_SECONDS`；runner 相对路径拼项目根
5. 输出：`downloads/<task_id>/dubbed_audio_<task_id>.wav`
6. `update_task(status=TASK_STATES['DUBBING'])`
7. 调 `DubGenerator.generate(...)`
8. 成功：`dub_warning_message=None`、还原 `prev_status`；**不写视频/字幕路径**
9. 失败：`dub_warning_message=f"dub: {res}"`、还原 `prev_status`、`return False`

### 6.4 process_task 门控（独立块，在 translate_subtitle 之后、上传之前）

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

说明：gate **不**要求 Demucs stage 一定在 completed 集合里——runner / `_run_remaster_dub` 自己检查 no_vocals 文件；缺了软跳过。避免 checkpoint 恢复时互相卡死。

## 7. config 新键

放 DEMUCS 块之后：

```python
"DUB_PYTHON": "/Users/mac/asr-venv/bin/python",
"DUB_RUNNER": "modules/dub_runner.py",
"DUB_XTTS_MODEL": "tts_models/multilingual/multi-dataset/xtts_v2",
"DUB_SPEAKER": "Ana Florence",
"DUB_LANGUAGE": "zh",
"DUB_DEVICE": "cpu",
"DUB_MAX_TEMPO": 1.5,
"DUB_TIMEOUT_SECONDS": 14400,
```

总开关**复用 `REMASTER_PIPELINE_ENABLED`**，不新增开关。

## 8. 测试

| 文件 | 覆盖 |
|------|------|
| `tests/test_dub_config.py` | DUB_* 键存在 + 默认值 + REMASTER 仍默认关 |
| `tests/test_dub_generator.py` | 缺 python/runner/srt/no_vocals；成功末行 JSON；非零退出；超时 kill；进度回调 |
| `tests/test_dub_timing.py` | 纯逻辑：pad / 加速 / cap max_tempo / 硬截 的 stretch 倍率计算 |
| `tests/test_dub_warning_persistence.py` | `dub_warning_message` 经 `update_task`/`get_task` 往返（照抄 demucs 回归） |

runner 真模型靠冒烟验证，不写重型集成单测。

## 9. 环境准备 + 冒烟 + 收尾

1. **RubberBand**：`brew install rubberband`，验证 `rubberband --help`
2. **coqui-tts / TTS**：装进 asr-venv；首次跑触发 XTTSv2 权重下载（~2GB）；HF 卡则镜像
3. **冒烟**：短中文 SRT + 一段 no_vocals → 产出 dubbed wav；核对时长≈背景、段起点可听、无 crash
4. 全测试 + 全分支复审 → squash 合并 main

**验收标准**：
- `REMASTER_PIPELINE_ENABLED=True` 且具备翻译字幕 + no_vocals 时产出 `dubbed_audio_<task_id>.wav`，且不影响上传
- 缺翻译字幕或缺 no_vocals 时软跳过，不 fail 任务
- 开关关时行为字节级不变

## 10. AI 重制管线进度

- ✅ WhisperX ASR + 字级对齐
- ✅ RAG 术语翻译
- ✅ Demucs 音轨分离
- ⬜ **RubberBand + XTTSv2 配音（本设计）**
- ⬜ PaddleOCR 字幕定位
- ⬜ FFmpeg 字幕擦除 + 合成（将消费本子项目的 dubbed_audio + OCR 区域）
