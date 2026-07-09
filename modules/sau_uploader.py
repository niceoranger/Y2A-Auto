#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SauPlatformUploader —— 把 upload_video 调用翻译成 social-auto-upload CLI 子进程。

接口与 modules/acfun_uploader.AcfunUploader / bilibili_uploader.BilibiliUploader 同构,
便于 task_manager 统一调度。
"""
import json
import logging
import os
import subprocess


class SauPlatformUploader:
    def __init__(self, sau_bin: str):
        self.sau_bin = sau_bin
        self.logger = None
        self.task_id = None

    def _log(self, msg):
        if self.logger:
            self.logger.info(msg)
        else:
            logging.getLogger("sau_uploader").info(msg)

    def upload_video(self, *, video_file_path, cover_file_path, title, description,
                     tags, platform, account, youtube_url="", task_id=None,
                     progress_callback=None, timeout=1800):
        """返回 (True, {url, rawid, platform, ...}) 或 (False, error_msg)。"""
        self.task_id = task_id
        if not self.sau_bin or not os.path.isfile(self.sau_bin):
            return False, f"sau 未安装或路径无效: {self.sau_bin}"
        if not video_file_path or not os.path.exists(video_file_path):
            return False, f"视频文件不存在: {video_file_path}"
        if not cover_file_path or not os.path.exists(cover_file_path):
            return False, f"封面文件不存在: {cover_file_path}"
        if not account:
            return False, f"未配置 {platform} 的 sau 账号(--account)"
        if not title:
            return False, "标题为空,无法上传"

        cmd = [self.sau_bin, platform, "upload-video",
               "--account", str(account),
               "--file", video_file_path,
               "--title", str(title),
               "--desc", str(description or ""),
               "--thumbnail", cover_file_path]
        # sau 的 --tags 是单个逗号分隔字符串(不是多次 --tag)
        tag_str = ",".join(str(t).strip() for t in (tags or []) if str(t).strip())
        if tag_str:
            cmd.extend(["--tags", tag_str])

        self._log(f"调用 sau: {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=os.environ.copy(),
            )
        except FileNotFoundError as e:
            return False, f"启动 sau 失败: {e}"

        last_json_obj = None
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
                            last_json_obj = json.loads(stripped)
                        except ValueError:
                            pass
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return False, f"sau 上传超时({timeout}s)"
        except Exception as e:
            proc.kill()
            proc.wait()
            return False, f"sau 子进程异常: {e}"
        finally:
            if proc.stdout:
                proc.stdout.close()

        if proc.returncode != 0:
            summary = "\n".join(tail[-8:])
            return False, f"sau 退出码 {proc.returncode};输出末尾:\n{summary}"

        if isinstance(last_json_obj, dict):
            last_json_obj.setdefault("platform", platform)
            return True, last_json_obj
        # 无 JSON 结果:用末行做软证据
        return True, {"platform": platform, "url": "", "rawid": "", "tail": tail[-3:]}