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

    def transcribe(self, *, video_path, output_srt_path, language="auto",
                   model="large-v3", device="cpu", compute_type="int8",
                   batch_size=16, task_id=None, progress_callback=None, timeout=7200):
        """返回 (True, {ok, srt, segments, ...}) 或 (False, error_msg)。"""
        self.task_id = task_id
        if not self.python_bin or not os.path.isfile(self.python_bin):
            return False, f"asr-venv python 未安装或路径无效: {self.python_bin}"
        if not self.runner_path or not os.path.isfile(self.runner_path):
            return False, f"whisperx runner 不存在: {self.runner_path}"
        if not video_path or not os.path.exists(video_path):
            return False, f"视频文件不存在: {video_path}"

        cmd = [self.python_bin, self.runner_path,
               "--video", video_path,
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
