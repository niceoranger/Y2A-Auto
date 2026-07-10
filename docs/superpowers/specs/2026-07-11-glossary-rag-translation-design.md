# 术语一致性 RAG 翻译 设计文档

> **状态**: 已批准，待实现
> **子项目归属**: AI 重制管线（第 2 块，翻译层增强）
> **前置依赖**: 无（独立增量，可单独启用）

## §1 范围

翻译前先用 LLM 扫全片原文抽取术语表（人名/专有名词/高频梗），逐批翻译时用字符串匹配命中本批涉及的术语，注入 system prompt，保证**全片术语译法统一**。

**不做**（YAGNI）：
- 不引入嵌入模型 / 向量库（用字符串匹配）
- 不做跨视频全局术语库（每任务独立）
- 不改 ASR / 配音 / 合成（那些是其他子项目）

## §2 目标与非目标

**目标**：解决现有翻译"每批 3 条独立翻译、跨批次零术语一致性"的缺陷——同一人名/术语全片统一译法。

**非目标**：整体翻译质量的其他方面（语气、流畅度）不在本子项目范围；仅聚焦术语一致性。

## §3 组件

| 文件 | 类型 | 职责 |
|------|------|------|
| `modules/glossary_extractor.py` | 新增 | `GlossaryExtractor`：全片原文喂 LLM，返回 `{source: target}` 术语表（含 note） |
| `modules/glossary_store.py` | 新增 | `GlossaryStore`：术语表持久化到 `downloads/<task_id>/glossary.json`；`match(texts)` 字符串匹配返回命中术语子集 |
| `modules/subtitle_translator.py` | 修改 | `translate_file` 开头（RAG 开启时）调抽取器建表；`translate_batch` 对每批原文匹配命中术语，注入 system prompt |
| `modules/prompt_manager.py` | 修改 | `get_subtitle_system_prompt` 接收可选术语参数，追加「术语对照表（必须遵守）」段 |
| `modules/config_manager.py` | 修改 | 新增 `GLOSSARY_RAG_ENABLED` / `GLOSSARY_MAX_TERMS` / `GLOSSARY_OPENAI_*` 键 |

## §4 数据流

```
translate_file(input, output, target_language) 开始
  │
  ├─ [GLOSSARY_RAG_ENABLED=False] → 现有行为，逐字节不变
  │
  └─ [GLOSSARY_RAG_ENABLED=True]
       ├─ 读全片 items（原文）
       ├─ GlossaryExtractor.extract(all_source_texts, target_language)
       │     → LLM 返回 {terms:[{source, target, note}]}
       │     → 截断到 GLOSSARY_MAX_TERMS
       │     → GlossaryStore.save(glossary.json)
       └─ 逐批翻译（现有并发调度不变）:
             batch_texts → GlossaryStore.match(batch_texts) → 命中术语子集
             → 注入 system prompt「术语对照表」
             → translate_batch（现有逻辑）
```

## §5 术语抽取（GlossaryExtractor）

- **输入**：全片原文列表（拼成带行号的文本块）+ 目标语言
- **调用**：整片一次 LLM 调用（字幕通常几百条，qwopus 上下文足够）
- **输出契约**：`response_format={"type":"json_object"}`，返回 `{"terms":[{"source":"...","target":"...","note":"..."}]}`
- **LLM 端点**：`GLOSSARY_OPENAI_*` 配置，留空回退主 `OPENAI_*`（即本地 qwopus）
- **截断**：命中超过 `GLOSSARY_MAX_TERMS`（默认 50）时，保留前 N 条
- **抽取失败**：记 warning，返回空表 → 退化为普通翻译（不中断）

## §6 术语匹配（GlossaryStore）

- **存储**：`downloads/<task_id>/glossary.json`，结构 `{"terms":[{"source","target","note"}]}`
- **match(texts: List[str]) → List[dict]**：
  - 对每条术语的 `source`，在本批 texts（拼接后）做**大小写无关子串匹配**
  - **按 source 长度降序**匹配（防止短术语是长术语子串时的误命中，如 "Tom" vs "Tom Hanks"）
  - 返回命中的术语子集（去重）
- **纯逻辑，无 IO 依赖**（save/load 分离，便于单测）

## §7 Prompt 注入

`get_subtitle_system_prompt` 增加可选参数 `glossary_terms: List[dict] = None`：
- 为空/None → prompt 与现在**完全相同**（不加术语段）
- 非空 → 在 system prompt 末尾追加：
  ```
  ## 术语对照表（必须严格遵守，保证全片统一）
  以下术语必须按此译法翻译：
  - {source} → {target}（{note}）
  ...
  ```

## §8 配置键

| 键 | 默认 | 说明 |
|----|------|------|
| `GLOSSARY_RAG_ENABLED` | `False` | RAG 术语总开关，默认关，行为与现在一致 |
| `GLOSSARY_MAX_TERMS` | `50` | 术语表上限，防 prompt 膨胀 |
| `GLOSSARY_OPENAI_BASE_URL` | `""` | 抽取用端点，留空回退主 `OPENAI_BASE_URL` |
| `GLOSSARY_OPENAI_API_KEY` | `""` | 留空回退主 `OPENAI_API_KEY` |
| `GLOSSARY_OPENAI_MODEL_NAME` | `""` | 留空回退主 `OPENAI_MODEL_NAME` |

## §9 错误处理

| 场景 | 处理 |
|------|------|
| 抽取 LLM 调用失败 | 记 warning，返回空表，退化为普通翻译，不中断 |
| 抽取返回非法 JSON | 同上，空表 |
| 术语表为空 | 逐批翻译不注入术语段（等价现有行为） |
| 某批 0 命中 | 该批 system prompt 不加术语段 |
| 术语超 MAX_TERMS | 截断保留前 N |

## §10 测试

| 测试 | 类型 | 内容 |
|------|------|------|
| `test_glossary_store.py` | 纯逻辑单测 | `match` 大小写无关、子串防误匹配（长度降序）、去重、空表、save/load 往返 |
| `test_glossary_extractor.py` | stub LLM 单测 | mock LLM 返回 JSON → 解析成术语表；失败→空表；超限→截断 |
| `test_glossary_translate_integration.py` | 集成 | RAG 开启时 system prompt 含术语段；关闭时 prompt 与现有逐字节一致 |

## §11 与现有翻译的关系

**完全增量、默认关闭**。`GLOSSARY_RAG_ENABLED=False` 时：
- `translate_file` 不调抽取器
- `translate_batch` 不做匹配
- `get_subtitle_system_prompt(glossary_terms=None)` 返回与现在完全相同的 prompt

开启才走"抽取 → 匹配 → 注入"路径。现有批量/并发/严格补救/一对齐逻辑**全部不动**。

## §12 默认决策（brainstorm §8）

1. **术语表每任务独立**——不跨视频复用（不同视频术语不同，跨片库是 YAGNI）
2. **抽取整片一次调用**——字幕通常几百条，qwopus 上下文够；真遇超长视频再加分块（本期不做）
