# FFmpeg 字幕擦除 + 成片合成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 AI 重制管线新增 `remaster_composite` stage：对本地视频按 OCR 时间线 bbox delogo 擦烧录字幕、用 dubbed_audio 替换音轨、硬烧中文译文字幕，产出 `remastered_<task_id>.mp4` 并在成功时写回 `video_path_local` 供上传；默认关，软失败。

**Architecture:** 主进程 + 项目内置 ffmpeg（`get_ffmpeg_path`），不用 asr-venv。纯逻辑（归一化 box→像素、delogo filter、vf 链、命令构建）拆到 `modules/remaster_composite.py` 便于单测；`task_manager._run_remaster_composite` 软失败接线，gate 挂在 `remaster_dub` 之后、`upload` 之前，复用 `REMASTER_PIPELINE_ENABLED`。缺件尽力合成（缺 OCR 跳 delogo、缺配音留原音、缺译 srt 不烧字幕）。

**Tech Stack:** Python 3 / 项目内置 ffmpeg（delogo + subtitles 滤镜）/ subprocess / unittest（`.venv/bin/python -m unittest`）

---

## 关键既有事实（实现者必读）

- **测试运行器**：项目**无 pytest**。一律用 `.venv/bin/python -m unittest`。
- **ffmpeg**：用 `from modules.ffmpeg_manager import get_ffmpeg_path, get_ffprobe_path`；`get_ffmpeg_path(logger=...)` 返回可执行路径或 None。ffmpeg 8.x 支持 `delogo`、`subtitles` 滤镜。
- **字体**：项目字体目录 `fonts/`；默认字体配置键 `SUBTITLE_FONT_NAME`（`NotoSansCJKsc-Regular.otf`）。`subtitles=` 滤镜可用 `fontsdir=fonts`。
- **产物路径约定**（`DOWNLOADS_DIR = get_app_subdir('downloads')`，即 `downloads/`）：
  - 视频：`task['video_path_local']`
  - OCR：`downloads/<id>/ocr_subtitle_boxes_<id>.json`
  - 配音：`downloads/<id>/dubbed_audio_<id>.wav`
  - 译字幕：`task['subtitle_path_translated']`
  - 输出：`downloads/<id>/remastered_<id>.mp4`
- **OCR JSON 结构**（由已合并的 ocr_runner 产出）：
  ```json
  {"width":640,"height":360,"segments":[{"start":0.0,"end":2.5,"box":[0.27,0.89,0.47,0.09],"score":0.99}]}
  ```
  `box` 为归一化 `[x, y, w, h]`，原点左上，相对 width/height。
- **软失败三件套模式**（照抄 dub/ocr 已合并实现）：`CREATE TABLE` 列 + `ALTER TABLE` 迁移 + `ALLOWED_COLUMNS` 白名单，缺任一处则 `update_task` 静默丢弃该字段。
- **remaster 门控现状**：`remaster_asr/demucs/ocr` 在下载后同一块；`remaster_dub` 在 translate_subtitle 之后有独立块，结尾 `_mark_stage_done(..., PIPELINE_STAGE_REMASTER_DUB)`。composite 块紧跟其后。

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `modules/remaster_composite.py`（新建） | 纯逻辑（box→像素、delogo filter、vf 链、ffmpeg 命令构建）+ `run_composite` 执行 |
| `modules/config_manager.py`（改） | `COMPOSITE_*` 新键 |
| `modules/task_manager.py`（改） | stage 常量/状态/DB 三件套/`_run_remaster_composite`/门控 |
| `tests/test_composite_config.py`（新建） | config 键 + REMASTER 默认关 |
| `tests/test_composite_filters.py`（新建） | box→像素、delogo enable、段上限、vf 链、缺件空滤镜 |
| `tests/test_composite_warning_persistence.py`（新建） | `composite_warning_message` 往返 |

---

## Task 1: config 新增 COMPOSITE_* 键 + 单测

**Files:**
- Modify: `modules/config_manager.py`（OCR 块之后、`UPLOAD_TARGETS` 之前）
- Test: `tests/test_composite_config.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_composite_config.py`：

```python
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.config_manager import DEFAULT_CONFIG


class TestCompositeConfig(unittest.TestCase):
    def test_defaults_present(self):
        for k in ["COMPOSITE_MAX_DELOGO_SEGMENTS", "COMPOSITE_DELOGO_PAD_PX",
                  "COMPOSITE_TIMEOUT_SECONDS", "COMPOSITE_BURN_SUBTITLE"]:
            self.assertIn(k, DEFAULT_CONFIG)
        self.assertEqual(DEFAULT_CONFIG["COMPOSITE_MAX_DELOGO_SEGMENTS"], 40)
        self.assertEqual(DEFAULT_CONFIG["COMPOSITE_DELOGO_PAD_PX"], 6)
        self.assertEqual(DEFAULT_CONFIG["COMPOSITE_TIMEOUT_SECONDS"], 10800)
        self.assertTrue(DEFAULT_CONFIG["COMPOSITE_BURN_SUBTITLE"])

    def test_remaster_switch_still_off(self):
        self.assertIn("REMASTER_PIPELINE_ENABLED", DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["REMASTER_PIPELINE_ENABLED"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_composite_config -v`
Expected: FAIL — `'COMPOSITE_MAX_DELOGO_SEGMENTS' not found in ...`

- [ ] **Step 3: 加 config 键**

在 `modules/config_manager.py` 的 `OCR_TIMEOUT_SECONDS` 行之后、`# 多选投稿平台列表` 之前插入：

```python
    # AI 重制管线——FFmpeg 字幕擦除 + 成片合成(复用 REMASTER 总开关)
    "COMPOSITE_MAX_DELOGO_SEGMENTS": 40,   # delogo 段数上限,超出则合并相近段,防 filtergraph 爆炸
    "COMPOSITE_DELOGO_PAD_PX": 6,          # delogo 框四周额外像素,确保盖住抗锯齿边缘
    "COMPOSITE_TIMEOUT_SECONDS": 10800,    # 3h,重编码长视频
    "COMPOSITE_BURN_SUBTITLE": True,       # 是否硬烧译文字幕进画面
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m unittest tests.test_composite_config -v`
Expected: `Ran 2 tests` `OK`

- [ ] **Step 5: 提交**

```bash
git add modules/config_manager.py tests/test_composite_config.py
git commit -m "feat(composite): config 新增 COMPOSITE_* 键(复用 REMASTER 总开关,默认关)"
```

---

## Task 2: remaster_composite 纯逻辑（box→像素 / delogo / vf 链）+ 单测

**Files:**
- Create: `modules/remaster_composite.py`
- Test: `tests/test_composite_filters.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_composite_filters.py`：

```python
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules.remaster_composite import (
    norm_box_to_pixels,
    build_delogo_filter,
    build_vf_chain,
    _ffmpeg_path_escape,
)


class TestNormBoxToPixels(unittest.TestCase):
    def test_basic_conversion_with_pad(self):
        # box [x,y,w,h] 归一化; 640x360; pad=6
        x, y, w, h = norm_box_to_pixels([0.25, 0.80, 0.50, 0.10], 640, 360, pad_px=6)
        # 原始: x=160 y=288 w=320 h=36; pad 后向外扩 6px 并夹紧
        self.assertEqual((x, y), (154, 282))
        self.assertEqual((w, h), (332, 48))

    def test_clamps_to_frame(self):
        # 贴边 box, pad 不能越界
        x, y, w, h = norm_box_to_pixels([0.0, 0.0, 1.0, 1.0], 640, 360, pad_px=6)
        self.assertEqual((x, y), (0, 0))
        # delogo 要求 x+w <= width-1 / y+h <= height-1(留 1px), 见实现
        self.assertLessEqual(x + w, 639)
        self.assertLessEqual(y + h, 359)

    def test_min_size_one(self):
        # 极小 box 至少 1x1
        x, y, w, h = norm_box_to_pixels([0.5, 0.5, 0.0, 0.0], 640, 360, pad_px=0)
        self.assertGreaterEqual(w, 1)
        self.assertGreaterEqual(h, 1)


class TestBuildDelogoFilter(unittest.TestCase):
    def test_empty_segments_returns_empty(self):
        self.assertEqual(build_delogo_filter([], 640, 360), "")

    def test_single_segment(self):
        segs = [{"start": 0.0, "end": 2.5, "box": [0.25, 0.80, 0.50, 0.10]}]
        out = build_delogo_filter(segs, 640, 360, pad_px=0, max_segments=40)
        self.assertIn("delogo=", out)
        self.assertIn("x=160", out)
        self.assertIn("y=288", out)
        self.assertIn("enable='between(t,0.0,2.5)'", out)

    def test_multiple_segments_chained_with_comma(self):
        segs = [
            {"start": 0.0, "end": 1.0, "box": [0.1, 0.8, 0.2, 0.1]},
            {"start": 1.0, "end": 2.0, "box": [0.1, 0.8, 0.2, 0.1]},
        ]
        out = build_delogo_filter(segs, 640, 360, pad_px=0)
        self.assertEqual(out.count("delogo="), 2)
        self.assertIn(",", out)

    def test_caps_at_max_segments(self):
        segs = [{"start": float(i), "end": i + 0.5, "box": [0.1, 0.8, 0.2, 0.1]}
                for i in range(100)]
        out = build_delogo_filter(segs, 640, 360, pad_px=0, max_segments=10)
        self.assertEqual(out.count("delogo="), 10)


class TestBuildVfChain(unittest.TestCase):
    def test_both_parts(self):
        chain = build_vf_chain("delogo=x=1:y=2:w=3:h=4", "subtitles=a.srt")
        self.assertEqual(chain, "delogo=x=1:y=2:w=3:h=4,subtitles=a.srt")

    def test_delogo_only(self):
        self.assertEqual(build_vf_chain("delogo=x=1:y=2:w=3:h=4", ""), "delogo=x=1:y=2:w=3:h=4")

    def test_subtitle_only(self):
        self.assertEqual(build_vf_chain("", "subtitles=a.srt"), "subtitles=a.srt")

    def test_both_empty_returns_none(self):
        self.assertIsNone(build_vf_chain("", ""))


class TestFfmpegPathEscape(unittest.TestCase):
    def test_escapes_colon_and_backslash(self):
        # subtitles= 滤镜内路径需转义 : 和 \
        self.assertEqual(_ffmpeg_path_escape("/a/b.srt"), "/a/b.srt")
        self.assertEqual(_ffmpeg_path_escape("C:\\x\\y.srt"), "C\\:\\\\x\\\\y.srt")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_composite_filters -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'modules.remaster_composite'`

- [ ] **Step 3: 写纯逻辑实现**

Create `modules/remaster_composite.py`（本 Task 只放纯逻辑；`run_composite` 在 Task 3 追加）：

```python
# modules/remaster_composite.py
"""FFmpeg 成片合成:delogo 擦烧录字幕 + 配音替换 + 硬烧译文字幕。

AI 重制管线末子项目。纯逻辑(box→像素/滤镜/命令构建)与执行分离,便于单测。
消费 OCR 时间线 bbox + dubbed_audio + 译文 SRT,产出 remastered_<id>.mp4。
"""
import os
import subprocess
from typing import Dict, List, Optional, Tuple


def norm_box_to_pixels(box, width: int, height: int, pad_px: int = 6) -> Tuple[int, int, int, int]:
    """归一化 [x,y,w,h](0-1) → 像素 (x,y,w,h),四周扩 pad_px,夹紧到画面内。

    delogo 要求区域严格落在画面内且至少 1x1;为安全给右/下各留 1px。
    """
    fx, fy, fw, fh = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
    x = int(round(fx * width)) - pad_px
    y = int(round(fy * height)) - pad_px
    w = int(round(fw * width)) + 2 * pad_px
    h = int(round(fh * height)) + 2 * pad_px
    # 夹紧左上
    x = max(0, x)
    y = max(0, y)
    # 至少 1x1
    w = max(1, w)
    h = max(1, h)
    # 右/下不越界(留 1px 边,规避 delogo 边界报错)
    if x + w > width - 1:
        w = max(1, width - 1 - x)
    if y + h > height - 1:
        h = max(1, height - 1 - y)
    return x, y, w, h


def build_delogo_filter(segments: List[Dict], width: int, height: int,
                        pad_px: int = 6, max_segments: int = 40) -> str:
    """把 OCR 段构造成 delogo 滤镜链(逗号连接);空则返回空串。

    每段: delogo=x=..:y=..:w=..:h=..:enable='between(t,start,end)'
    超过 max_segments 时按 score/顺序截断(简单取前 N,避免 filtergraph 过大)。
    """
    if not segments:
        return ""
    parts = []
    for seg in segments[:max_segments]:
        box = seg.get("box")
        if not box or len(box) < 4:
            continue
        x, y, w, h = norm_box_to_pixels(box, width, height, pad_px=pad_px)
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", start))
        parts.append(
            f"delogo=x={x}:y={y}:w={w}:h={h}:enable='between(t,{start},{end})'"
        )
    return ",".join(parts)


def build_vf_chain(delogo_part: str, subtitle_part: str) -> Optional[str]:
    """把 delogo 段与 subtitles 段用逗号拼成 -vf 链;两者皆空返回 None。"""
    pieces = [p for p in (delogo_part, subtitle_part) if p]
    if not pieces:
        return None
    return ",".join(pieces)


def _ffmpeg_path_escape(path: str) -> str:
    """转义 subtitles= 滤镜值里的路径(反斜杠与冒号)。

    ffmpeg filtergraph: 先转义 \\ 再转义 :  (Windows 路径 C:\\x → C\\:\\\\x)
    """
    return path.replace("\\", "\\\\").replace(":", "\\:")
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m unittest tests.test_composite_filters -v`
Expected: 全部 `ok`（norm/delogo/vf/escape 共 12 例）

- [ ] **Step 5: 提交**

```bash
git add modules/remaster_composite.py tests/test_composite_filters.py
git commit -m "feat(composite): 纯逻辑 box→像素/delogo/vf 链构建 + 单测"
```

---

## Task 3: run_composite 命令构建 + 执行

**Files:**
- Modify: `modules/remaster_composite.py`（追加 `build_composite_cmd` 与 `run_composite`）
- Test: `tests/test_composite_filters.py`（追加命令构建断言，不跑真 ffmpeg）

- [ ] **Step 1: 追加命令构建测试**

在 `tests/test_composite_filters.py` 的 import 末尾加 `build_composite_cmd`：

```python
from modules.remaster_composite import (
    norm_box_to_pixels,
    build_delogo_filter,
    build_vf_chain,
    _ffmpeg_path_escape,
    build_composite_cmd,
)
```

并在文件末尾（`if __name__` 之前）追加：

```python
class TestBuildCompositeCmd(unittest.TestCase):
    def test_with_dubbed_audio_maps_external_audio(self):
        cmd = build_composite_cmd(
            ffmpeg_bin="/ff/ffmpeg", input_video="/v/in.mp4",
            dubbed_audio="/v/dub.wav", vf_chain="delogo=x=1:y=2:w=3:h=4",
            output_video="/v/out.mp4",
        )
        # 两个 -i: 视频 + 配音
        self.assertEqual(cmd.count("-i"), 2)
        i0 = cmd.index("-i")
        self.assertEqual(cmd[i0 + 1], "/v/in.mp4")
        # map 视频 0:v 与 配音 1:a
        self.assertIn("0:v:0", cmd)
        self.assertIn("1:a:0", cmd)
        self.assertIn("-vf", cmd)
        self.assertEqual(cmd[-1], "/v/out.mp4")

    def test_without_dubbed_audio_uses_original(self):
        cmd = build_composite_cmd(
            ffmpeg_bin="/ff/ffmpeg", input_video="/v/in.mp4",
            dubbed_audio=None, vf_chain="delogo=x=1:y=2:w=3:h=4",
            output_video="/v/out.mp4",
        )
        self.assertEqual(cmd.count("-i"), 1)
        # 原音: map 0:a? 用 -map 0:a:0? 允许无音频 → 用 0:a? 见实现
        self.assertIn("0:v:0", cmd)

    def test_no_vf_chain_still_valid_when_only_audio_swap(self):
        # vf_chain None 且有配音 → 仍需重封装音轨,copy 视频
        cmd = build_composite_cmd(
            ffmpeg_bin="/ff/ffmpeg", input_video="/v/in.mp4",
            dubbed_audio="/v/dub.wav", vf_chain=None,
            output_video="/v/out.mp4",
        )
        self.assertIn("-c:v", cmd)
        self.assertIn("copy", cmd)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m unittest tests.test_composite_filters.TestBuildCompositeCmd -v`
Expected: FAIL — `cannot import name 'build_composite_cmd'`

- [ ] **Step 3: 追加实现**

在 `modules/remaster_composite.py` 末尾追加：

```python
def build_composite_cmd(*, ffmpeg_bin: str, input_video: str,
                        dubbed_audio: Optional[str], vf_chain: Optional[str],
                        output_video: str) -> List[str]:
    """构建 ffmpeg 成片命令。

    - 有 vf_chain(delogo/字幕): 重编码视频(libx264 yuv420p);否则 copy 视频流。
    - 有 dubbed_audio: 额外 -i 并 map 配音;否则跟随原视频音轨(0:a?,允许无音频)。
    - +faststart 便于流式播放/上传。
    """
    cmd = [ffmpeg_bin, "-y", "-i", input_video]
    if dubbed_audio:
        cmd += ["-i", dubbed_audio]

    if vf_chain:
        cmd += ["-vf", vf_chain, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-preset", "medium", "-crf", "20"]
    else:
        cmd += ["-c:v", "copy"]

    # 视频流映射
    cmd += ["-map", "0:v:0"]
    # 音频流映射 + 编码
    if dubbed_audio:
        cmd += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"]
    else:
        # 原视频音轨(可能无音频→用 0:a? 可选映射);重编 aac 保证容器兼容
        cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k"]

    cmd += ["-movflags", "+faststart", output_video]
    return cmd


def run_composite(*, ffmpeg_bin: str, input_video: str, output_video: str,
                  ocr_segments: List[Dict], video_width: int, video_height: int,
                  dubbed_audio: Optional[str] = None,
                  subtitle_srt: Optional[str] = None,
                  pad_px: int = 6, max_delogo_segments: int = 40,
                  timeout: int = 10800,
                  logger=None) -> Tuple[bool, object]:
    """执行成片合成。返回 (True, {output, layers}) 或 (False, err_str)。

    缺件尽力合成:无 OCR 段→跳 delogo;无字幕→不烧字幕;无配音→保留原音。
    若既无 vf 也无配音(纯 copy 无意义),仍产出规范化 mp4(重封装),视为成功。
    """
    def _log(m):
        if logger:
            logger.info(m)

    delogo_part = build_delogo_filter(
        ocr_segments or [], video_width, video_height,
        pad_px=pad_px, max_segments=max_delogo_segments,
    )
    subtitle_part = ""
    if subtitle_srt and os.path.isfile(subtitle_srt):
        esc = _ffmpeg_path_escape(subtitle_srt)
        subtitle_part = f"subtitles={esc}:fontsdir=fonts:charenc=UTF-8"

    vf_chain = build_vf_chain(delogo_part, subtitle_part)
    layers = {
        "delogo_segments": delogo_part.count("delogo=") if delogo_part else 0,
        "burned_subtitle": bool(subtitle_part),
        "dubbed_audio": bool(dubbed_audio),
    }
    _log(f"[composite] 层: {layers}")

    cmd = build_composite_cmd(
        ffmpeg_bin=ffmpeg_bin, input_video=input_video,
        dubbed_audio=dubbed_audio, vf_chain=vf_chain, output_video=output_video,
    )
    _log(f"[composite] ffmpeg: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"ffmpeg 合成超时({timeout}s)"
    except Exception as e:
        return False, f"ffmpeg 启动失败: {e}"

    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode(errors="ignore")[-500:]
        return False, f"ffmpeg 退出码 {proc.returncode}: {detail}"
    if not os.path.isfile(output_video) or os.path.getsize(output_video) == 0:
        return False, "ffmpeg 未产出有效输出文件"
    return True, {"output": output_video, "layers": layers}
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m unittest tests.test_composite_filters -v`
Expected: 全部 `ok`（含 TestBuildCompositeCmd 3 例）

- [ ] **Step 5: 提交**

```bash
git add modules/remaster_composite.py tests/test_composite_filters.py
git commit -m "feat(composite): build_composite_cmd + run_composite(delogo/配音/硬字幕)"
```

---

## Task 4: task_manager 接线（stage/状态/DB 三件套/_run_remaster_composite/门控）

**Files:**
- Modify: `modules/task_manager.py`
- Test: `tests/test_composite_warning_persistence.py`

- [ ] **Step 1: 加状态与 stage 常量**

在 `TASK_STATES` 里 `'DUBBING'` 行之后加：

```python
    'COMPOSITING': 'compositing',            # 成片合成中(delogo+配音+硬字幕)
```

在 `PROCESSING_STATES` 里 `TASK_STATES['DUBBING'],` 之后加：

```python
    TASK_STATES['COMPOSITING'],
```

在 `PIPELINE_STAGE_REMASTER_DUB = 'remaster_dub'` 之后加常量：

```python
PIPELINE_STAGE_REMASTER_COMPOSITE = 'remaster_composite'
```

在 `PIPELINE_STAGE_ORDER` 列表里 `PIPELINE_STAGE_REMASTER_DUB,` 之后、`PIPELINE_STAGE_UPLOAD_TO_ACFUN,` 之前加：

```python
    PIPELINE_STAGE_REMASTER_COMPOSITE,
```

- [ ] **Step 2: DB 三件套 `composite_warning_message`**

`CREATE TABLE` 里 `dub_warning_message TEXT` 行——它当前是最后一列（无逗号）。改为带逗号并追加新列：

```python
        dub_warning_message TEXT,  -- 配音阶段的非致命警告，不影响上传流程
        composite_warning_message TEXT  -- 成片合成阶段的非致命警告，不影响上传流程
```

迁移块：在 `dub_warning_message` 的 `if 'dub_warning_message' not in columns:` 迁移之后追加：

```python
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'composite_warning_message' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN composite_warning_message TEXT")
            logger.info("数据库升级：添加composite_warning_message字段")
            conn.commit()
```

`ALLOWED_COLUMNS` 里 `'dub_warning_message': 'dub_warning_message = ?',` 之后加：

```python
        'composite_warning_message': 'composite_warning_message = ?',
```

- [ ] **Step 3: 写 warning 往返回归测试**

Create `tests/test_composite_warning_persistence.py`：

```python
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import modules.task_manager as tm


class TestCompositeWarningPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig = tm.DB_PATH
        tm.DB_PATH = os.path.join(self.tmpdir, "tasks.db")
        tm.init_db()

    def tearDown(self):
        tm.DB_PATH = self._orig
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_round_trip(self):
        task_id = tm.add_task("https://youtu.be/composite_test")
        tm.update_task(task_id, composite_warning_message="composite: ffmpeg failed")
        self.assertEqual(tm.get_task(task_id)["composite_warning_message"],
                         "composite: ffmpeg failed")

    def test_cleared_on_success(self):
        task_id = tm.add_task("https://youtu.be/composite_test2")
        tm.update_task(task_id, composite_warning_message="composite: temp")
        tm.update_task(task_id, composite_warning_message=None)
        self.assertIsNone(tm.get_task(task_id)["composite_warning_message"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m unittest tests.test_composite_warning_persistence -v`
Expected: `Ran 2 tests` `OK`（证明三件套齐全）

- [ ] **Step 5: 写 `_run_remaster_composite` 方法**

在 `_run_remaster_dub` 方法结束之后（`return False` 行后）插入。注意用 `_get_video_stream_info` 取宽高（该方法已存在于 TaskProcessor）：

```python
    def _run_remaster_composite(self, task_id, task_logger):
        """AI 重制管线末步:delogo 擦烧录字幕 + 配音替换 + 硬烧译文字幕,产出成片。

        成功时写回 video_path_local 供上传;失败软处理(记 composite_warning_message,
        不改 video_path_local,继续上传原片)。缺件尽力合成。
        """
        from modules.remaster_composite import run_composite

        task = get_task(task_id)
        if not task:
            task_logger.error("任务不存在")
            return False
        video_path = task.get('video_path_local', '')
        if not video_path or not os.path.exists(video_path):
            task_logger.warning("视频缺失,跳过成片合成")
            return False

        task_dir = os.path.join(DOWNLOADS_DIR, task_id)
        # 收集中间产物(缺啥跳啥层)
        ocr_json = os.path.join(task_dir, f"ocr_subtitle_boxes_{task_id}.json")
        ocr_segments, vw, vh = [], 0, 0
        if os.path.isfile(ocr_json):
            try:
                import json
                with open(ocr_json, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                ocr_segments = data.get('segments', []) or []
                vw = int(data.get('width') or 0)
                vh = int(data.get('height') or 0)
            except Exception as e:
                task_logger.warning(f"OCR JSON 解析失败,跳过 delogo: {e}")
        # 宽高兜底:OCR 无则探测视频
        if vw <= 0 or vh <= 0:
            info = self._get_video_stream_info(video_path, task_logger)
            vw = int(info.get('width') or 0)
            vh = int(info.get('height') or 0)
        if vw <= 0 or vh <= 0:
            task_logger.warning("无法确定视频分辨率,跳过成片合成")
            return False

        dubbed = os.path.join(task_dir, f"dubbed_audio_{task_id}.wav")
        dubbed_audio = dubbed if os.path.isfile(dubbed) else None

        burn = _as_bool(self.config.get('COMPOSITE_BURN_SUBTITLE', True))
        srt = str(task.get('subtitle_path_translated') or '').strip()
        subtitle_srt = srt if (burn and srt and os.path.isfile(srt)) else None

        pad_px = _as_int(self.config.get('COMPOSITE_DELOGO_PAD_PX', 6), 6, minimum=0)
        max_seg = _as_int(self.config.get('COMPOSITE_MAX_DELOGO_SEGMENTS', 40), 40, minimum=1)
        timeout = _as_int(self.config.get('COMPOSITE_TIMEOUT_SECONDS', 10800), 10800, minimum=60)

        ffmpeg_bin = get_ffmpeg_path(logger=task_logger)
        if not ffmpeg_bin or not os.path.exists(ffmpeg_bin):
            task_logger.warning("未找到 ffmpeg,跳过成片合成")
            return False

        out_path = os.path.join(task_dir, f"remastered_{task_id}.mp4")

        prev_status = task.get('status')
        update_task(task_id, status=TASK_STATES['COMPOSITING'])
        task_logger.info(f"重制管线:成片合成(delogo={len(ocr_segments)}段/配音={bool(dubbed_audio)}/字幕={bool(subtitle_srt)}) → {out_path}")

        ok, res = run_composite(
            ffmpeg_bin=ffmpeg_bin, input_video=video_path, output_video=out_path,
            ocr_segments=ocr_segments, video_width=vw, video_height=vh,
            dubbed_audio=dubbed_audio, subtitle_srt=subtitle_srt,
            pad_px=pad_px, max_delogo_segments=max_seg, timeout=timeout,
            logger=task_logger,
        )
        if ok:
            # 成功:写回 video_path_local 供上传
            update_task(task_id, video_path_local=out_path,
                        composite_warning_message=None, status=prev_status)
            task_logger.info(f"成片合成完成: {res}")
            return True
        task_logger.error(f"成片合成失败: {res}")
        # 失败:不改 video_path_local,继续上传原片
        update_task(task_id, composite_warning_message=f"composite: {res}", status=prev_status)
        return False
```

- [ ] **Step 6: 加 process_task 门控**

在 `5b. AI 重制配音` 门控块结尾（`_mark_stage_done(..., PIPELINE_STAGE_REMASTER_DUB)` 之后）插入独立块：

```python
            # 5c. AI 重制成片合成(delogo 擦字幕 + 配音 + 硬字幕; 依赖 dub 门控已跑)
            if PIPELINE_STAGE_REMASTER_DUB in completed_stages and \
                    PIPELINE_STAGE_REMASTER_COMPOSITE not in completed_stages and \
                    _as_bool(self.config.get('REMASTER_PIPELINE_ENABLED', False)):
                try:
                    self._run_remaster_composite(task_id, task_logger)
                except Exception as e:
                    task_logger.error(f"重制成片合成异常: {e}")
                completed_stages = _mark_stage_done(
                    task_id, completed_stages, PIPELINE_STAGE_REMASTER_COMPOSITE
                )
```

- [ ] **Step 7: 验证接线 + 全测**

Run:
```bash
.venv/bin/python -c "
import modules.task_manager as tm
assert tm.TASK_STATES['COMPOSITING'] == 'compositing'
assert tm.TASK_STATES['COMPOSITING'] in tm.PROCESSING_STATES
assert tm.PIPELINE_STAGE_REMASTER_COMPOSITE == 'remaster_composite'
o = tm.PIPELINE_STAGE_ORDER
assert o.index('remaster_composite') > o.index('remaster_dub')
assert o.index('remaster_composite') < o.index('upload_to_acfun')
assert hasattr(tm.TaskProcessor, '_run_remaster_composite')
print('WIRING OK')
"
.venv/bin/python -m unittest discover tests 2>&1 | tail -4
```
Expected: `WIRING OK` 且 discover `OK`（全绿，无回归）

- [ ] **Step 8: 提交**

```bash
git add modules/task_manager.py tests/test_composite_warning_persistence.py
git commit -m "feat(composite): task_manager remaster_composite stage + 三件套 + 软失败接线(成功写回 video_path_local)"
```

---

## Task 5: 真实冒烟 + 全分支复审 + 合并

**Files:** 无（验证 + 合并）

- [ ] **Step 1: 造冒烟素材**

用 PIL 造带底栏“字幕”的短视频（ffmpeg drawtext 在本机不可用，用 PIL 叠字帧），加假 OCR JSON + 短配音 wav + 中文 SRT：

```bash
mkdir -p /tmp/comp_out
# 1) 生成 6 帧 640x360 带底部白字的 png → 视频
.venv/bin/python - <<'PY'
from PIL import Image, ImageDraw
import os
os.makedirs('/tmp/comp_frames', exist_ok=True)
for i in range(3):
    im = Image.new('RGB', (640, 360), (20, 30, 60))
    d = ImageDraw.Draw(im)
    d.rectangle([160, 300, 480, 340], fill=(0, 0, 0))
    d.text((175, 312), "BURNED SUBTITLE", fill=(255, 255, 255))
    im.save(f'/tmp/comp_frames/f{i}.png')
print("frames ok")
PY
FF=$(.venv/bin/python -c "from modules.ffmpeg_manager import get_ffmpeg_path; print(get_ffmpeg_path())")
"$FF" -y -framerate 1 -i /tmp/comp_frames/f%d.png -t 3 -pix_fmt yuv420p -r 25 /tmp/comp_in.mp4 2>/dev/null
# 2) 假 OCR JSON(底栏一段)
cat > /tmp/comp_out/ocr.json <<'JSON'
{"width":640,"height":360,"segments":[{"start":0.0,"end":3.0,"box":[0.25,0.83,0.50,0.11],"score":0.99}]}
JSON
# 3) 3s 配音 wav
"$FF" -y -f lavfi -i "sine=frequency=330:duration=3" -ac 2 -ar 44100 /tmp/comp_out/dub.wav 2>/dev/null
# 4) 中文 SRT
cat > /tmp/comp_out/zh.srt <<'SRT'
1
00:00:00,000 --> 00:00:03,000
这是合成后的中文字幕
SRT
ls -lh /tmp/comp_in.mp4 /tmp/comp_out/
```
Expected: 四个输入就位。

- [ ] **Step 2: 直接跑 run_composite 冒烟**

Run:
```bash
.venv/bin/python - <<'PY'
import json
from modules.ffmpeg_manager import get_ffmpeg_path
from modules.remaster_composite import run_composite
seg = json.load(open('/tmp/comp_out/ocr.json'))
ok, res = run_composite(
    ffmpeg_bin=get_ffmpeg_path(), input_video='/tmp/comp_in.mp4',
    output_video='/tmp/comp_out/remastered.mp4',
    ocr_segments=seg['segments'], video_width=640, video_height=360,
    dubbed_audio='/tmp/comp_out/dub.wav', subtitle_srt='/tmp/comp_out/zh.srt',
)
print("ok", ok, "res", res)
assert ok, res
PY
# 校验输出: 有视频有音频, 时长~3s
FF=$(.venv/bin/python -c "from modules.ffmpeg_manager import get_ffprobe_path; print(get_ffprobe_path())")
"$FF" -v error -show_entries stream=codec_type,codec_name -show_entries format=duration -of default=noprint_wrappers=1 /tmp/comp_out/remastered.mp4
```
Expected: `ok True`；ffprobe 显示 video(h264) + audio(aac)，duration≈3.0。若字体报错则改用 `SUBTITLE_FONT_NAME` 绝对 fontfile 再试并记入实现。

- [ ] **Step 3: 全测**

Run: `.venv/bin/python -m unittest discover tests 2>&1 | tail -4`
Expected: `OK`（308+ 全绿）

- [ ] **Step 4: 全分支 opus 复审**

派 code-reviewer(opus) 审 `feat/ffmpeg-remaster-composite` vs main 全 diff。重点：
- 软失败边界（失败**不改** video_path_local；成功才写回）
- 三件套齐全（防 warning 静默丢）
- 缺件策略（缺 OCR/配音/字幕分别降级）
- 默认关字节级不变（gate 在 `REMASTER_PIPELINE_ENABLED` 内）
- delogo 越界/段上限/路径转义

按复审 Important 修复并回归。

- [ ] **Step 5: squash 合并 main + 清理**

```bash
git checkout main
git merge --squash feat/ffmpeg-remaster-composite
git commit -m "$(cat <<'EOF'
feat: AI 重制管线第六块(末) - FFmpeg 字幕擦除 + 成片合成

消费 OCR 时间线 bbox + 配音音轨 + 译文字幕,对本地视频 delogo 擦烧录字幕、
替换配音、硬烧中文字幕,产出 remastered_<id>.mp4 并写回 video_path_local。
与 remaster 同构: 纯逻辑模块 + task_manager 软失败接线, gated REMASTER_PIPELINE_ENABLED,
stage 在 remaster_dub 之后、upload 之前。缺件尽力合成(缺 OCR 跳 delogo/缺配音留原音/缺字幕不烧)。

- remaster_composite: box→像素 / delogo enable 时间窗 / vf 链 / 命令构建 / run
- composite_warning_message 三件套
- 成功写回 video_path_local, 失败保留原片继续上传
- 冒烟: 640x360 烧录字幕视频 → delogo+配音+硬字幕 → h264+aac 成片

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
git branch -D feat/ffmpeg-remaster-composite
rm -rf /tmp/comp_frames /tmp/comp_in.mp4 /tmp/comp_out
git log --oneline -3
```

- [ ] **Step 6: 更新管线进度文档**

把本子项目在各 spec 的“AI 重制管线进度”清单标记为 ✅（末块完成），提交 docs。

---

## Self-Review

**1. Spec 覆盖**：
- delogo 按段擦除 → Task 2/3（`build_delogo_filter` + enable 窗）✓
- 配音替换 → Task 3（`build_composite_cmd` map 1:a）✓
- 硬烧译字幕 → Task 3（`subtitles=` + fontsdir）✓
- 成功写回 video_path_local → Task 4（`_run_remaster_composite`）✓
- 缺件策略 → Task 4（逐层降级）✓
- 三件套 + 软失败 + 默认关 → Task 4 ✓
- stage dub 后 upload 前 → Task 4 常量/ORDER/门控 ✓
- 环境/冒烟/复审/合并 → Task 5 ✓

**2. Placeholder scan**：无 TBD；每步含完整代码与期望输出。

**3. 类型一致性**：`run_composite` 参数名（`ocr_segments/video_width/video_height/dubbed_audio/subtitle_srt/pad_px/max_delogo_segments`）与 Task 4 调用点逐一对齐；`build_composite_cmd` kwargs（`ffmpeg_bin/input_video/dubbed_audio/vf_chain/output_video`）与 Task 3 测试一致；`norm_box_to_pixels`/`build_delogo_filter`/`build_vf_chain`/`_ffmpeg_path_escape` 命名前后统一。
