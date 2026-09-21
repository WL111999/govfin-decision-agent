"""本体编辑提案生成：把对齐结果翻译成可执行、可校验的编辑动作。

LLM 在这里的角色被刻意限制成"填语义细节"，而不是"决定要不要演化"。
要不要演化由符号对齐的结论定死（别名 or 新类 or 拒绝），LLM 只负责回答
符号层回答不了的问题：这个概念的定义该怎么写、该带哪些属性、有哪些显然
互斥的类。这样即使 LLM 出现幻觉，它能造成的最坏后果也只是"新类的定义写得
不好"，而不会凭空造出一个本该走别名学习的新类——那才是会毁掉图谱结构的一类错误。

LLM 不可用时（无 key、离线测试、限流熔断）退化成确定性启发式提案。这不是
"降级方案摆着好看"：演化管道必须能在没有 LLM 的环境里跑完一整轮，否则
CI 里根本测不了它。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from govfin.evolution.aligner import AlignmentResult
from govfin.evolution.unk_pool import KIND_RELATION
from govfin.llm.base import LLMClient
from govfin.ontology.model import Ontology

ALLOWED_ACTIONS = {
    "add_class",
    "add_property",
    "add_alias",
    "add_property_alias",
    "set_parent",
    "add_disjoint",
}

_HEURISTIC_CONFIDENCE = 0.45  # 无 LLM 时的提案置信度，低于自动采纳门槛

_PROPOSAL_SYSTEM = """你是领域本体的编辑提案生成器。你的输出会被程序直接执行，因此：

1. 只能使用给定动作白名单中的动作，只能引用给定的已有类名。
2. 不要新增类去表示一个已有类的同义说法——调用方已经做过符号对齐，你只需按它给的 decision 生成编辑。
3. 属性名用中文短语，不要用英文或驼峰。
4. 只输出 JSON，不要解释文字。

动作格式：
- {"action":"add_class","class_name":"...","parent_class":"...","definition":"...","aliases":["..."],"required_properties":["..."],"optional_properties":["..."]}
- {"action":"add_property","property_name":"...","domain":"...","range":"...","kind":"object","definition":"..."}
- {"action":"add_alias","class_name":"...","alias":"..."}
- {"action":"add_disjoint","class_a":"...","class_b":"..."}

输出格式：{"edits":[...],"definition_rationale":"一句话说明依据","confidence":0.0~1.0}
"""


@dataclass
class EditProposal:
    proposal_id: str
    cluster_id: str
    canonical: str
    kind: str
    decision: str  # alias | new_class | reject
    edits: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    rationale: str = ""
    origin: str = "heuristic"  # llm | heuristic | symbolic
    rejected_reason: str = ""
    evidence: dict = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        return self.decision != "reject" and bool(self.edits)

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "cluster_id": self.cluster_id,
            "canonical": self.canonical,
            "kind": self.kind,
            "decision": self.decision,
            "edits": self.edits,
            "confidence": round(self.confidence, 4),
            "rationale": self.rationale,
            "origin": self.origin,
            "rejected_reason": self.rejected_reason,
            "evidence": self.evidence,
        }


class EditProposer:
    def __init__(
        self,
        ontology: Ontology,
        client: LLMClient | None = None,
        *,
        use_llm: bool = True,
    ) -> None:
        self.ontology = ontology
        self.client = client
        self.use_llm = use_llm and client is not None

    # ------------------------------------------------------------------

    def propose(self, alignment: AlignmentResult, *, contexts: list[str] | None = None) -> EditProposal:
        base = EditProposal(
            proposal_id=f"prop:{alignment.cluster_id.split(':')[-1]}",
            cluster_id=alignment.cluster_id,
            canonical=alignment.canonical,
            kind=alignment.kind,
            decision=alignment.decision,
            confidence=alignment.confidence,
            evidence=alignment.evidence,
        )

        if alignment.decision == "reject":
            base.origin = "symbolic"
            base.rejected_reason = "；".join(alignment.reasons) or "符号对齐未通过"
            return base

        if alignment.decision == "alias":
            is_property = alignment.evidence.get("target_kind") == "object_property"
            action = "add_property_alias" if is_property else "add_alias"
            key = "property_name" if is_property else "class_name"
            base.edits = [
                {"action": action, key: alignment.target_class, "alias": alias}
                for alias in alignment.new_aliases
                if alias != alignment.target_class
            ]
            base.origin = "symbolic"
            base.rationale = "；".join(alignment.reasons)
            if not base.edits:
                base.decision = "reject"
                base.rejected_reason = "候选已可通过现有别名解析，无需任何编辑"
            return base

        # decision == new_class：实体走 add_class，关系走 add_property
        if alignment.kind == KIND_RELATION:
            return self._propose_property(base, alignment, contexts or [])
        return self._propose_class(base, alignment, contexts or [])

    # ------------------------------------------------------------------

    def _propose_property(
        self, base: EditProposal, alignment: AlignmentResult, contexts: list[str]
    ) -> EditProposal:
        if not alignment.domain_class or not alignment.range_class:
            base.decision = "reject"
            base.rejected_reason = "关系候选缺少确定的 domain/range"
            return base
        heuristic = {
            "action": "add_property",
            "property_name": alignment.canonical,
            "domain": alignment.domain_class,
            "range": alignment.range_class,
            "kind": "object",
            "definition": f"从文本中观测到的关系：{alignment.canonical}",
        }
        base.edits = [heuristic]
        base.rationale = "；".join(alignment.reasons)

        if not self.use_llm:
            # 关系的 domain/range 是从 40 字上下文窗口里推出来的，证据强度远低于
            # 名称层面的别名匹配。定错 domain/range 会让路径搜索**静默**走错路由，
            # 因此这里和实体类一样压低置信度，强制走人工仲裁。
            base.confidence = min(alignment.confidence, _HEURISTIC_CONFIDENCE)
            base.origin = "heuristic"
            return base

        payload = self._ask_llm_relation(alignment, contexts)
        if payload is None:
            base.confidence = min(alignment.confidence, _HEURISTIC_CONFIDENCE)
            base.origin = "heuristic"
            base.rationale += "（LLM 不可用，退回启发式提案）"
            return base
        edits, rationale, llm_conf = payload
        base.edits = edits
        base.rationale = rationale
        base.confidence = min(0.95, 0.6 * llm_conf + 0.4 * alignment.confidence)
        base.origin = "llm"
        return base

    def _ask_llm_relation(
        self, alignment: AlignmentResult, contexts: list[str]
    ) -> tuple[list[dict], str, float] | None:
        assert self.client is not None
        prompt = (
            f"候选关系：{alignment.canonical}\n"
            f"上下文启发式推断：domain=「{alignment.domain_class}」 range=「{alignment.range_class}」\n"
            f"推断依据：{'；'.join(alignment.reasons)}\n"
            f"原文上下文片段：\n"
            + "\n".join(f"- {c}" for c in contexts[:5] if c)
            + f"\n\n当前本体已有类（domain/range 只能取这些）：\n"
            + "、".join(
                sorted(
                    c.name
                    for c in self.ontology.classes.values()
                    if c.layer in (1, 2)
                )
            )
            + "\n请判断该关系是否真实存在、domain/range 是否正确，然后生成 add_property 编辑。"
            "如果上下文证据不足以确定方向，就在 confidence 里如实反映（低于 0.5）。\n"
        )
        try:
            data = self.client.complete_json(prompt, system=_PROPOSAL_SYSTEM)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(data, dict):
            return None
        edits = self._sanitize(data.get("edits"))
        edits = [e for e in edits if e.get("action") == "add_property"]
        if not edits:
            return None
        rationale = str(data.get("definition_rationale") or "；".join(alignment.reasons))[:500]
        try:
            conf = float(data.get("confidence", alignment.confidence))
        except (TypeError, ValueError):
            conf = alignment.confidence
        return edits, rationale, min(max(conf, 0.0), 1.0)

    def _propose_class(
        self, base: EditProposal, alignment: AlignmentResult, contexts: list[str]
    ) -> EditProposal:
        parent = alignment.parent_class
        if parent is None or parent not in self.ontology.classes:
            base.decision = "reject"
            base.rejected_reason = "没有可挂靠的父类"
            return base

        parent_cls = self.ontology.classes[parent]
        heuristic = {
            "action": "add_class",
            "class_name": alignment.canonical,
            "parent_class": parent,
            "definition": f"由 UNK 候选演化而来，挂靠于「{parent}」。"
            f"原始观测上下文：{'；'.join(contexts[:2]) if contexts else '（无）'}",
            "aliases": [],
            "required_properties": list(parent_cls.required_properties),
            "optional_properties": [],
            "layer": parent_cls.layer,
        }

        if not self.use_llm:
            base.edits = [heuristic]
            base.confidence = min(alignment.confidence, _HEURISTIC_CONFIDENCE)
            base.rationale = "；".join(alignment.reasons)
            base.origin = "heuristic"
            return base

        payload = self._ask_llm(alignment, contexts)
        if payload is None:
            base.edits = [heuristic]
            base.confidence = min(alignment.confidence, _HEURISTIC_CONFIDENCE)
            base.rationale = "；".join(alignment.reasons) + "（LLM 不可用，退回启发式提案）"
            base.origin = "heuristic"
            return base

        edits, rationale, llm_conf = payload
        base.edits = edits
        base.rationale = rationale
        base.confidence = min(0.97, 0.6 * llm_conf + 0.4 * alignment.confidence)
        base.origin = "llm"
        return base

    def _ask_llm(
        self, alignment: AlignmentResult, contexts: list[str]
    ) -> tuple[list[dict], str, float] | None:
        assert self.client is not None
        ontology_brief = self._ontology_brief(alignment.parent_class)
        prompt = (
            f"候选概念：{alignment.canonical}\n"
            f"类别：{'关系' if alignment.kind == KIND_RELATION else '实体'}\n"
            f"符号对齐结论：新增类，建议父类「{alignment.parent_class}」\n"
            f"对齐依据：{'；'.join(alignment.reasons)}\n"
            f"观测次数：{alignment.evidence.get('observations', '未知')}\n"
            f"原文上下文片段：\n"
            + "\n".join(f"- {c}" for c in contexts[:5] if c)
            + f"\n\n当前本体已有类（只能引用这些作为 parent_class / domain / range）：\n{ontology_brief}\n"
        )
        try:
            data = self.client.complete_json(prompt, system=_PROPOSAL_SYSTEM)
        except Exception:  # noqa: BLE001 - LLM 故障必须降级而不是中断整轮演化
            return None
        if not isinstance(data, dict):
            return None
        edits = self._sanitize(data.get("edits"))
        if not edits:
            return None
        rationale = str(data.get("definition_rationale") or "；".join(alignment.reasons))[:500]
        try:
            conf = float(data.get("confidence", alignment.confidence))
        except (TypeError, ValueError):
            conf = alignment.confidence
        return edits, rationale, min(max(conf, 0.0), 1.0)

    def _ontology_brief(self, focus: str) -> str:
        if focus and focus in self.ontology.classes:
            cls = self.ontology.classes[focus]
            chain = [" / ".join(reversed(self.ontology.ancestors(focus))) or "（根）", focus]
            siblings = [
                c.name for c in self.ontology.classes.values() if c.parent == focus
            ] or ["（无子类）"]
            return (
                f"父类路径：{' > '.join(chain)}\n"
                f"父类定义：{cls.definition}\n"
                f"父类必需属性：{cls.required_properties}\n"
                f"父类已有子类：{siblings}"
            )
        return "、".join(sorted(self.ontology.classes)[:60])

    def _sanitize(self, raw) -> list[dict]:
        """把 LLM 输出压回合法编辑集合。

        任何引用不存在的类、越权动作、字段缺失的编辑一律丢弃而不是报错——
        一条坏编辑不应该让整批提案作废，但**绝不能**被放行到 apply_edits。
        """
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        known = set(self.ontology.classes)
        for item in raw:
            if not isinstance(item, dict):
                continue
            action = item.get("action")
            if action not in ALLOWED_ACTIONS:
                continue
            if action == "add_class":
                parent = item.get("parent_class")
                name = str(item.get("class_name") or "").strip()
                if not name or name in known or parent not in known:
                    continue
                out.append(
                    {
                        "action": "add_class",
                        "class_name": name,
                        "parent_class": parent,
                        "definition": str(item.get("definition") or "")[:500],
                        "aliases": [str(a) for a in (item.get("aliases") or []) if str(a).strip()][:8],
                        "required_properties": [
                            str(p) for p in (item.get("required_properties") or [])
                            if str(p) in self.ontology.properties
                        ],
                        "optional_properties": [
                            str(p) for p in (item.get("optional_properties") or [])
                            if str(p) in self.ontology.properties and p not in (item.get("required_properties") or [])
                        ],
                        "layer": self.ontology.classes[parent].layer,
                    }
                )
            elif action == "add_property":
                name = str(item.get("property_name") or "").strip()
                domain, rng = item.get("domain"), item.get("range")
                if not name or name in self.ontology.properties:
                    continue
                if domain not in known or rng not in known:
                    continue
                out.append(
                    {
                        "action": "add_property",
                        "property_name": name,
                        "domain": domain,
                        "range": rng,
                        "kind": "object",
                        "definition": str(item.get("definition") or "")[:300],
                    }
                )
            elif action == "add_alias":
                target = item.get("class_name")
                alias = str(item.get("alias") or "").strip()
                if target not in known or not alias:
                    continue
                out.append({"action": "add_alias", "class_name": target, "alias": alias})
            elif action == "add_property_alias":
                target = item.get("property_name")
                alias = str(item.get("alias") or "").strip()
                prop = self.ontology.properties.get(target)
                if prop is None or not alias or alias == target:
                    continue
                out.append(
                    {"action": "add_property_alias", "property_name": target, "alias": alias}
                )
            elif action == "set_parent":
                target, parent = item.get("class_name"), item.get("parent_class")
                if target not in known or parent not in known or target == parent:
                    continue
                out.append({"action": "set_parent", "class_name": target, "parent_class": parent})
            elif action == "add_disjoint":
                a, b = item.get("class_a"), item.get("class_b")
                if a not in known or b not in known or a == b:
                    continue
                out.append({"action": "add_disjoint", "class_a": a, "class_b": b})
        return out


__all__ = ["EditProposal", "EditProposer", "ALLOWED_ACTIONS"]
