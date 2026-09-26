"""频道定位门禁：只放行财经相关内容。

背景：监控源是 Fox Business 频道，但其内容混杂（9/11 纪念、纯政治、AI 新闻、
体育等），与「财经速递」频道定位不符。在监控筛选阶段用本地 Qwen 对标题做
财经相关性分类，不相关的不进流水线（不下载、不处理、不发布）。

判定策略：
  1) 关键词快速通道：标题含明确财经词 → 直接通过（省一次模型调用）
  2) Qwen 分类：模糊标题交给模型判断（finance_related: yes/no）
  3) 模型不可用时：保守放行（宁多发勿漏发，由人工审标题兜底）
"""
import json
import re
import threading
import urllib.request

_cache: dict = {}
_cache_lock = threading.Lock()
_QWEN_URL = 'http://127.0.0.1:8090/v1/chat/completions'
_MODEL = '/Users/mac/models/qwen3.8-27b-8bit'
_SYSTEM = (
    '你是财经科技频道的内容编辑。判断给定的英文视频标题是否与频道定位相关'
    '（宏观经济、股市、美联储、贸易、税收、企业盈利、大宗商品、房地产、'
    '就业、加密货币，以及人工智能、科技公司、芯片半导体等科技产业新闻）。'
    '纯政治评论、纪念日、体育、娱乐、天气等算不相关；'
    '政治人物谈经济政策算相关。'
    '只输出JSON: {"finance_related": true/false}'
)


def _qwen_classify(title: str) -> bool:
    body = json.dumps({
        'model': _MODEL,
        'messages': [{'role': 'system', 'content': _SYSTEM},
                     {'role': 'user', 'content': title}],
        'max_tokens': 500, 'enable_thinking': False,
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(_QWEN_URL, data=body,
                                 headers={'Content-Type': 'application/json'})
    r = json.load(urllib.request.urlopen(req, timeout=60))
    text = (r['choices'][0]['message'].get('content') or '').strip()
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        try:
            return bool(json.loads(m.group(0)).get('finance_related'))
        except Exception:
            pass
    return 'true' in text.lower()


def is_finance_related(title: str, logger=None) -> bool:
    """判断标题是否财经相关；带缓存与保守降级。"""
    title = (title or '').strip()
    if not title:
        return True
    low = title.lower()
    with _cache_lock:
        if low in _cache:
            return _cache[low]
    # 按需求取消关键词快速通道：所有标题一律 AI 分类（缓存除外）
    try:
        verdict = _qwen_classify(title)
    except Exception as e:
        if logger:
            logger.warning(f"财经门禁模型调用失败，保守放行: {e}")
        return True  # 降级放行
    with _cache_lock:
        _cache[low] = verdict
    return verdict
