#!/usr/bin/env python3
"""
批量删除 B 站创作中心「审核不通过」稿件。

背景:
- B 站删除稿件接口 /x/web/archive/delete 强制要求极验点选验证,
  无法纯脚本完成; 本工具把除验证码以外的全部流程自动化,
  用户只需在自动弹出的本地验证页里连续点击验证。

用法示例::

    # 先预览将要删除的稿件(不删)
    python tools/bili_clean_rejected.py --dry-run

    # 正式删除全部审核不通过稿件
    python tools/bili_clean_rejected.py

    # 只删前 5 条试试水
    python tools/bili_clean_rejected.py --limit 5

流程:
1. 用项目 cookies/bili_cookies.json 登录态拉取全部稿件;
2. 筛选 state == -2 (审核不通过) 并列出清单, 等待确认;
3. 启动本地验证页(默认 http://127.0.0.1:18765)并自动打开浏览器;
4. 逐条: 获取极验 challenge -> 用户在页面点选 -> 携带验证结果删除;
5. 输出结果清单 logs/bili_clean_<时间戳>.json。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.bilibili_auth import load_cookie_dict  # noqa: E402

MEMBER = "https://member.bilibili.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

STATE_REJECTED = -2

CAPTCHA_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>B站稿件删除验证</title>
<style>
body{font-family:-apple-system,sans-serif;background:#f4f5f7;display:flex;flex-direction:column;align-items:center;padding-top:24px;margin:0}
.box{background:#fff;border-radius:12px;padding:20px 28px;box-shadow:0 2px 12px rgba(0,0,0,.08);max-width:520px;text-align:center}
h3{margin:0 0 6px;font-size:17px}p{color:#666;font-size:13px;margin:4px 0}
.progress{font-size:15px;color:#00a1d6;font-weight:600;margin:10px 0 2px}
#captcha{min-height:190px;margin-top:10px}
.done{margin-top:40px;font-size:18px;color:#02b340;font-weight:700}
.err{color:#fb7299}
</style>
<script src="https://static.geetest.com/static/tools/gt.js"></script>
</head><body><div class="box">
<h3>请完成点选验证</h3>
<p>按提示依次点击指定文字/图案，完成后自动进入下一条</p>
<div class="progress" id="prog"></div>
<p id="title"></p>
<div id="captcha"></div>
</div>
<script>
let lastSeq = 0;
async function loadChallenge(){
  let cfg;
  for(;;){
    const r = await fetch('/init'); cfg = await r.json();
    if(cfg.done || cfg.seq > lastSeq) break;
    await new Promise(r => setTimeout(r, 400));   // 等待脚本挂出下一条新挑战
  }
  if(cfg.done){ document.querySelector('.box').innerHTML =
    '<div class="done">全部完成 ✓<br><small style="color:#999">可关闭本页，回到终端查看结果</small></div>'; return; }
  lastSeq = cfg.seq;
  document.getElementById('prog').textContent = '第 ' + cfg.index + ' / ' + cfg.total + ' 条';
  document.getElementById('title').textContent = cfg.title || '';
  document.getElementById('captcha').innerHTML = '';
  initGeetest({gt:cfg.gt, challenge:cfg.challenge, new_captcha:cfg.new_captcha,
               product:'popup', offline:!cfg.success}, obj=>{
    obj.appendTo('#captcha');
    obj.onSuccess(async ()=>{
      const v = obj.getValidate();
      if(!v){ loadChallenge(); return; }
      await fetch('/result', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(v)});
      setTimeout(loadChallenge, 600);
    });
    obj.onError(()=> setTimeout(loadChallenge, 1500));
  });
}
loadChallenge();
</script></body></html>"""


class CaptchaFlow:
    """本地验证页与主流程之间的握手: /init 提供当前 challenge, /result 收验证结果."""

    def __init__(self, port: int):
        self.port = port
        self.cfg: Dict = {"done": False}
        self.result: Optional[Dict] = None
        self._event = threading.Event()
        self._seq = 0
        flow = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(obj, ensure_ascii=False).encode())

            def do_GET(self):
                if urlparse(self.path).path == "/":
                    body = CAPTCHA_PAGE.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(body)
                elif urlparse(self.path).path == "/init":
                    self._json(flow.cfg)
                else:
                    self._json({"err": 404}, 404)

            def do_POST(self):
                if urlparse(self.path).path == "/result":
                    n = int(self.headers.get("Content-Length", 0))
                    flow.result = json.loads(self.rfile.read(n))
                    flow._event.set()
                    self._json({"ok": True})
                else:
                    self._json({"err": 404}, 404)

        self._server = HTTPServer(("127.0.0.1", port), Handler)

    def start(self):
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        self._server.shutdown()

    def serve(self, pre: Dict, index: int, total: int, title: str):
        """挂出一条新 challenge 供页面拉取(seq 递增, 页面据此识别新挑战)."""
        self.result = None
        self._event.clear()
        self._seq += 1
        self.cfg = {
            "gt": pre["gt"],
            "challenge": pre["challenge"],
            "success": pre.get("success"),
            "new_captcha": pre.get("new_captcha", 1),
            "index": index,
            "total": total,
            "title": title,
            "seq": self._seq,
            "done": False,
        }

    def finish(self):
        self.result = None
        self._event.clear()
        self.cfg = {"done": True}

    def wait_result(self, timeout: float) -> Optional[Dict]:
        self._event.wait(timeout)
        return self.result


def build_session(cookies: Dict[str, str]) -> requests.Session:
    s = requests.Session()
    s.cookies.update(cookies)
    s.headers.update({
        "User-Agent": UA,
        "Referer": MEMBER + "/platform/upload-manager/article",
        "Origin": MEMBER,
    })
    return s


def fetch_rejected(session: requests.Session, mid: str) -> List[Dict]:
    out: List[Dict] = []
    pn, ps, total = 1, 30, None
    while True:
        r = session.get(
            MEMBER + "/x/web/archives",
            params={"pn": pn, "ps": ps, "keyword": "", "mid": mid, "order": "mddate"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json().get("data") or {}
        arcs = data.get("arc_audits") or []
        if total is None:
            total = (data.get("page") or {}).get("count") or 0
            print(f"稿件总数: {total}")
        if not arcs:
            break
        for item in arcs:
            arc = item.get("Archive", {})
            if arc.get("state") == STATE_REJECTED:
                out.append({
                    "aid": arc.get("aid"),
                    "bvid": arc.get("bvid"),
                    "title": arc.get("title") or "",
                })
        if pn * ps >= total:
            break
        pn += 1
        time.sleep(0.35)
    return out


def geetest_pre(session: requests.Session) -> Dict:
    r = session.get(
        MEMBER + "/x/geetest/pre", params={"t": int(time.time() * 1000)}, timeout=15
    )
    r.raise_for_status()
    data = r.json().get("data") or {}
    if not data.get("gt") or not data.get("challenge"):
        raise RuntimeError(f"获取极验 challenge 失败: {data}")
    return data


def delete_archive(
    session: requests.Session, csrf: str, aid: int, pre: Dict, validate: Dict
) -> Dict:
    payload = {
        "aid": aid,
        "csrf": csrf,
        "geetest_challenge": validate.get("geetest_challenge", ""),
        "geetest_validate": validate.get("geetest_validate", ""),
        "geetest_seccode": validate.get("geetest_seccode", ""),
        "success": pre.get("success"),
    }
    r = session.post(MEMBER + "/x/web/archive/delete", data=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def main() -> int:
    ap = argparse.ArgumentParser(description="批量删除B站审核不通过稿件")
    ap.add_argument("--cookies", default=str(PROJECT_ROOT / "cookies" / "bili_cookies.json"))
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条(调试用), 0=全部")
    ap.add_argument("--port", type=int, default=18765, help="本地验证页端口")
    ap.add_argument("--dry-run", action="store_true", help="仅列出待删稿件, 不删除")
    ap.add_argument("--yes", action="store_true", help="跳过终端确认(已了解后果)")
    args = ap.parse_args()

    cookies = load_cookie_dict(args.cookies)
    csrf = cookies.get("bili_jct")
    mid = cookies.get("DedeUserID")
    if not csrf or not mid:
        print("cookie 缺少 bili_jct / DedeUserID, 无法操作")
        return 2

    session = build_session(cookies)
    print("正在拉取稿件列表 ...")
    rejected = fetch_rejected(session, mid)
    if args.limit > 0:
        rejected = rejected[: args.limit]

    if not rejected:
        print("没有找到审核不通过的稿件, 无需处理")
        return 0

    print(f"\n审核不通过稿件共 {len(rejected)} 条:")
    for i, v in enumerate(rejected[:10], 1):
        print(f"  {i:>3}. {v['title'][:38]}  ({v['bvid']})")
    if len(rejected) > 10:
        print(f"  ... 其余 {len(rejected) - 10} 条略")

    if args.dry_run:
        print("\n[dry-run] 以上稿件待删除, 未执行任何操作")
        return 0

    if not args.yes:
        answer = input(f"\n确认删除以上 {len(rejected)} 条稿件? 此操作不可恢复 [y/N] ").strip().lower()
        if answer != "y":
            print("已取消")
            return 0

    flow = CaptchaFlow(args.port)
    flow.start()
    url = f"http://127.0.0.1:{args.port}/"
    print(f"\n验证页面: {url} (即将自动打开; 若未打开请手动访问)")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    deleted: List[Dict] = []
    failed: List[Dict] = []
    try:
        for i, video in enumerate(rejected, 1):
            ok = False
            last_err = ""
            for attempt in range(3):
                pre = geetest_pre(session)
                flow.serve(pre, i, len(rejected), video["title"])
                print(f"[{i}/{len(rejected)}] 等待验证: {video['title'][:32]} ...", flush=True)
                validate = flow.wait_result(timeout=180)
                if not validate:
                    last_err = "等待验证超时(3分钟未完成点选)"
                    continue
                resp = delete_archive(session, csrf, video["aid"], pre, validate)
                if resp.get("code") == 0:
                    ok = True
                    break
                last_err = f"code={resp.get('code')} {resp.get('message')}"
                if resp.get("code") in (-101, -111):  # 登录态失效
                    print("登录态已失效, 请更新 cookies 后重跑(已删除的不会重复处理)")
                    break
                time.sleep(1.5)
            if ok:
                deleted.append(video)
                print(f"    已删除 {video['bvid']}")
            else:
                failed.append({**video, "error": last_err})
                print(f"    失败: {last_err}")
            time.sleep(random.uniform(1.2, 2.5))
    except KeyboardInterrupt:
        print("\n用户中断")
    finally:
        flow.finish()
        time.sleep(0.5)
        flow.stop()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = PROJECT_ROOT / "logs" / f"bili_clean_{stamp}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps({"deleted": deleted, "failed": failed}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    print(f"\n完成: 成功删除 {len(deleted)} 条, 失败 {len(failed)} 条")
    if failed:
        print("失败清单:")
        for v in failed:
            print(f"  {v['bvid']} {v['title'][:30]} -> {v['error']}")
    print(f"明细已保存: {out}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
