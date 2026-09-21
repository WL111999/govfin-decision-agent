"""决策审计：从决策节点出发反向走回原始证据，并检测依据是否已经失效。

审计与推理是**两个方向**的同一张图。推理从事实走向结论（企业 → 社保记录 →
风险指标 → 条款 → 阈值），审计从结论走回事实。两条路径都必须是图上的真实边，
而不是靠 ID 前缀约定的字符串拼接——那样一旦有人改了 ID 规则，审计就会静默断链。

``drift`` 是这个模块里最值得留意的一项。决策把证据快照冻结在 Layer3，
时间一长，层 1 的社保记录被补录、层 2 的条款被修订，就会与冻结快照不一致。
这种不一致**不是错误**——决策当时确实依据的是旧数据。但它是监管问询里最常被
追问的一件事："这条结论放到今天还成立吗？" 因此系统必须能主动把它指出来，
而不是等审计员逐字段比对。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from govfin.reasoning.provenance import FROZEN_KEY
from govfin.graph.store import GraphStore

# 冻结快照里与"结论是否仍然成立"无关的字段：本体版本、内部键等。
_IGNORED_PROPS = {"id", "ontology_version", "create_time", "update_time"}


@dataclass
class AuditFinding:
    kind: str  # clause_excerpt | entity_state | evidence | drift
    label: str
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "label": self.label, "detail": self.detail}


@dataclass
class AuditReport:
    decision_id: str
    subject: str = ""
    verdict: str = ""
    confidence: float = 0.0
    timestamp: str = ""
    steps: list[dict] = field(default_factory=list)
    accepted_chains: list[dict] = field(default_factory=list)
    rejected_chains: list[dict] = field(default_factory=list)
    clauses: list[dict] = field(default_factory=list)
    drift: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "subject": self.subject,
            "verdict": self.verdict,
            "confidence": round(self.confidence, 6),
            "timestamp": self.timestamp,
            "steps": self.steps,
            "accepted_chains": self.accepted_chains,
            "rejected_chains": self.rejected_chains,
            "clauses": self.clauses,
            "drift_count": len(self.drift),
            "drift": self.drift,
        }

    def render(self, *, chains_per_constraint: int = 2) -> str:
        """人类可读的审计报告。监管问询要的就是这一页。

        每个约束只详列置信度最高的 ``chains_per_constraint`` 条链。同一批事实往往
        有十几条走法（一个指标经实际控制人走到、经社保记录也能走到），全铺出来
        会把关键信息淹没在重复里；但被略去的条数必须如实写明，否则报告的读者会
        以为那就是全部。
        """
        lines = [
            f"决策审计报告  {self.decision_id}",
            f"  主体      {self.subject}",
            f"  结论      {self.verdict}   （置信度 {self.confidence:.4f}）",
            f"  决策时点  {self.timestamp}",
        ]
        for step in self.steps:
            lines.append(
                f"  步骤 {step['序号']}  {step['操作']} —— {step['中间结论']}"
            )
        if self.accepted_chains:
            lines.append(f"  采纳的推理链（共 {len(self.accepted_chains)} 条）：")
            shown: dict[str, int] = {}
            for chain in self.accepted_chains:
                key = chain["constraint"]
                shown[key] = shown.get(key, 0) + 1
                if shown[key] > chains_per_constraint:
                    continue
                lines.append(
                    f"    [{key}] {chain['chain']}"
                    f"   置信度 {chain['confidence']:.4f}  {chain['hops']} 跳"
                )
                for edge in chain.get("evidence", []):
                    snippet = edge.get("snippet") or "（无片段）"
                    lines.append(
                        f"        ← {edge['etype']}｜{edge.get('evidence_class')}"
                        f"｜{edge.get('source_document') or '—'}｜{snippet}"
                    )
            omitted = {
                key: count - chains_per_constraint
                for key, count in shown.items()
                if count > chains_per_constraint
            }
            if omitted:
                lines.append(
                    "    （同类走法已折叠："
                    + "、".join(f"{k} 另 {v} 条" for k, v in omitted.items())
                    + "）"
                )
        if self.clauses:
            lines.append("  依据条款原文：")
            for clause in self.clauses:
                lines.append(f"    《{clause['label']}》")
                lines.append(f"        {clause['text']}")
        if self.rejected_chains:
            lines.append("  被拒绝的推理链（保留以便复核）：")
            for chain in self.rejected_chains:
                lines.append(f"    ✗ {chain['chain']}")
                lines.append(f"      理由：{chain['reason']}")
        if self.drift:
            lines.append("  ⚠ 依据漂移（决策作出后图上的数据已变化）：")
            for item in self.drift:
                lines.append(f"    {item['node']} 的「{item['prop']}」"
                             f"：决策时 {item['frozen']!r} → 现在 {item['current']!r}")
        else:
            lines.append("  ✓ 未检测到依据漂移：决策所引用的证据与当前图状态一致。")
        return "\n".join(lines)


class AuditTrail:
    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def trace(self, decision_id: str) -> AuditReport:
        node_id = decision_id if decision_id.startswith("决策:") else f"决策:{decision_id}"
        node = self.store.get_node(node_id)
        if node is None:
            raise KeyError(f"未找到决策节点: {node_id}")
        props = node["props"]
        report = AuditReport(
            decision_id=str(props.get("决策编号") or node_id),
            subject=str(props.get("决策主体") or ""),
            verdict=str(props.get("结论") or ""),
            confidence=float(props.get("置信度") or 0.0),
            timestamp=str(props.get("决策时点") or ""),
        )

        for step_node in self._targets(node_id, "包含步骤"):
            step = step_node["props"]
            report.steps.append(
                {
                    "序号": step.get("步骤序号"),
                    "操作": step.get("操作"),
                    "中间结论": step.get("中间结论"),
                }
            )
        report.steps.sort(key=lambda s: s.get("序号") or 0)

        for path_node in self._targets(node_id, "收纳路径"):
            chain = self._render_chain(path_node)
            if chain is None:
                continue
            if path_node["ntype"] == "采纳路径":
                report.accepted_chains.append(chain)
            else:
                report.rejected_chains.append(
                    {"chain": chain["chain"], "reason": chain["reason"]}
                )
        report.accepted_chains.sort(key=lambda c: -c["confidence"])

        for clause_node in self._targets(node_id, "决策依据条款"):
            report.clauses.append(
                {
                    "node": clause_node["id"],
                    "label": clause_node["label"],
                    "text": str(clause_node["props"].get("条款原文") or "（无原文）"),
                }
            )

        report.drift = self.detect_drift(decision_id)
        return report

    # ------------------------------------------------------------------

    def detect_drift(self, decision_id: str) -> list[dict]:
        """比对冻结快照与当前图状态，列出决策依据中已经变化的属性。

        一条路径可能与其他路径共享节点，一个节点也可能被多条采纳路径引用；
        同一个变化只应该报一次，否则审计报告里会出现七行一模一样的"断缴→正常"，
        让人误以为有七处问题。
        """
        node_id = decision_id if decision_id.startswith("决策:") else f"决策:{decision_id}"
        drift: list[dict] = []
        seen: set[tuple] = set()

        def _add(item: dict) -> None:
            key = (item["node"], item["prop"], repr(item["frozen"]), repr(item["current"]))
            if key in seen:
                return
            seen.add(key)
            drift.append(item)

        for path_node in self._targets(node_id, "收纳路径"):
            frozen = path_node["props"].get(FROZEN_KEY)
            if not isinstance(frozen, dict):
                continue
            for snap in frozen.get("nodes") or []:
                current = self.store.get_node(snap.get("id"))
                if current is None:
                    _add(
                        {
                            "node": snap.get("label") or snap.get("id"),
                            "prop": "（节点）",
                            "frozen": "存在",
                            "current": "已被删除",
                        }
                    )
                    continue
                old = snap.get("props") or {}
                new = current.get("props") or {}
                for key, value in old.items():
                    if key in _IGNORED_PROPS:
                        continue
                    if key not in new:
                        _add(
                            {
                                "node": current["label"],
                                "prop": key,
                                "frozen": value,
                                "current": "已被删除",
                            }
                        )
                    elif new[key] != value:
                        _add(
                            {
                                "node": current["label"],
                                "prop": key,
                                "frozen": value,
                                "current": new[key],
                            }
                        )
        return drift

    def decisions_referencing(self, node_id: str) -> list[dict]:
        """反向查询：哪些决策引用了这个节点。

        "这条条款已废止，之前哪些授信结论建立在它上面"——监管问询里的高频问题，
        在图上就是一次入边查询。这正是把决策链也建成图、而不是写进报表的价值。
        """
        out: list[dict] = []
        for edge in self.store.find_edges(dst=node_id):
            if edge["etype"] not in ("决策依据条款", "决策涉及实体", "路径终点", "路径起点"):
                continue
            source = self.store.get_node(edge["src"])
            if source is None:
                continue
            decision = source
            if source["ntype"] != "授信决策":
                parents = self._targets(source["id"], "包含步骤", reverse=True)
                parents = [p for p in parents if p["ntype"] == "授信决策"]
                if not parents:
                    continue
                decision = parents[0]
            if decision["id"] in {d["decision_node"] for d in out}:
                continue
            out.append(
                {
                    "decision_node": decision["id"],
                    "decision_id": decision["props"].get("决策编号"),
                    "subject": decision["props"].get("决策主体"),
                    "verdict": decision["props"].get("结论"),
                    "via": edge["etype"],
                }
            )
        return out

    # ------------------------------------------------------------------

    def _targets(self, node_id: str, etype: str, *, reverse: bool = False) -> list[dict]:
        edges = (
            self.store.find_edges(dst=node_id, etype=etype)
            if reverse
            else self.store.find_edges(src=node_id, etype=etype)
        )
        out = []
        for edge in edges:
            other = self.store.get_node(edge["src"] if reverse else edge["dst"])
            if other is not None:
                out.append(other)
        return out

    def _render_chain(self, path_node: dict) -> dict | None:
        props = path_node["props"]
        frozen = props.get(FROZEN_KEY)
        if not isinstance(frozen, dict):
            return {
                "constraint": props.get("约束", ""),
                "chain": props.get("证据链", ""),
                "confidence": float(props.get("置信度") or 0.0),
                "hops": int(props.get("跳数") or 0),
                "evidence": [],
                "reason": props.get("拒绝原因", ""),
            }
        evidence = []
        for edge in frozen.get("edges") or []:
            payload = edge.get("evidence") or {}
            evidence.append(
                {
                    "etype": edge.get("etype"),
                    "evidence_class": edge.get("evidence_class"),
                    "snippet": payload.get("snippet"),
                    "source_document": payload.get("source_document"),
                    "source_locator": payload.get("source_locator"),
                    "modality": payload.get("modality"),
                }
            )
        return {
            "constraint": props.get("约束", ""),
            "chain": props.get("证据链", ""),
            "confidence": float(props.get("置信度") or 0.0),
            "hops": int(props.get("跳数") or 0),
            "evidence": evidence,
            "reason": props.get("拒绝原因", ""),
        }


__all__ = ["AuditFinding", "AuditReport", "AuditTrail"]
