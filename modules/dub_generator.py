#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DubGenerator —— subprocess 适配器,调 modules/dub_runner.py(用 asr-venv python)。

与 WhisperXAsr 同构:逐行解析 stdout 进度 + 末行 JSON 结果。
"""
import json
import logging
import os
import subprocess


class DubGenerator:
    def __init__(self, python_bin: str, runner_path: str):
        self.python_bin = python_bin
        self.runner_path = runner_path
        self.logger = None
        self.task_id = None

    def _log(self, msg):
        if self.logger:
            self.logger.info(msg)
        else:
            logging.getLogger("dub_generator").info(msg)

    def generate(self, *, translated_srt_path, no_vocals_wav, output_path, task_id,
                 speaker="Ana Florence", language="zh", device="cpu", max_tempo=1.5,
                 model="tts_models/multilingual/multi-dataset/xtts_v2",
                 progress_callback=None, timeout=14400):
        """返回 (True, {ok, dubbed, segments, ...}) 或 (False, error_msg)。"""
        self.task_id = task_id
        if not self.python_bin or not os.path.isfile(self.python_bin):
            return False, f"asr-venv python 未安装或路径无效: {self.python_bin}"
        if not self.runner_path or not os.path.isfile(self.runner_path):
            return False, f"dub runner 不存在: {self.runner_path}"
        if not translated_srt_path or not os.path.exists(translated_srt_path):
            return False, f"字幕文件不存在: {translated_srt_path}"
        if not no_vocals_wav or not os.path.exists(no_vocals_wav):
            return False, f"背景音文件不存在: {no_vocals_wav}"

        cmd = [self.python_bin, self.runner_path,
               "--translated-srt", translated_srt_path,
               "--no-vocals", no_vocals_wav,
               "--output", output_path,
               "--task-id", str(task_id),
               "--speaker", str(speaker or "Ana Florence"),
               "--language", str(language or "zh"),
               "--device", str(device or "cpu"),
               "--max-tempo", str(max_tempo or 1.5),
               "--model", str(model or "tts_models/multilingual/multi-dataset/xtts_v2")]
        self._log(f"调用 dub runner: {' '.join(cmd)}")
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
            return False, f"dub 超时({timeout}s)"
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
