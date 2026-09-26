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
        # huggingface.co 直连在国内网络常被 SSL 重置;模型已缓存时优先离线加载,
        # 离线失败(缓存缺失)再联网重试并挂应用代理,避免 ASR 间歇性崩掉导致无字幕发布。
        offline_env = os.environ.copy()
        offline_env["HF_HUB_OFFLINE"] = "1"
        # onnxruntime(silero VAD)内置的微软遥测后台线程经本机代理拿坏响应时会
        # recursive_mutex 抛异常 abort(退出码 -6);关闭遥测即可根除
        offline_env["ORT_DISABLE_TELEMETRY"] = "1"
        self._log(f"调用 whisperx runner: {' '.join(cmd)} (HF_HUB_OFFLINE=1)")
        ok, result = self._run_runner(cmd, offline_env, timeout, progress_callback)
        if not ok and self._looks_like_offline_miss(result):
            online_env = self._build_online_env()
            if online_env is not None:
                self._log("离线加载失败(疑似缓存缺失),联网重试并使用代理下载模型")
                ok, result = self._run_runner(cmd, online_env, timeout, progress_callback)
        if not ok:
            return False, result if isinstance(result, str) else str(result)
        return True, result

    @staticmethod
    def _looks_like_offline_miss(error: object) -> bool:
        """判断 runner 失败是否疑似『离线模式缺缓存/网络被墙』,可联网重试。"""
        if not isinstance(error, str):
            return False
        text = error.lower()
        return any(kw in text for kw in (
            "offline", "cache", "huggingface", "ssl", "connection",
            "max retries", "localentrynotfound", "请求超时", "网络",
        ))

    @staticmethod
    def _build_online_env() -> dict:
        """构建联网重试环境:带应用代理,不设 HF_HUB_OFFLINE。"""
        env = os.environ.copy()
        env.pop("HF_HUB_OFFLINE", None)
        env["ORT_DISABLE_TELEMETRY"] = "1"
        try:
            from modules.config_manager import load_config
            from modules.youtube_handler import build_proxy_url
            proxy = build_proxy_url(load_config() or {})
            if proxy:
                env.setdefault("HTTPS_PROXY", proxy)
                env.setdefault("HTTP_PROXY", proxy)
        except Exception:
            pass
        return env

    def _run_runner(self, cmd, env, timeout, progress_callback):
        """执行 runner 子进程,逐行读进度,返回 (ok, result_dict_or_error)。"""
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1, env=env)
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
