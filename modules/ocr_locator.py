#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OcrLocator —— subprocess 适配器,调 modules/ocr_runner.py(用 asr-venv python)。

与 DemucsSeparator 同构:逐行解析 stdout 进度 + 末行 JSON 结果。
"""
import json
import logging
import os
import re
import subprocess
import threading

# ISO 639 语言码 → PaddleOCR lang 参数(未覆盖的语言回退配置值)
_PADDLE_LANG_MAP = {
    "zh": "ch",
    "en": "en",
    "ja": "japan",
    "ko": "korean",
    "fr": "french",
    "de": "german",
    "ru": "russian",
}

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def guess_language_from_text(sample: str) -> str:
    """从文本内容粗判语言:'zh' / 'en' / ''(无法判断)。

    只区分 CJK 与拉丁两大类——用于给 OCR 选模型,不追求精确语种。
    """
    sample = str(sample or "")
    cjk = len(_CJK_RE.findall(sample))
    latin = len(_LATIN_RE.findall(sample))
    total = cjk + latin
    if total < 10:
        return ""
    if cjk / total >= 0.2:
        return "zh"
    if latin / total >= 0.8:
        return "en"
    return ""


def guess_language_from_srt_file(srt_path: str, max_bytes: int = 200_000) -> str:
    """读 SRT 正文猜语言。时间轴/序号行只含数字与冒号,不干扰字母统计。"""
    try:
        with open(srt_path, "r", encoding="utf-8", errors="ignore") as f:
            sample = f.read(max_bytes)
        return guess_language_from_text(sample)
    except Exception:
        return ""


def resolve_ocr_lang(detected_lang: str, fallback: str = "ch") -> str:
    """把检测到的语言码映射为 PaddleOCR lang;未知/空/auto 回退 fallback。"""
    lang = str(detected_lang or "").strip().lower()
    if "-" in lang:
        lang = lang.split("-")[0]
    return _PADDLE_LANG_MAP.get(lang, str(fallback or "ch").strip() or "ch")


class OcrLocator:
    def __init__(self, python_bin: str, runner_path: str):
        self.python_bin = python_bin
        self.runner_path = runner_path
        self.logger = None
        self.task_id = None

    def _log(self, msg):
        if self.logger:
            self.logger.info(msg)
        else:
            logging.getLogger("ocr_locator").info(msg)

    def locate(self, *, video_path, output_json, task_id,
               sample_interval_sec=2.0, lang="ch", device="cpu",
               iou_threshold=0.5, bottom_band_min_y=0.6, min_rec_score=0.6,
               workers=1,
               progress_callback=None, timeout=3600):
        """返回 (True, {ok, boxes, ...}) 或 (False, error_msg)。"""
        self.task_id = task_id
        if not self.python_bin or not os.path.isfile(self.python_bin):
            return False, f"asr-venv python 未安装或路径无效: {self.python_bin}"
        if not self.runner_path or not os.path.isfile(self.runner_path):
            return False, f"ocr runner 不存在: {self.runner_path}"
        if not video_path or not os.path.exists(video_path):
            return False, f"视频文件不存在: {video_path}"

        cmd = [self.python_bin, self.runner_path,
               "--video", video_path,
               "--output", output_json,
               "--task-id", str(task_id),
               "--sample-interval", str(sample_interval_sec if sample_interval_sec is not None else 2.0),
               "--lang", str(lang or "ch"),
               "--device", str(device or "cpu"),
               "--workers", str(max(1, int(workers or 1))),
               "--iou-threshold", str(iou_threshold if iou_threshold is not None else 0.5),
               "--bottom-band-min-y", str(bottom_band_min_y if bottom_band_min_y is not None else 0.6),
               "--min-rec-score", str(min_rec_score if min_rec_score is not None else 0.6)]
        self._log(f"调用 ocr runner: {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1, env=os.environ.copy())
        except FileNotFoundError as e:
            return False, f"启动 runner 失败: {e}"

        last_json = None
        tail = []
        timed_out = {"flag": False}

        def _kill_on_timeout():
            timed_out["flag"] = True
            try:
                proc.kill()
            except Exception:
                pass

        timer = threading.Timer(float(timeout), _kill_on_timeout)
        timer.daemon = True
        timer.start()
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
            proc.wait()
        except Exception as e:
            try:
                proc.kill(); proc.wait()
            except Exception:
                pass
            return False, f"runner 子进程异常: {e}"
        finally:
            timer.cancel()
            if proc.stdout:
                proc.stdout.close()

        if timed_out["flag"]:
            return False, f"ocr 超时({timeout}s)"

        if proc.returncode != 0:
            summary = "\n".join(tail[-8:])
            return False, f"runner 退出码 {proc.returncode};末尾输出:\n{summary}"

        if isinstance(last_json, dict) and last_json.get("ok"):
            return True, last_json
        err = (last_json or {}).get("error") if isinstance(last_json, dict) else None
        return False, err or f"runner 未返回成功 JSON;末尾:\n{''.join(tail[-6:])}"
