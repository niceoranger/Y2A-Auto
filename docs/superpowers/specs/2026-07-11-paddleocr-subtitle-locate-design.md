# PaddleOCR 字幕定位 设计文档

> AI 重制管线第 5 个子项目。从视频抽帧跑 PaddleOCR，把烧录字幕区域聚类成时间线 bbox 段并落盘 JSON，供后续 FFmpeg 字幕擦除消费。全本地优先。

**日期**：2026-07-11  
**状态**：设计已批准，待写实现计划  
**前置**：WhisperX / RAG 术语 / Demucs / XTTSv2 配音（均已合并）

---

## 1. 目标与边界

**目标**：对任务本地视频按间隔抽帧，用 PaddleOCR 检测文本框，按时间聚类为字幕带 bbox 段，写出 JSON 中间产物。

**边界（关键）**：
- **只定位落盘，不擦除、不改视频、不写任何下游业务路径字段**
- 软失败：写 `ocr_warning_message`、还原 status、`return False`，不 raise、不阻断上传
- 总开关复用 `REMASTER_PIPELINE_ENABLED`（默认关）
- 不依赖翻译 / 配音结果；与 ASR、Demucs 同属下载后 remaster 侧

**不在本子项目**：
- FFmpeg delogo / inpaint / 最终成片
- 用固定底栏启发式完全替代 OCR
- 把 bbox 自动接进现有字幕烧录路径

## 2. 决策摘要

| 项 | 选择 | 理由 |
|----|------|------|
| 边界 | 纯定位 + 落盘 | 与 WhisperX/Demucs/配音一致 |
| 采样 | 抽帧间隔（默认 0.5s）+ IoU 时间聚类 | 速度与稳定性平衡 |
| 画面 | **全画面** det | 用户明确选择；靠尺寸/宽高比过滤噪声 |
| 产物 | 时间线 bbox 段 JSON | FFmpeg 可按段 delogo，也可再取并集做固定带 |
| 坐标 | 归一化 `[x,y,w,h]`（0–1） | 分辨率无关 |
| 环境 | 复用 `asr-venv` | 少维护一个 venv |
| 总开关 | `REMASTER_PIPELINE_ENABLED` | 不新增开关 |

## 3. 架构

```
task_manager._run_remaster_ocr()
        │  Popen + 逐行 stdout 进度 + 末行 JSON + 超时 kill
        ▼
modules/ocr_locator.py   (OcrLocator adapter)
        │  subprocess: asr-venv/bin/python
        ▼
modules/ocr_runner.py
   ffprobe 元数据 → ffmpeg 抽帧 → PaddleOCR → 聚类 → JSON
        ▼
downloads/<task_id>/ocr_subtitle_boxes_<task_id>.json
```

**管线顺序**（`PIPELINE_STAGE_ORDER`）：

```
... → remaster_asr → remaster_demucs → remaster_ocr → translate_subtitle → remaster_dub → upload
```

OCR 与 ASR/Demucs **无数据依赖**；固定在 demucs 之后仅为 checkpoint 顺序清晰。  
process_task 中与 ASR/Demucs 同一 `REMASTER_PIPELINE_ENABLED` 门控块：asr → demucs → ocr，各自 try/except + `_mark_stage_done`。

## 4. 产物 JSON 契约

路径：`downloads/<task_id>/ocr_subtitle_boxes_<task_id>.json`

```json
{
  "video": "<abspath>",
  "width": 1920,
  "height": 1080,
  "duration_sec": 123.4,
  "sample_interval_sec": 0.5,
  "roi": "full",
  "lang": "ch",
  "segments": [
    {
      "start": 12.0,
      "end": 15.5,
      "box": [0.08, 0.82, 0.84, 0.12],
      "score": 0.91
    }
  ]
}
```

- `box`：归一化 `[x, y, w, h]`，原点左上，相对 `width`/`height`
- `score`：段内采样检测分数均值（或 max，实现取均值）
- `segments` 可为空数组（仍 `ok: true`，表示未检出烧录字幕）

## 5. ocr_runner.py（CLI，asr-venv）

### 5.1 参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--video` | 必填 | 本地视频 |
| `--output` | 必填 | 输出 JSON 路径 |
| `--task-id` | 必填 | 任务 id（临时目录） |
| `--sample-interval` | `0.5` | 抽帧间隔（秒） |
| `--lang` | `ch` | PaddleOCR 语言（ch 覆盖中英） |
| `--device` | `cpu` | 默认 cpu（Apple Silicon 稳妥） |
| `--iou-threshold` | `0.5` | 时间聚类 IoU 阈值 |

### 5.2 流程

1. 校验视频存在；`ffprobe` 取 width/height/duration；失败 → exit 2。
2. 临时目录 `downloads/<task_id>/.ocr_work_<task_id>/` 抽帧：  
   `ffmpeg -y -i video -vf fps=1/<interval> -q:v 2 frame_%06d.jpg`
3. 加载 PaddleOCR（`use_angle_cls=True` 可选；det 必需，rec 用于置信度/可选文本，默认开启 rec 以便 score）。
4. 对每帧：
   - 跑 OCR，得到像素框
   - 过滤：面积过小、宽高比极端（实现内常量，可后续配置化）
   - 转归一化 `[x,y,w,h]`，附 score、帧时间戳 `t = frame_index * interval`
5. **聚类**（纯逻辑，可单测）：
   - 按时间排序
   - 若与当前段 last 框 IoU ≥ threshold 且时间 gap ≤ 1.5×interval，并入段（box 取并集，score 均值）
   - 否则新开段
6. 写 JSON；stdout 末行成功/失败 JSON；清理临时帧。

### 5.3 stdout / 退出码

成功：

```json
{"ok": true, "boxes": "<path>.json", "segments": 12, "frames": 240, "device_used": "cpu"}
```

失败：`{"ok": false, "error": "..."}`  
退出码：`0` 成功 / `1` 运行失败 / `2` 参数或依赖缺失。

## 6. 聚类纯逻辑（modules/ocr_cluster.py）

独立模块，零 I/O，便于单测：

```python
def iou_box(a, b) -> float: ...
def merge_box(a, b) -> list:  # 并集 [x,y,w,h]
def cluster_detections(dets, iou_threshold=0.5, max_gap_sec=0.75) -> list[dict]:
    # dets: [{t, box, score}, ...]
    # returns: [{start, end, box, score}, ...]
```

`max_gap_sec` 默认 `1.5 * sample_interval`，由 runner 传入。

## 7. OcrLocator adapter（modules/ocr_locator.py）

照抄 `DemucsSeparator` / `DubGenerator` 骨架：Popen、逐行进度、末行 JSON、超时 kill、finally close stdout、`(bool, dict|str)`。

校验：`python_bin` / `runner_path` / `video_path` 存在。

## 8. task_manager 接线

### 8.1 常量

- `PIPELINE_STAGE_REMASTER_OCR = 'remaster_ocr'`，ORDER：demucs 之后、translate_subtitle 之前
- `TASK_STATES['OCR_LOCATING'] = 'ocr_locating'` → `PROCESSING_STATES`

### 8.2 DB：`ocr_warning_message` 三件套

1. CREATE TABLE 列  
2. ALTER 迁移  
3. ALLOWED_COLUMNS  

（与 demucs/dub 同一模式，缺一即静默丢警告。）

### 8.3 `_run_remaster_ocr(task_id, task_logger)`

1. `get_task`；无任务 → False  
2. `video_path_local` 缺失 → warn 跳过  
3. 读 `OCR_*` config；runner 相对路径拼项目根  
4. 输出 `downloads/<id>/ocr_subtitle_boxes_<id>.json`  
5. `status=OCR_LOCATING` → `OcrLocator.locate(...)`  
6. 成功：`ocr_warning_message=None`、还原 prev_status；**不写视频/字幕路径**  
7. 失败：`ocr_warning_message=f"ocr: {res}"`、还原 prev_status、False  

### 8.4 process_task 门控

与 ASR/Demucs **同一** REMASTER 门控块内追加：

```python
# 已有 asr / demucs 后
try:
    self._run_remaster_ocr(task_id, task_logger)
except Exception as e:
    task_logger.error(f"重制 OCR 异常: {e}")
completed_stages = _mark_stage_done(
    task_id, completed_stages, PIPELINE_STAGE_REMASTER_OCR
)
```

门控条件仍是：download 完成 + remaster 总开关开 +（按现有 asr 门控写法：asr 未完成则进块，块内 asr/demucs/ocr 各 mark done）。  
实现时 **对齐当前 asr/demucs 块的实际结构**（以代码为准），保证 ocr 在 demucs 之后执行并单独 mark stage。

## 9. config 新键

放 DUB 块之后：

```python
"OCR_PYTHON": "/Users/mac/asr-venv/bin/python",
"OCR_RUNNER": "modules/ocr_runner.py",
"OCR_SAMPLE_INTERVAL_SEC": 0.5,
"OCR_LANG": "ch",
"OCR_DEVICE": "cpu",
"OCR_IOU_THRESHOLD": 0.5,
"OCR_TIMEOUT_SECONDS": 3600,
```

## 10. 测试

| 文件 | 覆盖 |
|------|------|
| `tests/test_ocr_config.py` | OCR_* 键 + REMASTER 仍默认关 |
| `tests/test_ocr_locator.py` | 缺 python/runner/video；成功 JSON；非零退出；超时；进度 |
| `tests/test_ocr_cluster.py` | IoU、并集 box、gap 断段、空输入 |
| `tests/test_ocr_warning_persistence.py` | `ocr_warning_message` 往返 |

runner 真模型靠冒烟，不写重型集成单测。

## 11. 环境准备 + 冒烟 + 收尾

1. asr-venv 安装：`paddlepaddle` + `paddleocr`（Apple Silicon 用官方 CPU 指引；若冲突再记入 plan）  
2. 冒烟：含烧录字幕的短视频 → JSON 存在、`segments` 合理、归一化坐标 ∈[0,1]  
3. 全测试 + 全分支复审 → squash 合并 main  

**验收**：
- `REMASTER_PIPELINE_ENABLED=True` 时产出 `ocr_subtitle_boxes_<task_id>.json`，不影响上传  
- 开关关时行为字节级不变  
- 无字幕视频可 `ok` + `segments: 0`，软处理  

## 12. AI 重制管线进度

- ✅ WhisperX ASR + 字级对齐  
- ✅ RAG 术语翻译  
- ✅ Demucs 音轨分离  
- ✅ XTTSv2 + RubberBand 配音  
- ⬜ **PaddleOCR 字幕定位（本设计）**  
- ⬜ FFmpeg 字幕擦除 + 合成（将消费本 JSON + `dubbed_audio`）
