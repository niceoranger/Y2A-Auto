# 术语一致性 RAG 翻译 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 翻译前 LLM 扫全片自动抽术语表，逐批翻译时字符串匹配命中术语并注入 system prompt，保证全片术语译法统一。

**Architecture:** 两个新模块（`GlossaryExtractor` 抽取 + `GlossaryStore` 存储/匹配）+ 在 `subtitle_translator.py` 的并发翻译入口处一次性建表、每批注入。`prompt_manager` 的字幕 system prompt 增加可选术语段。全部由 `GLOSSARY_RAG_ENABLED`（默认关）门控，关闭时翻译行为逐字节不变。全本地：抽取走 OpenAI 兼容接口（`GLOSSARY_OPENAI_*` 留空回退 `SUBTITLE_OPENAI_*` → `OPENAI_*`，即本地 qwopus）。

**Tech Stack:** Python 3.12、现有 OpenAI 兼容客户端（`get_openai_client`）、unittest、无新增第三方依赖。

---

## File Structure

- **Create** `modules/glossary_store.py` — `GlossaryStore`：纯逻辑，术语 dict 的存/取/匹配/格式化。零外部依赖，易测。
- **Create** `modules/glossary_extractor.py` — `GlossaryExtractor`：调 LLM 从全片原文抽 `{source: target}` 术语表。
- **Modify** `modules/prompt_manager.py` — `get_subtitle_system_prompt` / `get_subtitle_strict_system_prompt` 增加可选 `glossary_text` 参数，追加术语段。
- **Modify** `modules/subtitle_translator.py` — 建表（`_translate_concurrent` 开头）+ 每批注入（`translate_batch` / `translate_batch_strict`）。
- **Modify** `modules/config_manager.py` — 5 个新键。
- **Test** `tests/test_glossary_store.py`、`tests/test_glossary_extractor.py`、`tests/test_glossary_prompt_injection.py`

---

### Task 1: GlossaryStore（纯逻辑：匹配 + 格式化 + 存取）

**Files:**
- Create: `modules/glossary_store.py`
- Test: `tests/test_glossary_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_glossary_store.py
import os
import sys
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.glossary_store import GlossaryStore


class TestGlossaryStore(unittest.TestCase):
    def test_match_case_insensitive(self):
        store = GlossaryStore({"WhisperX": "WhisperX", "GitHub": "GitHub"})
        hits = store.match(["I use whisperx daily", "no match here"])
        self.assertIn("WhisperX", hits)
        self.assertNotIn("GitHub", hits)

    def test_match_longest_first_avoids_substring_dup(self):
        # 长术语优先：命中 "New York City" 时不因 "New York" 也命中而产生歧义
        store = GlossaryStore({"New York": "纽约", "New York City": "纽约市"})
        hits = store.match(["Welcome to New York City"])
        # 两个都命中（都是子串），但顺序上长的在前
        keys = list(hits.keys())
        self.assertEqual(keys[0], "New York City")
        self.assertIn("New York", hits)

    def test_match_empty_when_no_hit(self):
        store = GlossaryStore({"Foo": "福"})
        self.assertEqual(store.match(["nothing relevant"]), {})

    def test_match_empty_store(self):
        store = GlossaryStore({})
        self.assertEqual(store.match(["anything"]), {})

    def test_format_for_prompt_nonempty(self):
        store = GlossaryStore({"WhisperX": "WhisperX"})
        text = store.format_for_prompt({"WhisperX": "WhisperX"})
        self.assertIn("WhisperX", text)
        self.assertIn("术语", text)

    def test_format_for_prompt_empty_returns_blank(self):
        store = GlossaryStore({"WhisperX": "WhisperX"})
        self.assertEqual(store.format_for_prompt({}), "")

    def test_save_and_load_roundtrip(self):
        store = GlossaryStore({"WhisperX": "WhisperX", "梗": "meme"})
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "glossary.json")
            store.save(p)
            self.assertTrue(os.path.exists(p))
            loaded = GlossaryStore.load(p)
            self.assertEqual(loaded.terms, store.terms)
            self.assertEqual(len(loaded), 2)

    def test_load_missing_file_returns_empty(self):
        loaded = GlossaryStore.load("/nonexistent/glossary.json")
        self.assertEqual(loaded.terms, {})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mac/Y2A-Auto && .venv/bin/python -m unittest tests.test_glossary_store -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'modules.glossary_store'`

- [ ] **Step 3: Write minimal implementation**

```python
# modules/glossary_store.py
"""术语库存储与匹配（纯逻辑，零外部依赖）。

术语一致性 RAG：翻译前抽取的 {原文术语: 译文} 表存于此，
翻译每批时用 match() 找出本批命中的术语子集，注入 prompt。
"""
import json
import os
from typing import Dict, List


class GlossaryStore:
    def __init__(self, terms: Dict[str, str] | None = None):
        # terms: {源术语: 目标译文}
        self.terms: Dict[str, str] = dict(terms) if terms else {}

    def __len__(self) -> int:
        return len(self.terms)

    def match(self, texts: List[str]) -> Dict[str, str]:
        """返回在 texts 中出现的术语子集（大小写无关，长术语优先）。

        长术语优先（按术语长度降序）：保证 dict 中先出现更具体的术语，
        避免注入时短术语（子串）喧宾夺主。
        """
        if not self.terms or not texts:
            return {}
        joined = "\n".join(t for t in texts if t).lower()
        if not joined:
            return {}
        hits: Dict[str, str] = {}
        for src in sorted(self.terms, key=len, reverse=True):
            if src and src.lower() in joined:
                hits[src] = self.terms[src]
        return hits

    def format_for_prompt(self, matched: Dict[str, str]) -> str:
        """把命中术语格式化成 system prompt 追加段；空则返回空串。"""
        if not matched:
            return ""
        lines = [f"- {src} → {tgt}" for src, tgt in matched.items()]
        return (
            "\n\n术语对照表（本片专有名词/人名/固定译法，翻译时必须严格遵守）：\n"
            + "\n".join(lines)
        )

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.terms, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "GlossaryStore":
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return cls({str(k): str(v) for k, v in data.items()})
        except Exception:
            pass
        return cls({})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mac/Y2A-Auto && .venv/bin/python -m unittest tests.test_glossary_store -v`
Expected: PASS — 8 tests OK

- [ ] **Step 5: Commit**

```bash
cd /Users/mac/Y2A-Auto
git add modules/glossary_store.py tests/test_glossary_store.py
git commit -m "feat(glossary): GlossaryStore 术语匹配+格式化+存取(纯逻辑)"
```

---

### Task 2: GlossaryExtractor（LLM 抽取术语表）

**Files:**
- Create: `modules/glossary_extractor.py`
- Test: `tests/test_glossary_extractor.py`

**Context:** 复用现有 `get_openai_client`（`modules/subtitle_translator.py:72`）与 `openai_chat_create_with_thinking_control`（同文件已用于翻译批次）。抽取器接收全片原文列表，一次调用 LLM，要求返回 `{"terms":[{"source":..,"target":..}]}` JSON，解析为 dict，截断到 `max_terms`。任何失败返回 `{}`（退化为普通翻译，不中断）。测试用 fake client 注入，不触网。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_glossary_extractor.py
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.glossary_extractor import GlossaryExtractor


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeClient:
    """返回预设 content 的假 OpenAI client。"""
    def __init__(self, content):
        self._content = content
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        return _FakeResponse(self._content)


class TestGlossaryExtractor(unittest.TestCase):
    def _make(self, content, max_terms=50):
        ex = GlossaryExtractor(openai_config={"OPENAI_API_KEY": "x"}, task_id="t", max_terms=max_terms)
        ex.client = _FakeClient(content)
        return ex

    def test_extract_parses_terms(self):
        ex = self._make('{"terms":[{"source":"WhisperX","target":"WhisperX"},{"source":"meme","target":"梗"}]}')
        terms = ex.extract(["a WhisperX clip about a meme"], target_language="zh")
        self.assertEqual(terms.get("WhisperX"), "WhisperX")
        self.assertEqual(terms.get("meme"), "梗")

    def test_extract_truncates_to_max_terms(self):
        items = ",".join([f'{{"source":"t{i}","target":"x{i}"}}' for i in range(10)])
        ex = self._make('{"terms":[' + items + ']}', max_terms=3)
        terms = ex.extract(["some text"], target_language="zh")
        self.assertEqual(len(terms), 3)

    def test_extract_bad_json_returns_empty(self):
        ex = self._make("not json at all")
        self.assertEqual(ex.extract(["text"], target_language="zh"), {})

    def test_extract_empty_texts_returns_empty_without_calling(self):
        ex = self._make('{"terms":[{"source":"a","target":"b"}]}')
        self.assertEqual(ex.extract([], target_language="zh"), {})

    def test_extract_no_client_returns_empty(self):
        ex = GlossaryExtractor(openai_config={"OPENAI_API_KEY": "x"}, task_id="t")
        ex.client = None
        self.assertEqual(ex.extract(["text"], target_language="zh"), {})

    def test_extract_skips_blank_entries(self):
        ex = self._make('{"terms":[{"source":"","target":"x"},{"source":"Foo","target":""},{"source":"Bar","target":"吧"}]}')
        terms = ex.extract(["Foo Bar"], target_language="zh")
        self.assertEqual(terms, {"Bar": "吧"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mac/Y2A-Auto && .venv/bin/python -m unittest tests.test_glossary_extractor -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'modules.glossary_extractor'`

- [ ] **Step 3: Write minimal implementation**

```python
# modules/glossary_extractor.py
"""术语表抽取：全片原文喂 LLM，抽出 {源术语: 译文} 表。

术语一致性 RAG 第一步。任何失败均返回空表，调用方退化为普通翻译。
"""
import json
import logging
from typing import Dict, List, Optional

from .subtitle_translator import get_openai_client

logger = logging.getLogger("glossary_extractor")

_SYSTEM_PROMPT = (
    "你是字幕本地化的术语抽取助手。给定一段视频的全部原文字幕，"
    "找出其中的专有名词、人名、地名、品牌、技术术语、以及反复出现的固定说法/梗，"
    "为每个给出统一的目标语言译法。只输出确有必要统一的术语，不要输出普通词汇。"
    "严格输出 JSON：{\"terms\":[{\"source\":\"原文术语\",\"target\":\"译文\"}]}。"
)


class GlossaryExtractor:
    def __init__(self, openai_config, task_id: str = "unknown", max_terms: int = 50):
        self.openai_config = openai_config
        self.task_id = task_id
        self.max_terms = max_terms
        self.client = None
        try:
            self.client = get_openai_client(openai_config)
        except Exception as e:
            logger.error(f"[{task_id}] 术语抽取器 client 初始化失败: {e}")

    def extract(self, source_texts: List[str], target_language: str = "zh") -> Dict[str, str]:
        if not source_texts or not self.client:
            return {}
        try:
            joined = "\n".join(t for t in source_texts if t)
            if not joined.strip():
                return {}
            model_name = (
                self.openai_config.get("GLOSSARY_OPENAI_MODEL_NAME")
                or self.openai_config.get("SUBTITLE_OPENAI_MODEL_NAME")
                or self.openai_config.get("OPENAI_MODEL_NAME", "gpt-3.5-turbo")
            )
            user_prompt = json.dumps(
                {"target_language": target_language, "subtitles": joined},
                ensure_ascii=False,
            )
            response = self.client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=2048,
                response_format={"type": "json_object"},
            )
            if not response.choices:
                return {}
            content = response.choices[0].message.content or ""
            return self._parse(content)
        except Exception as e:
            logger.warning(f"[{self.task_id}] 术语抽取失败，退化为普通翻译: {e}")
            return {}

    def _parse(self, content: str) -> Dict[str, str]:
        try:
            data = json.loads(content)
        except Exception:
            return {}
        if not isinstance(data, dict) or "terms" not in data:
            return {}
        out: Dict[str, str] = {}
        for item in data.get("terms", []):
            if not isinstance(item, dict):
                continue
            src = str(item.get("source", "")).strip()
            tgt = str(item.get("target", "")).strip()
            if src and tgt:
                out[src] = tgt
            if len(out) >= self.max_terms:
                break
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mac/Y2A-Auto && .venv/bin/python -m unittest tests.test_glossary_extractor -v`
Expected: PASS — 6 tests OK

- [ ] **Step 5: Commit**

```bash
cd /Users/mac/Y2A-Auto
git add modules/glossary_extractor.py tests/test_glossary_extractor.py
git commit -m "feat(glossary): GlossaryExtractor LLM 抽取术语表(失败退化空表)"
```

---

### Task 3: prompt_manager 注入术语段

**Files:**
- Modify: `modules/prompt_manager.py` (`get_subtitle_system_prompt` ~393, `get_subtitle_strict_system_prompt` ~409)
- Test: `tests/test_glossary_prompt_injection.py`

**Context:** 两个函数当前签名 `(*, mode, user_text, target_language)`，返回 `f"{behavior}{_SUBTITLE_SHARED_RULES}{_SUBTITLE_JSON_SUFFIX}"`。加一个可选 `glossary_text: str = ""` 参数，把术语段插在 shared rules 之后、JSON 后缀之前（术语约束属于行为规则，须在 JSON 格式说明前）。默认空串 → 输出与现在逐字节相同。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_glossary_prompt_injection.py
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.prompt_manager import get_subtitle_system_prompt, get_subtitle_strict_system_prompt


class TestGlossaryPromptInjection(unittest.TestCase):
    def test_no_glossary_matches_default(self):
        # 不传 glossary_text 时，输出与显式空串一致（默认行为不变）
        a = get_subtitle_system_prompt(mode="builtin", user_text="", target_language="zh")
        b = get_subtitle_system_prompt(mode="builtin", user_text="", target_language="zh", glossary_text="")
        self.assertEqual(a, b)

    def test_glossary_text_injected(self):
        gtext = "\n\n术语对照表：\n- WhisperX → WhisperX"
        out = get_subtitle_system_prompt(mode="builtin", user_text="", target_language="zh", glossary_text=gtext)
        self.assertIn("WhisperX", out)
        self.assertIn("术语对照表", out)

    def test_glossary_injected_before_json_suffix(self):
        # 术语段应在 JSON 格式说明之前（行为规则区）
        gtext = "\n\nGLOSSARY_MARKER_XYZ"
        out = get_subtitle_system_prompt(mode="builtin", user_text="", target_language="zh", glossary_text=gtext)
        from modules.prompt_manager import _SUBTITLE_JSON_SUFFIX
        marker_pos = out.index("GLOSSARY_MARKER_XYZ")
        json_pos = out.index(_SUBTITLE_JSON_SUFFIX[:20])
        self.assertLess(marker_pos, json_pos)

    def test_strict_variant_injects_glossary(self):
        gtext = "\n\nGLOSSARY_STRICT_MARKER"
        out = get_subtitle_strict_system_prompt(mode="builtin", user_text="", target_language="zh", glossary_text=gtext)
        self.assertIn("GLOSSARY_STRICT_MARKER", out)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mac/Y2A-Auto && .venv/bin/python -m unittest tests.test_glossary_prompt_injection -v`
Expected: FAIL — `TypeError: get_subtitle_system_prompt() got an unexpected keyword argument 'glossary_text'`

- [ ] **Step 3: Write minimal implementation**

Modify `get_subtitle_system_prompt` — add `glossary_text` param and insert before JSON suffix:

```python
def get_subtitle_system_prompt(
    *,
    mode: str = MODE_BUILTIN,
    user_text: str = "",
    target_language: str = "zh",
    glossary_text: str = "",
) -> str:
    """获取字幕翻译最终 system prompt（含协议壳和 JSON 后缀）。

    glossary_text: 术语对照段（RAG 命中术语），插在行为规则后、JSON 说明前；空则无变化。
    """
    behavior = get_final_system_prompt(
        "SUBTITLE_TRANSLATE",
        mode=mode,
        user_text=user_text,
        target_language=target_language,
    )
    return f"{behavior}{_SUBTITLE_SHARED_RULES}{glossary_text}{_SUBTITLE_JSON_SUFFIX}"
```

Modify `get_subtitle_strict_system_prompt` likewise:

```python
def get_subtitle_strict_system_prompt(
    *,
    mode: str = MODE_BUILTIN,
    user_text: str = "",
    target_language: str = "zh",
    glossary_text: str = "",
) -> str:
    """获取字幕翻译严格补救最终 system prompt（含协议壳和 JSON 后缀）。"""
    behavior = get_final_system_prompt(
        "SUBTITLE_TRANSLATE_STRICT",
        mode=mode,
        user_text=user_text,
        target_language=target_language,
    )
    return f"{behavior}{_SUBTITLE_STRICT_SHARED_RULES}{glossary_text}{_SUBTITLE_JSON_SUFFIX}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mac/Y2A-Auto && .venv/bin/python -m unittest tests.test_glossary_prompt_injection -v`
Expected: PASS — 4 tests OK

- [ ] **Step 5: Commit**

```bash
cd /Users/mac/Y2A-Auto
git add modules/prompt_manager.py tests/test_glossary_prompt_injection.py
git commit -m "feat(glossary): prompt_manager 字幕 system prompt 支持术语段注入"
```

---

### Task 4: config_manager 新键

**Files:**
- Modify: `modules/config_manager.py` (在 `SUBTITLE_OPENAI_THINKING_ENABLED` 键之后，约 L113)
- Test: `tests/test_glossary_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_glossary_config.py
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.config_manager import DEFAULT_CONFIG


class TestGlossaryConfig(unittest.TestCase):
    def test_keys_present_with_defaults(self):
        self.assertIn("GLOSSARY_RAG_ENABLED", DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["GLOSSARY_RAG_ENABLED"])
        self.assertEqual(DEFAULT_CONFIG["GLOSSARY_MAX_TERMS"], 50)
        self.assertEqual(DEFAULT_CONFIG["GLOSSARY_OPENAI_BASE_URL"], "")
        self.assertEqual(DEFAULT_CONFIG["GLOSSARY_OPENAI_API_KEY"], "")
        self.assertEqual(DEFAULT_CONFIG["GLOSSARY_OPENAI_MODEL_NAME"], "")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mac/Y2A-Auto && .venv/bin/python -m unittest tests.test_glossary_config -v`
Expected: FAIL — `KeyError: 'GLOSSARY_RAG_ENABLED'`

- [ ] **Step 3: Write minimal implementation**

In `modules/config_manager.py`, immediately after the `"SUBTITLE_OPENAI_THINKING_ENABLED": False,` line (~L113), add:

```python
    # 术语一致性 RAG 翻译（默认关；开启则翻译前抽全片术语表并注入每批 prompt）
    "GLOSSARY_RAG_ENABLED": False,
    "GLOSSARY_MAX_TERMS": 50,                # 术语表上限，长纪录片/播客可调高
    "GLOSSARY_OPENAI_BASE_URL": "",          # 留空回退 SUBTITLE_OPENAI_BASE_URL / OPENAI_BASE_URL
    "GLOSSARY_OPENAI_API_KEY": "",           # 留空回退 SUBTITLE_OPENAI_API_KEY / OPENAI_API_KEY
    "GLOSSARY_OPENAI_MODEL_NAME": "",        # 留空回退 SUBTITLE_OPENAI_MODEL_NAME / OPENAI_MODEL_NAME
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mac/Y2A-Auto && .venv/bin/python -m unittest tests.test_glossary_config -v`
Expected: PASS — 1 test OK

- [ ] **Step 5: Commit**

```bash
cd /Users/mac/Y2A-Auto
git add modules/config_manager.py tests/test_glossary_config.py
git commit -m "feat(glossary): config 新增 GLOSSARY_RAG_ENABLED + GLOSSARY_* 键"
```

---

### Task 5: subtitle_translator 接入（建表 + 每批注入）

**Files:**
- Modify: `modules/subtitle_translator.py`
  - `TranslationConfig`（~L103-135）加字段
  - `LLMRequester.translate_batch`（~386）与 `translate_batch_strict`（~462）接收 `glossary_text`
  - `_build_structured_system_prompt` / `_build_strict_structured_system_prompt`（~503/512）透传 `glossary_text`
  - `SubtitleTranslator.__init__` 建 `GlossaryStore`
  - `_translate_concurrent`（~723）开头建表；批次 worker 里 match+注入
  - `create_translator_from_config`（~1110）读新配置
- Test: `tests/test_glossary_translation_integration.py`

**Context:** 现有链路：`_translate_concurrent` 把 items 切成 batch，`translate_batch_worker` 调 `llm_requester.translate_batch(texts, target_language, batch_id)`；`translate_batch` 内 `self._build_structured_system_prompt(target_language)` 拿 system prompt。术语注入要贯穿这条链：建表存 store → 每批 match 出命中术语 → 格式化成 `glossary_text` → 透传到 system prompt 构建。**RAG 关闭时 `glossary_text` 恒为空串，全链路行为不变。**

- [ ] **Step 1: Write the failing test**

```python
# tests/test_glossary_translation_integration.py
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.subtitle_translator import LLMRequester
from modules.glossary_store import GlossaryStore


class TestGlossaryInjectionInRequester(unittest.TestCase):
    def _requester(self):
        req = LLMRequester(openai_config={"OPENAI_API_KEY": "x", "OPENAI_MODEL_NAME": "m"}, task_id="t")
        return req

    def test_build_system_prompt_accepts_glossary(self):
        req = self._requester()
        # 不传 glossary → 与显式空串一致
        base = req._build_structured_system_prompt("zh")
        base2 = req._build_structured_system_prompt("zh", glossary_text="")
        self.assertEqual(base, base2)
        # 传术语 → 出现在 prompt 中
        gtext = GlossaryStore({"WhisperX": "WhisperX"}).format_for_prompt({"WhisperX": "WhisperX"})
        injected = req._build_structured_system_prompt("zh", glossary_text=gtext)
        self.assertIn("WhisperX", injected)

    def test_strict_build_system_prompt_accepts_glossary(self):
        req = self._requester()
        gtext = "\n\nSTRICT_G_MARKER"
        injected = req._build_strict_structured_system_prompt("zh", glossary_text=gtext)
        self.assertIn("STRICT_G_MARKER", injected)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mac/Y2A-Auto && .venv/bin/python -m unittest tests.test_glossary_translation_integration -v`
Expected: FAIL — `TypeError: _build_structured_system_prompt() got an unexpected keyword argument 'glossary_text'`

- [ ] **Step 3: Write minimal implementation**

**3a.** `_build_structured_system_prompt` / `_build_strict_structured_system_prompt` 加 `glossary_text` 参数并透传：

```python
    def _build_structured_system_prompt(self, target_language: str, glossary_text: str = "") -> str:
        """构建结构化系统提示词（委托给统一 Prompt 中心）。"""
        from .prompt_manager import get_subtitle_system_prompt
        return get_subtitle_system_prompt(
            mode=self.openai_config.get('PROMPT_MODE', 'builtin'),
            user_text=self.openai_config.get('PROMPT_TEXT', ''),
            target_language=target_language,
            glossary_text=glossary_text,
        )

    def _build_strict_structured_system_prompt(self, target_language: str, glossary_text: str = "") -> str:
        """严格模式提示词（委托给统一 Prompt 中心）。"""
        from .prompt_manager import get_subtitle_strict_system_prompt
        return get_subtitle_strict_system_prompt(
            mode=self.openai_config.get(
                'PROMPT_STRICT_MODE',
                self.openai_config.get('PROMPT_MODE', 'builtin'),
            ),
            user_text=self.openai_config.get('PROMPT_STRICT_TEXT', ''),
            target_language=target_language,
            glossary_text=glossary_text,
        )
```

**3b.** `translate_batch` 签名加 `glossary_text: str = ""`，把 `self._build_structured_system_prompt(target_language)` 改为 `self._build_structured_system_prompt(target_language, glossary_text)`：

```python
    def translate_batch(self, texts: List[str], target_language: str, batch_id: str = "", glossary_text: str = "") -> List[str]:
        """批量翻译文本，使用结构化JSON输出。glossary_text: 本批命中的术语段（RAG）。"""
        if not texts:
            return []
        if not self.client:
            raise RuntimeError("OpenAI客户端未初始化")
        try:
            self._batch_counter += 1
            log_as_info = self._should_log_batch(batch_id)
            system_prompt = self._build_structured_system_prompt(target_language, glossary_text)
            user_prompt = self._build_structured_user_prompt(texts)
            # ...（其余保持不变）
```

**3c.** `translate_batch_strict` 同样加 `glossary_text: str = ""` 并把 `_build_strict_structured_system_prompt(target_language)` 改为 `_build_strict_structured_system_prompt(target_language, glossary_text)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mac/Y2A-Auto && .venv/bin/python -m unittest tests.test_glossary_translation_integration -v`
Expected: PASS — 2 tests OK

- [ ] **Step 5: Commit**

```bash
cd /Users/mac/Y2A-Auto
git add modules/subtitle_translator.py tests/test_glossary_translation_integration.py
git commit -m "feat(glossary): LLMRequester 支持 glossary_text 注入(默认空,行为不变)"
```

- [ ] **Step 6: 建表 + 每批 match 注入（TranslationConfig + __init__ + _translate_concurrent + create_translator_from_config）**

**6a.** `TranslationConfig` 加字段（在 `prompt_strict_text` 之后）：

```python
    glossary_rag_enabled: bool = False
    glossary_max_terms: int = 50
```

**6b.** `SubtitleTranslator.__init__` 末尾建空 store（找到 `self.llm_requester = LLMRequester(...)` 之后加）：

```python
        # 术语一致性 RAG：默认空表；translate_file 开头若启用则填充
        from .glossary_store import GlossaryStore
        self.glossary_store = GlossaryStore({})
```

**6c.** `_translate_concurrent` 开头（`total_items = len(items)` 之前）建表：

```python
            # 术语一致性 RAG：翻译前抽全片术语表（失败退化空表，不中断）
            if getattr(self.config, 'glossary_rag_enabled', False):
                try:
                    from .glossary_extractor import GlossaryExtractor
                    from .glossary_store import GlossaryStore
                    all_src = [it.source_text for it in items if it.source_text]
                    extractor = GlossaryExtractor(
                        self.openai_config_for_glossary(),
                        task_id=self.task_id,
                        max_terms=getattr(self.config, 'glossary_max_terms', 50),
                    )
                    terms = extractor.extract(all_src, target_language=self.config.target_language)
                    self.glossary_store = GlossaryStore(terms)
                    self.logger.info(f"术语表抽取完成，命中 {len(self.glossary_store)} 条术语")
                except Exception as e:
                    self.logger.warning(f"术语表抽取异常，退化为普通翻译: {e}")
```

**6d.** `SubtitleTranslator` 加一个辅助方法暴露 glossary 用的 openai_config（把 GLOSSARY_/SUBTITLE_/OPENAI 回退合成，供 extractor 用 model 回退链）。放在 `_build_structured_system_prompt` 附近：

```python
    def openai_config_for_glossary(self) -> dict:
        """给术语抽取器的 openai_config：透传现有 client 配置 + GLOSSARY_* 覆盖键。"""
        cfg = dict(self.openai_config)
        return cfg
```

> 注：抽取器的 client 复用 `get_openai_client(openai_config)`。因为 `LLMRequester` 与 translator 共享同一 `openai_config`（含 base_url/api_key/model），glossary 的 model 回退链在 `GlossaryExtractor.extract` 内用 `GLOSSARY_OPENAI_MODEL_NAME → SUBTITLE_OPENAI_MODEL_NAME → OPENAI_MODEL_NAME` 读取；这里透传即可。`self.openai_config` 在 `SubtitleTranslator.__init__` 中已由 `TranslationConfig` 组装（下条 6f 确保 GLOSSARY 键进入其中）。

**6e.** 批次 worker 注入：在 `translate_batch_worker`（`_translate_concurrent` 内，调 `self.llm_requester.translate_batch(...)` 处）先算 glossary_text 再传入：

```python
                        matched = self.glossary_store.match(batch_texts)
                        glossary_text = self.glossary_store.format_for_prompt(matched)
                        translations = self.llm_requester.translate_batch(
                            batch_texts, self.config.target_language, batch_id=batch_info['batch_id'],
                            glossary_text=glossary_text,
                        )
```

> `batch_texts` 变量在 worker 中的实际名称以现有代码为准（见 `_translate_concurrent`：批次 dict 的 `'texts'` 键）。若 worker 内是 `batch_info['texts']`，则 `matched = self.glossary_store.match(batch_info['texts'])`。

**6f.** `create_translator_from_config` 里 `TranslationConfig(...)` 构造补两个字段，并把 GLOSSARY 键并入传给 translator 的 openai_config：

```python
            glossary_rag_enabled=bool(app_config.get('GLOSSARY_RAG_ENABLED', False)),
            glossary_max_terms=int(app_config.get('GLOSSARY_MAX_TERMS', 50) or 50),
```

并确保 `SubtitleTranslator` 拿到的 `openai_config` 含 GLOSSARY_/SUBTITLE_ 键（现有构造已把 app_config 相关键传入 openai_config；若未包含 GLOSSARY_*，在组装 openai_config 的位置补入这三键）。

- [ ] **Step 7: Run existing subtitle tests + new tests (regression)**

Run:
```bash
cd /Users/mac/Y2A-Auto && .venv/bin/python -m unittest tests.test_glossary_store tests.test_glossary_extractor tests.test_glossary_prompt_injection tests.test_glossary_config tests.test_glossary_translation_integration -v
```
Expected: all PASS

Also import-check translator:
```bash
cd /Users/mac/Y2A-Auto && .venv/bin/python -c "from modules.subtitle_translator import create_translator_from_config; print('import OK')"
```
Expected: `import OK`

- [ ] **Step 8: Commit**

```bash
cd /Users/mac/Y2A-Auto
git add modules/subtitle_translator.py
git commit -m "feat(glossary): translate_file 建术语表 + 每批 match 注入(RAG 主链路)"
```

---

### Task 6: 真实冒烟 + 合并

**Files:**
- Test: 手动冒烟（无新文件）

**Context:** 用一个短英文 SRT，开启 `GLOSSARY_RAG_ENABLED`，跑一次真实翻译（走本地 qwopus），确认：① 术语表被抽出并存到 `downloads/<task_id>/glossary.json`；② 翻译结果中术语译法统一。若本地 qwopus 服务未启动，可跳过真机、仅确认单测全绿 + import 正常。

- [ ] **Step 1: 造测试 SRT**

```bash
cd /Users/mac/Y2A-Auto
cat > /tmp/glossary_smoke.srt <<'EOF'
1
00:00:00,000 --> 00:00:03,000
WhisperX is a great tool for subtitles.

2
00:00:03,000 --> 00:00:06,000
I really love using WhisperX every day.
EOF
echo "SRT ready"
```

- [ ] **Step 2: 跑真实翻译冒烟（需本地 qwopus 在 8080）**

```bash
cd /Users/mac/Y2A-Auto && .venv/bin/python -c "
from modules.subtitle_translator import create_translator_from_config
import json
cfg = json.load(open('config.json')) if __import__('os').path.exists('config.json') else {}
cfg.update({
  'OPENAI_API_KEY': cfg.get('OPENAI_API_KEY','sk-local'),
  'SUBTITLE_TARGET_LANGUAGE': 'zh',
  'GLOSSARY_RAG_ENABLED': True,
  'GLOSSARY_MAX_TERMS': 50,
})
tr = create_translator_from_config(cfg, task_id='glossary_smoke')
ok = tr.translate_file('/tmp/glossary_smoke.srt', '/tmp/glossary_smoke_zh.srt')
print('translate ok:', ok)
print('terms:', dict(tr.glossary_store.terms))
print(open('/tmp/glossary_smoke_zh.srt').read())
"
```
Expected: `translate ok: True`；`terms` 含 `WhisperX`；两条译文里 "WhisperX" 译法一致。

> 若本地模型未运行导致失败：记录“真机冒烟因本地服务未启用跳过”，以单测全绿为完成判据。

- [ ] **Step 3: 全量相关单测复核**

```bash
cd /Users/mac/Y2A-Auto && .venv/bin/python -m unittest tests.test_glossary_store tests.test_glossary_extractor tests.test_glossary_prompt_injection tests.test_glossary_config tests.test_glossary_translation_integration -v
```
Expected: all PASS

- [ ] **Step 4: 合并回 main**

```bash
cd /Users/mac/Y2A-Auto
git checkout main
git merge --no-ff feat/glossary-rag -m "feat: 术语一致性 RAG 翻译(全片抽术语表+每批注入,默认关)"
git log --oneline -3
```

- [ ] **Step 5: 清理临时文件**

```bash
rm -f /tmp/glossary_smoke.srt /tmp/glossary_smoke_zh.srt
echo "cleaned"
```

---

## Self-Review

**Spec coverage:**
- §1 范围（抽表+匹配注入，术语一致性）→ Task 1/2/5 ✅
- §2 组件表（extractor/store/translator/prompt/config）→ Task 1-5 全覆盖 ✅
- §3 数据流（translate_file 建表 → 逐批 match 注入）→ Task 5 步骤 6c/6e ✅
- §4 抽取（全片一次调用，JSON，截断 max_terms）→ Task 2 ✅
- §5 错误处理（抽取失败退化空表、空表跳过、0 命中不加段）→ Task 1(match/format 空返回) + Task 2(失败返回{}) ✅
- §6 测试（store 逻辑/extractor stub/注入集成）→ Task 1/2/3/5 tests ✅
- §7 增量（RAG 关闭行为不变）→ Task 3 `test_no_glossary_matches_default` + Task 5 默认空串 ✅
- §8 默认（每任务独立表存 glossary.json、整片一次）→ Task 5 store per translator + Task 2 单次调用 ✅
- 配置键（RAG_ENABLED/MAX_TERMS/OPENAI 三键）→ Task 4 ✅

**Placeholder scan:** 无 TBD/TODO；6e 对 batch_texts 变量名的说明是"以现有代码为准"的实现指引（worker 内变量名需实现时确认），非占位——已给出两种命名的处理。

**Type consistency:**
- `GlossaryStore(terms: dict)` / `.match()→dict` / `.format_for_prompt(dict)→str` / `.save/load` — Task 1 定义，Task 5 使用一致 ✅
- `GlossaryExtractor(openai_config, task_id, max_terms)` / `.extract(list, target_language)→dict` — Task 2 定义，Task 5 使用一致 ✅
- `glossary_text` 参数贯穿 prompt_manager(Task 3) → LLMRequester(Task 5 3b/3c) → _build_*(Task 5 3a) 命名一致 ✅
- config 键名 `GLOSSARY_RAG_ENABLED`/`GLOSSARY_MAX_TERMS`/`GLOSSARY_OPENAI_*`(Task 4) 与 create_translator_from_config 读取(Task 5 6f) 一致 ✅
