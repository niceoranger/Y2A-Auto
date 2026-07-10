# modules/glossary_extractor.py
"""术语表抽取：全片原文喂 LLM，抽出 {源术语: 译文} 表。

术语一致性 RAG 第一步。任何失败均返回空表，调用方退化为普通翻译。
"""
import json
import logging
from typing import Dict, List

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
        # 用 extract_json_from_text 剥离 markdown code fence / reasoning 包裹，
        # 因为本地模型(如 qwopus)常无视 response_format 仍把 JSON 裹进 ```json fence。
        from .utils import extract_json_from_text
        data = extract_json_from_text(content, expected_type=dict)
        if not isinstance(data, dict) or "terms" not in data:
            return {}
        terms = data.get("terms", [])
        if not isinstance(terms, list):
            return {}
        out: Dict[str, str] = {}
        for item in terms:
            if not isinstance(item, dict):
                continue
            src_raw = item.get("source")
            tgt_raw = item.get("target")
            if not isinstance(src_raw, str) or not isinstance(tgt_raw, str):
                continue
            src = src_raw.strip()
            tgt = tgt_raw.strip()
            if src and tgt:
                out[src] = tgt
            if len(out) >= self.max_terms:
                break
        return out
