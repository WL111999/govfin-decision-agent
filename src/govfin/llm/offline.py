"""离线确定性后端。

存在意义有两个，都不是"凑数"：
1. 让完整管道（UNK 捕获 → 聚类 → 约束验证 → 提案 → 仲裁 → 版本递增）在没有
   网络、没有 API Key 的环境下也能端到端跑通并被暴力测试覆盖。
2. 作为 fuzz / chaos 测试的故障注入基线——既要能"永远成功"，也要能按脚本
   "第 N 次调用失败"，用来验证重试与熔断逻辑。

它按 prompt 里出现的结构关键字返回**符合 schema 的** JSON，不做任何语义理解，
因此不能用于演示效果，只用于工程验证。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading

from govfin.errors import LLMError, LLMTimeout


class OfflineProvider:
    """规则驱动的确定性 provider。"""

    name = "offline"

    def __init__(self, *, fail_on_calls: set[int] | None = None, always_fail: str | None = None) -> None:
        self._call_index = 0
        self._lock = threading.Lock()
        self._fail_on_calls = fail_on_calls or set()
        self._always_fail = always_fail

    def chat(self, messages: list[dict[str, str]], *, temperature: float, timeout: float) -> str:
        with self._lock:
            self._call_index += 1
            index = self._call_index

        if self._always_fail == "timeout":
            raise LLMTimeout(f"离线后端注入超时（第 {index} 次调用）")
        if self._always_fail == "error":
            raise LLMError(f"离线后端注入错误（第 {index} 次调用）")
        if index in self._fail_on_calls:
            raise LLMError(f"离线后端按脚本注入失败（第 {index} 次调用）")

        prompt = messages[-1]["content"] if messages else ""
        return _synthesize(prompt)


def _synthesize(prompt: str) -> str:
    if "本体编辑提案" in prompt or "ontology_edit" in prompt:
        return _proposal(prompt)
    if "实体" in prompt and "关系" in prompt and "抽取" in prompt:
        return _extraction(prompt)
    if "候选概念" in prompt and "命名" in prompt:
        return _naming(prompt)
    if "一致性" in prompt and "裁决" in prompt:
        return _consistency(prompt)
    return json.dumps({"result": "offline", "digest": _digest(prompt)}, ensure_ascii=False)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _proposal(prompt: str) -> str:
    concept = _first_match(prompt, r"候选概念[:：]\s*(.+)") or f"auto_concept_{_digest(prompt)}"
    concept = concept.strip().splitlines()[0][:60]
    return json.dumps(
        {
            "action": "add_class",
            "class_name": concept,
            "parent_class": _first_match(prompt, r"父类[:：]\s*(\S+)") or "监管行为",
            "definition": f"由 UNK 储备池聚类提升的候选概念：{concept}",
            "properties": [
                {"name": "来源文档数", "datatype": "integer", "required": True},
                {"name": "首次观测时间", "datatype": "date", "required": False},
            ],
            "constraints": ["必须为监管行为的子类", "不得与信贷产品产生层级冲突"],
            "confidence": 0.72,
            "rationale": "离线后端基于出现频次生成的结构化提案，未经语义判断。",
        },
        ensure_ascii=False,
    )


def _extraction(prompt: str) -> str:
    """从 prompt 文本里抓出中文机构名/日期，产出符合五元组结构的抽取结果。"""
    text = prompt
    entities: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r"[一-龥]{2,12}(?:有限公司|股份有限公司|有限责任公司)", text):
        name = m.group(0)
        if name in seen:
            continue
        seen.add(name)
        entities.append({"text": name, "type": "企业", "start": m.start(), "end": m.end()})
    for m in re.finditer(r"(\d{4}-\d{2}-\d{2})", text):
        key = f"date:{m.group(1)}"
        if key in seen:
            continue
        seen.add(key)
        entities.append({"text": m.group(1), "type": "日期", "start": m.start(), "end": m.end()})

    relations = [
        {
            "head": entities[i]["text"],
            "tail": entities[i + 1]["text"],
            "type": "UNK-RELATION",
            "evidence": "离线后端基于邻近共现推断",
        }
        for i in range(len(entities) - 1)
    ]
    return json.dumps({"entities": entities, "relations": relations}, ensure_ascii=False)


def _naming(prompt: str) -> str:
    return json.dumps(
        {
            "canonical_name": _first_match(prompt, r"聚类[:：]\s*(.+)") or "未命名候选概念",
            "aliases": [],
            "note": "离线后端未做别名归一。",
        },
        ensure_ascii=False,
    )


def _consistency(prompt: str) -> str:
    return json.dumps(
        {"consistent": True, "violations": [], "note": "离线后端不做公理推理。"},
        ensure_ascii=False,
    )


def _first_match(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1) if m else None
