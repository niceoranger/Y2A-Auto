"""bilibili 上传传输层补丁。

背景：member.bilibili.com 的 preupload 端点对 aiohttp 客户端指纹一律回
406 + code 601「您上传视频过快」（反爬伪装话术），真实浏览器 Chrome 可过。
排查矩阵（2026-09-09 实测）：
  aiohttp + ugcfx/bup          → 406/601
  aiohttp + ugcfx(短参数)      → 403(WAF)
  curl_cffi(chrome) + ugcfx/bup → 406/601
  curl_cffi(chrome) + ugcfx(浏览器参数形状) → 200 OK:1  ✅

方案：上传期间把 bilibili_api 的 session.request 换成 curl_cffi
（impersonate=chrome，过 WAF），并把 preupload 参数修正为浏览器形状
（profile=ugcfx，去 version/build/ssl，补 cdn=99）。上传结束恢复原样，
不影响应用内其他 bilibili_api 调用（登录/QR 等）。
"""
import asyncio
import os
import threading

_LOCK = threading.Lock()
_ACTIVE = 0
_ORIG = None  # (client, original_request)


def _load_bili_cookie_pairs():
    """读取扫码登录落盘的B站cookie(配置键 BILIBILI_COOKIES_PATH),返回 (name,value) 列表。

    机会性补全,文件缺失/格式异常时返回空列表(维持仅传库内cookie的旧行为);
    相对路径以仓库根目录(modules/..)解析,不依赖进程工作目录。
    """
    try:
        from .config_manager import load_config
        cookies_path = str(load_config().get('BILIBILI_COOKIES_PATH') or 'cookies/bili_cookies.json')
        if not os.path.isabs(cookies_path):
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            cookies_path = os.path.join(repo_root, cookies_path)
        import json as _json
        with open(cookies_path, encoding='utf-8') as f:
            data = _json.load(f)
        if isinstance(data, dict):
            data = data.get('cookies') or []
        return [(str(c.get('name')), str(c.get('value')))
                for c in data if isinstance(c, dict) and c.get('name')]
    except Exception:
        return []


class _Resp:
    """BiliAPIResponse 最小兼容面（video_uploader 用到 .code/.json()/.utf8_text/.headers）"""

    def __init__(self, r):
        self.code = r.status_code
        self.headers = dict(r.headers)
        try:
            self.cookies = {c.name: c.value for c in r.cookies.jar}
        except Exception:
            self.cookies = {}
        self.raw = r.content or b''
        self.url = str(r.url)
        self._r = r

    def json(self):
        return self._r.json()

    def utf8_text(self):
        return self._r.text


_UPLOAD_PAGE_REFERER = 'https://member.bilibili.com/platform-upload/video/'


def _is_preupload(url):
    return 'member.bilibili.com/preupload' in url


def _fix_preupload_params(url, params):
    # 2026-09-09 实测: 静默期满后, 原始 ugcfx/bup 参数 + Chrome TLS + 全量 cookie
    # 即可拿到完整认证会话(auth/biz_id/endpoint)。此前 406 全是账号限流窗所致,
    # 参数归一化(转 r=probe)反而会拿到无 auth 的探测响应, 已退役。
    return params


def _make_patched_request(orig_request):
    from curl_cffi import requests as creq

    async def patched_request(method='', url='', params={}, data={}, files={},
                              headers={}, cookies={}, allow_redirects=True, **kw):
        fixed_params = _fix_preupload_params(url, params or {})

        def do():
            merged_cookies = dict(cookies or {})
            # WAF 要求 preupload 带 SESSDATA 完整 cookie；库只传 buvid 子集，此处补全
            if 'bilibili.com' in url and 'SESSDATA' not in merged_cookies:
                for _name, _value in _load_bili_cookie_pairs():
                    merged_cookies.setdefault(_name, _value)
            eff_headers = dict(headers or {})
            # UA 必须与 impersonate 的 TLS 指纹配套，外部 UA(不一致的 Chrome 版本)会被 WAF 识破
            eff_headers.pop('User-Agent', None)
            eff_headers.pop('user-agent', None)
            kwargs = {
                'params': fixed_params,
                'headers': eff_headers,
                'allow_redirects': allow_redirects,
                'impersonate': 'chrome',
                'timeout': 900,
            }
            if isinstance(data, (str, bytes)) and data:
                kwargs['data'] = data
            elif isinstance(data, dict) and data:
                kwargs['data'] = data
            if files:
                conv = {}
                for k, f in files.items():
                    blob = getattr(f, 'read', None)
                    if blob is not None:
                        conv[k] = (getattr(f, 'name', 'blob'), blob())
                    elif isinstance(f, tuple):
                        conv[k] = f
                if conv:
                    kwargs['files'] = conv
            if merged_cookies:
                kwargs['cookies'] = merged_cookies
            return creq.request(method or 'GET', url, **kwargs)

        r = await asyncio.to_thread(do)
        return _Resp(r)

    return patched_request


class _TransportGuard:
    """上下文管理器：进入时装补丁，退出时还原（支持嵌套计数）"""

    def __enter__(self):
        install()
        return self

    def __exit__(self, *exc):
        uninstall()
        return False


def install():
    global _ACTIVE, _ORIG
    with _LOCK:
        if _ACTIVE > 0:
            _ACTIVE += 1
            return
        from bilibili_api.utils import network
        # session 按事件循环缓存，必须打在类上才能覆盖 asyncio.run 新建 loop 里的实例
        cls = network.sessions[network.selected_client]
        orig_request = cls.request

        async def patched_request(self, method='', url='', params={}, data={}, files={},
                                  headers={}, cookies={}, allow_redirects=True, **kw):
            inner = _make_patched_request(orig_request)
            return await inner(method=method, url=url, params=params, data=data, files=files,
                               headers=headers, cookies=cookies,
                               allow_redirects=allow_redirects, **kw)

        cls.request = patched_request
        _ORIG = (cls, orig_request)
        _ACTIVE = 1


def uninstall():
    global _ACTIVE, _ORIG
    with _LOCK:
        if _ACTIVE <= 0:
            return
        _ACTIVE -= 1
        if _ACTIVE == 0 and _ORIG is not None:
            cls, orig = _ORIG
            cls.request = orig
            _ORIG = None


def upload_transport():
    return _TransportGuard()
