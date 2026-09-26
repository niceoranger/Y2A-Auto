"""bilibili 新时代上传器 v2 —— 按 2026-09-09 浏览器实测协议实现。

老班 bilibili-api 的 preupload 已废（406/244001），新流程：
  1. POST /upload/file           → S3 预签名 PUT URL 列表（fxmeta 元文件）
  2. POST /upload/multipart/new  → 分片上传会话（视频本体）
  3. PUT 内容到 upos/预签名 URL
  4. multipart complete + /x/vu/web/add/v3 提交

限流为账号级：连续尝试会重置静默窗，必须长静默后单发 + 慢节奏。
本模块直接以 curl_cffi(chrome 指纹)+完整 cookie 走新协议，绕过老库。
"""
import json as _json
import os
import time

from curl_cffi import requests as _creq

_COOKIES_FILE = '/Users/mac/Y2A-Auto/cookies/bili_cookies.json'
_RATE_LIMITED_CODES = (244001, 601)


class BiliUploadV2Error(Exception):
    pass


def _load_cookies():
    with open(_COOKIES_FILE, encoding='utf-8') as f:
        return {c['name']: c['value'] for c in _json.load(f)}


def _session():
    s = _creq.Session(impersonate='chrome')
    s.headers['Referer'] = 'https://member.bilibili.com/platform/upload/video/'
    s.cookies.update(_load_cookies())
    return s


def _fetch(sess, method, url, **kw):
    kw.setdefault('timeout', 120)
    r = sess.request(method, url, **kw)
    if r.status_code >= 400:
        raise BiliUploadV2Error(f'HTTP {r.status_code} {url[:80]}')
    return r.json()


def init_upload(sess, filename, filesize, meta_upos_uri):
    """/upload/multipart/new 建分片会话，返回会话 params"""
    body = {
        'profile': 'ugcfx/bup',
        'init_params': {'meta_upos_uri': meta_upos_uri},
        'name': filename,
        'size': filesize,
    }
    d = _fetch(sess, 'POST', 'https://member.bilibili.com/upload/multipart/new',
               json=body)['data']
    if isinstance(d, dict) and d.get('code') in _RATE_LIMITED_CODES:
        raise BiliUploadV2Error(f'限流: {d.get("code")} {d.get("message")}')
    return d


def meta_upload(sess, filename):
    """/upload/file 拿元文件预签名 URL；返回 (uri, presigned_urls)"""
    d = _fetch(sess, 'POST', 'https://member.bilibili.com/upload/file',
               json={'profile': 'fxmeta/bup', 'name': 'file_meta.txt'})['data']
    uri = d.get('uri', '')
    urls = [r['url'] for r in d.get('reqs', []) if r.get('method') == 'PUT']
    if not urls:
        raise BiliUploadV2Error('未拿到预签名 URL')
    return uri, urls


def rate_limited_now():
    """轻量探测当前是否还在限流窗：multipart/new 的最小 POST。"""
    try:
        sess = _session()
        init_upload(sess, 'probe.bin', 1, 'upos://fxmetalf/probe.txt')
        return False
    except BiliUploadV2Error as e:
        return '限流' in str(e)
    finally:
        try:
            sess.close()
        except Exception:
            pass