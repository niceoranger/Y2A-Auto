#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OcrLocator —— subprocess 适配器,调 modules/ocr_runner.py(用 asr-venv python)。

与 DemucsSeparator 同构:逐行解析 stdout 进度 + 末行 JSON 结果。
"""
import json
import logging
import os
import subprocess
import threading


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
               sample_interval_sec=0.5, lang="ch", device="cpu",
               iou_threshold=0.5, bottom_band_min_y=0.6,
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
               "--sample-interval", str(sample_interval_sec if sample_interval_sec is not None else 0.5),
               "--lang", str(lang or "ch"),
               "--device", str(device or "cpu"),
               "--iou-threshold", str(iou_threshold if iou_threshold is not None else 0.5),
               "--bottom-band-min-y", str(bottom_band_min_y if bottom_band_min_y is not None else 0.6)]
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
