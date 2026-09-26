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
    x = max(0, x)
    y = max(0, y)
    w = max(1, w)
    h = max(1, h)
    if x + w > width - 1:
        w = max(1, width - 1 - x)
    if y + h > height - 1:
        h = max(1, height - 1 - y)
    return x, y, w, h


def build_delogo_filter(segments: List[Dict], width: int, height: int,
                        pad_px: int = 6, max_segments: int = 40) -> str:
    """把 OCR 段构造成 delogo 滤镜链(逗号连接);空则返回空串。"""
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
        if end <= start:
            continue
        parts.append(
            f"delogo=x={x}:y={y}:w={w}:h={h}:enable='between(t,{start},{end})'"
        )
    return ",".join(parts)


def build_erase_filters(segments: List[Dict], width: int, height: int,
                        pad_px: int = 6, max_delogo_segments: int = 40,
                        max_box_segments: int = 240,
                        wide_width_ratio: float = 0.45) -> Tuple[str, Dict[str, int]]:
    """构建擦除滤镜链:宽字幕带用黑色遮条,窄区域用 delogo。

    宽带(归一化宽 >= wide_width_ratio,常见于满幅烧录原字幕)用 delogo 会变成
    横贯画面的涂抹带(即"字幕马赛克");drawbox 实心遮条干净且开销极低,
    单独放宽数量上限,避免 40 段 delogo 上限导致擦除时有时无。
    返回 (滤镜链, 统计{delogo, box, skipped})。
    """
    parts: List[str] = []
    stats = {"delogo": 0, "box": 0, "skipped": 0}
    for seg in segments or []:
        box = seg.get("box")
        if not box or len(box) < 4:
            continue
        x, y, w, h = norm_box_to_pixels(box, width, height, pad_px=pad_px)
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", start))
        if end <= start:
            continue
        enable = f"enable='between(t,{start},{end})'"
        norm_w = float(box[2])
        if norm_w >= max(0.0, wide_width_ratio):
            if stats["box"] >= max_box_segments:
                stats["skipped"] += 1
                continue
            parts.append(
                f"drawbox=x={x}:y={y}:w={w}:h={h}:color=black@1.0:t=fill:{enable}"
            )
            stats["box"] += 1
        else:
            if stats["delogo"] >= max_delogo_segments:
                stats["skipped"] += 1
                continue
            parts.append(f"delogo=x={x}:y={y}:w={w}:h={h}:{enable}")
            stats["delogo"] += 1
    return ",".join(parts), stats


def build_vf_chain(delogo_part: str, subtitle_part: str) -> Optional[str]:
    """把 delogo 段与 subtitles 段用逗号拼成 -vf 链;两者皆空返回 None。"""
    pieces = [p for p in (delogo_part, subtitle_part) if p]
    if not pieces:
        return None
    return ",".join(pieces)


def _ffmpeg_path_escape(path: str) -> str:
    """转义 subtitles= 滤镜值里的路径(反斜杠与冒号)。"""
    return path.replace("\\", "\\\\").replace(":", "\\:")


def _ffmpeg_has_filter(ffmpeg_bin: str, filter_name: str) -> bool:
    """检测 ffmpeg 是否包含某视频滤镜(如 subtitles/delogo)。"""
    try:
        r = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-filters"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=20,
        )
        return r.returncode == 0 and filter_name in (r.stdout or "")
    except Exception:
        return False


def build_composite_cmd(*, ffmpeg_bin: str, input_video: str,
                        dubbed_audio: Optional[str], vf_chain: Optional[str],
                        output_video: str) -> List[str]:
    """构建 ffmpeg 成片命令。

    - 有 vf_chain(delogo/字幕): 重编码视频(libx264 yuv420p);否则 copy 视频流。
    - 有 dubbed_audio: 额外 -i 并 map 配音;否则跟随原视频音轨(0:a:0?,允许无音频)。
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

    cmd += ["-map", "0:v:0"]
    if dubbed_audio:
        cmd += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k"]

    cmd += ["-movflags", "+faststart", output_video]
    return cmd


def run_composite(*, ffmpeg_bin: str, input_video: str, output_video: str,
                  ocr_segments: List[Dict], video_width: int, video_height: int,
                  dubbed_audio: Optional[str] = None,
                  subtitle_srt: Optional[str] = None,
                  pad_px: int = 6, max_delogo_segments: int = 40,
                  wide_band_ratio: float = 0.45,
                  timeout: int = 10800,
                  logger=None) -> Tuple[bool, object]:
    """执行成片合成。返回 (True, {output, layers}) 或 (False, err_str)。

    缺件尽力合成:无 OCR 段→跳擦除;无字幕→不烧字幕;无配音→保留原音。
    subtitle_srt 接受 .srt(force_style 简化样式)或 .ass/.ssa(按文件自带样式渲染)。
    滤镜顺序恒为 擦除→字幕:中文字幕永远在擦除层之上。
    """
    def _log(m):
        if logger:
            logger.info(m)

    erase_part, erase_stats = build_erase_filters(
        ocr_segments or [], video_width, video_height,
        pad_px=pad_px, max_delogo_segments=max_delogo_segments,
        wide_width_ratio=wide_band_ratio,
    )
    subtitle_part = ""
    if subtitle_srt and os.path.isfile(subtitle_srt):
        if _ffmpeg_has_filter(ffmpeg_bin, "subtitles"):
            # 绝对 fontsdir + force_style;路径转义后单引号包裹,并去掉路径中的单引号
            try:
                from .utils import get_app_root_dir
                fonts_dir = os.path.join(get_app_root_dir(), "fonts")
            except Exception:
                fonts_dir = "fonts"
            srt_abs = os.path.abspath(subtitle_srt).replace("'", "")
            fonts_abs = os.path.abspath(fonts_dir).replace("'", "")
            srt_esc = _ffmpeg_path_escape(srt_abs)
            fonts_esc = _ffmpeg_path_escape(fonts_abs)
            sub_ext = os.path.splitext(srt_abs)[1].lower()
            if sub_ext in (".ass", ".ssa"):
                # ASS/SSA 自带校准样式(比例字号/底距),libass 直接按文件样式渲染
                subtitle_part = (
                    f"subtitles='{srt_esc}':fontsdir='{fonts_esc}':charenc=UTF-8"
                )
            else:
                # 默认中文字幕底栏样式(与项目硬烧风格接近的简化版)
                force = (
                    "FontName=Noto Sans CJK SC,FontSize=22,PrimaryColour=&H00FFFFFF,"
                    "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,"
                    "Alignment=2,MarginV=36"
                )
                subtitle_part = (
                    f"subtitles='{srt_esc}':fontsdir='{fonts_esc}':charenc=UTF-8:"
                    f"force_style='{force}'"
                )
        else:
            # 本机/项目 ffmpeg 可能未编 libass(如 brew 默认);硬烧软跳过,仍完成 delogo+配音
            _log(
                "[composite] 当前 ffmpeg 无 subtitles 滤镜(需 libass),跳过硬烧字幕;"
                "delogo/配音仍继续"
            )

    vf_chain = build_vf_chain(erase_part, subtitle_part)
    layers = {
        "delogo_segments": erase_stats["delogo"],
        "erase_box_segments": erase_stats["box"],
        "erase_skipped": erase_stats["skipped"],
        "burned_subtitle": bool(subtitle_part),
        "dubbed_audio": bool(dubbed_audio),
    }
    _log(f"[composite] 层: {layers}")

    # 无可合成层 → 软跳过,不跑 ffmpeg,不产出无意义 remastered 文件
    if (
        layers["delogo_segments"] == 0
        and layers["erase_box_segments"] == 0
        and not layers["burned_subtitle"]
        and not layers["dubbed_audio"]
    ):
        _log("[composite] 无可合成层(无 delogo/遮条/配音/硬字幕),跳过 ffmpeg")
        return True, {"skipped": True, "layers": layers}

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
