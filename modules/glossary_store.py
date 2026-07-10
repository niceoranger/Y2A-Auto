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
        except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
            # 文件不存在/JSON 损坏/格式非法 → 退化为空表；权限、磁盘等真实故障向上抛出
            pass
        return cls({})
