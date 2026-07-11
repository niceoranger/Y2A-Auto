# FFmpeg 字幕擦除 + 成片合成 设计文档

> AI 重制管线第 6（末）子项目。消费 OCR 时间线 bbox + 配音音轨 + 译文字幕，对本地视频 delogo 擦烧录字幕、替换配音、硬烧中文字幕，产出 `remastered_<task_id>.mp4` 并写回 `video_path_local` 供上传。全本地优先。

**日期**：2026-07-11  
**状态**：设计已批准，待写实现计划  
**前置**：WhisperX / RAG 术语 / Demucs / XTTSv2 配音 / PaddleOCR 定位（均已合并）

---

## 1. 目标与边界

**目标**：在 `remaster_dub` 之后合成最终成片：

1. 用 OCR 时间线 bbox **delogo** 擦烧录字幕  
2. 用 **dubbed_audio** 替换音轨  
3. 把 **中文译文 SRT 硬烧**进画面  
4. 写出 `remastered_<task_id>.mp4` 并 **写回 `video_path_local`** 供上传  

**边界（关键）**：
- gated by `REMASTER_PIPELINE_ENABLED`（默认关）
- **软失败**：写 `composite_warning_message`、还原 status、**不改** `video_path_local`（继续上传原片），不 raise、不阻断上传
- 缺件尽力合成（见 §2）
- 不修改 OCR/配音中间产物本身
- 合成跑在**主进程 Python + 项目 ffmpeg**（`get_ffmpeg_path`），**不**进 asr-venv

**不在本子项目**：
- 重做 OCR / 配音 / 翻译
- 云端渲染、复杂 inpaint 模型
- 多平台上传逻辑变更（只替换本地成片路径）

## 2. 决策摘要

| 项 | 选择 | 理由 |
|----|------|------|
| 边界 | 成片并写回 `video_path_local` | 直接服务投稿链路 |
| 擦除 | delogo 按 OCR 段 + enable 时间窗 | 经典、本机 ffmpeg 已支持 |
| 成片层 | 擦画面 + 配音 + 硬字幕 | 用户明确选择 |
| Stage | `remaster_composite`，在 dub 后、upload 前 | 依赖中间产物尽量就绪 |
| 运行环境 | 主 venv + `get_ffmpeg_path()` | 无需 Paddle/XTTS 依赖 |

**缺件策略**（尽力合成）：

| 输入 | 缺失时 |
|------|--------|
| `video_path_local` | 软跳过整段 composite |
| OCR JSON / segments 空 | 跳过 delogo，画面原样 |
| `dubbed_audio_*.wav` | 保留原视频音轨 |
| `subtitle_path_translated` | 不烧硬字幕 |

## 3. 架构

```
task_manager._run_remaster_composite()
        │  主进程调用
        ▼
modules/remaster_composite.py
   读 video + ocr json + dubbed wav + 译 srt
   → 组 filter_complex（delogo 链 + subtitles）
   → 音轨 map dubbed 或原音
   → remastered_<id>.mp4
        ▼
update_task(video_path_local=remastered_path)  # 仅成功时
```

**管线顺序**（`PIPELINE_STAGE_ORDER`）：

```
... → remaster_ocr → translate_subtitle → remaster_dub → remaster_composite → upload
```

**process_task 门控**（独立块，在 remaster_dub 之后、upload 之前）：

```python
if PIPELINE_STAGE_REMASTER_DUB in completed_stages and \
        PIPELINE_STAGE_REMASTER_COMPOSITE not in completed_stages and \
        _as_bool(self.config.get('REMASTER_PIPELINE_ENABLED', False)):
    try:
        self._run_remaster_composite(task_id, task_logger)
    except Exception as e:
        task_logger.error(f"重制合成异常: {e}")
    completed_stages = _mark_stage_done(
        task_id, completed_stages, PIPELINE_STAGE_REMASTER_COMPOSITE
    )
```

说明：现有 dub 门控在 translate 完成后会 **mark `remaster_dub` done**（即使配音软跳过）。composite 紧接其后，因此「无配音」时仍会进入 composite，并按缺件策略保留原音。

## 4. 输入 / 输出路径

| 角色 | 路径 |
|------|------|
| 视频 | `task['video_path_local']` |
| OCR | `downloads/<task_id>/ocr_subtitle_boxes_<task_id>.json` |
| 配音 | `downloads/<task_id>/dubbed_audio_<task_id>.wav` |
| 字幕 | `task['subtitle_path_translated']` |
| 成片 | `downloads/<task_id>/remastered_<task_id>.mp4` |

OCR JSON 契约（只读，不修改）：

```json
{
  "width": 1920, "height": 1080,
  "segments": [
    {"start": 12.0, "end": 15.5, "box": [0.08, 0.82, 0.84, 0.12], "score": 0.91}
  ]
}
```

`box` 为归一化 `[x, y, w, h]`（0–1，原点左上）。

## 5. remaster_composite 模块

### 5.1 纯逻辑（单测友好）

```python
def load_ocr_segments(json_path) -> list[dict]:
    # 缺文件/坏 JSON → []

def norm_box_to_pixels(box, width, height, pad_px=6) -> tuple[int,int,int,int]:
    # 转整数 x,y,w,h；pad 后夹紧到 [0,W)×[0,H)
    # delogo 要求 w,h >= 1

def merge_nearby_segments(segments, max_segments=40) -> list:
    # 段数超上限时：优先合并时间相邻且 IoU 高的段；仍超则按 score 保留高分

def build_delogo_filter(segments, width, height, pad_px=6, max_segments=40) -> str:
    # 返回 vf 片段：delogo=x:y:w:h:enable='between(t,s,e)',...
    # 无段 → ""

def build_subtitle_vf_fragment(srt_path, fonts_dir_or_style) -> str:
    # subtitles=...:force_style=...（路径转义）
    # 无 srt → ""

def build_vf_chain(delogo_part, subtitle_part) -> str | None:
    # 逗号连接；皆空 → None（可 -c:v copy 视音频策略而定）
```

### 5.2 命令构建与执行

```python
def build_composite_cmd(
    ffmpeg_bin, video_path, out_path,
    *, dubbed_wav=None, vf=None, timeout_hint=None
) -> list[str]:
    # 有 vf：-i video [-i dubbed] -vf vf -map 0:v -map 1:a|0:a?
    # 无 vf 但有 dubbed：-i video -i dubbed -map 0:v -map 1:a -c:v copy -c:a aac
    # 无 vf 无 dubbed：无需合成（调用方应软跳过）

def run_composite(...) -> (True, {"ok": True, "video": path}) | (False, err_str)
    # subprocess + 超时；检查输出文件存在且 size>0
```

**delogo 滤镜示例**（像素已算好）：

```
delogo=x=50:y=295:w=300:h=33:enable='between(t,0.0,2.5)'
```

多段用逗号串联。`enable` 时间用秒、浮点。

**硬字幕**：优先复用 `TaskProcessor` 已有 subtitle force_style / `subtitles=` 构建逻辑（抽取或调用同类辅助），字体使用配置 `SUBTITLE_FONT_NAME` 对应项目内置字体。路径含特殊字符时做 ffmpeg 转义。

**编码**：
- 有视频滤镜（delogo 或烧字幕）→ 必须重编码视频（libx264 或现有硬编探测，失败回退 libx264）
- 仅换音轨、无 vf → 可 `-c:v copy` + 音频 aac
- `-movflags +faststart`

## 6. task_manager 接线

### 6.1 常量

- `PIPELINE_STAGE_REMASTER_COMPOSITE = 'remaster_composite'`  
  ORDER：`remaster_dub` 之后、`upload_to_acfun` 之前  
- `TASK_STATES['COMPOSITING'] = 'compositing'` → `PROCESSING_STATES`

### 6.2 DB：`composite_warning_message` 三件套

1. CREATE TABLE 列  
2. ALTER 迁移  
3. ALLOWED_COLUMNS  

### 6.3 `_run_remaster_composite(task_id, task_logger)`

1. `get_task`；无任务 → False  
2. `video_path = video_path_local`；缺/不存在 → warn 跳过  
3. 拼路径：ocr json、dubbed wav、`subtitle_path_translated`  
4. 读 `COMPOSITE_*` config（max delogo、pad、timeout、burn 开关）  
5. 解析配置**先于** status 翻转（避免非法配置卡在 COMPOSITING）  
6. `status=COMPOSITING`  
7. 调 `run_composite(...)`  
8. **成功**：`update_task(video_path_local=remastered, composite_warning_message=None, status=prev)`  
9. **失败**：`composite_warning_message=f"composite: {res}"`、**不改** video_path、status=prev、False  

### 6.4 process_task 门控

见 §3。与 dub 门控同构：try/except + 总是 `_mark_stage_done(COMPOSITE)`。

## 7. config 新键

放 OCR 块之后：

```python
"COMPOSITE_MAX_DELOGO_SEGMENTS": 40,   # delogo 段数上限
"COMPOSITE_DELOGO_PAD_PX": 6,          # bbox 外扩像素
"COMPOSITE_TIMEOUT_SECONDS": 10800,    # 3h
"COMPOSITE_BURN_SUBTITLE": True,       # 硬烧译文字幕
```

总开关复用 `REMASTER_PIPELINE_ENABLED`，不新增总开关。

## 8. 测试

| 文件 | 覆盖 |
|------|------|
| `tests/test_composite_config.py` | COMPOSITE_* 键 + REMASTER 仍默认关 |
| `tests/test_composite_filters.py` | 归一化→像素夹紧；delogo enable 串；段数上限；空 segments；build_vf_chain 空/仅 delogo/delogo+sub |
| `tests/test_composite_warning_persistence.py` | `composite_warning_message` 往返 |

不强制真 ffmpeg 成片单测（重）；冒烟验证。

## 9. 环境 + 冒烟 + 收尾

1. 系统/项目 ffmpeg 已具备 `delogo` 滤镜（本机已验证）  
2. 冒烟：短视频 + 手写 OCR JSON（底栏一段）+ 短 wav + 中文 srt → 产出 remastered mp4，时长≈片源  
3. 全测试 + 全分支复审 → squash 合并 main  

**验收**：
- `REMASTER_PIPELINE_ENABLED=True` 且前置中间产物齐时：成片存在且 `video_path_local` 指向它  
- 缺 OCR/配音/字幕时仍尽量成片或合理软跳过，不 fail 整任务  
- 开关关时行为字节级不变  

## 10. AI 重制管线进度

- ✅ WhisperX ASR + 字级对齐  
- ✅ RAG 术语翻译  
- ✅ Demucs 音轨分离  
- ✅ XTTSv2 + RubberBand 配音  
- ✅ PaddleOCR 字幕定位  
- ⬜ **FFmpeg 字幕擦除 + 合成（本设计）**
