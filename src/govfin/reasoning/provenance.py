"""决策溯源：把一次推理固化进 Layer3，并支持事后完整回放。

这一层解决的是"凭什么这么判"这个问题在时间维度上的难点。决策作出时图上的
数据是这样的，三个月后社保记录被补录了、条款被修订了、本体演化了——**决策
当时看到的证据必须原样保留**。否则审计时重新跑一遍推理，得到的是一份用今天的
数据算出来的、看起来更漂亮的解释，而那份解释与当初的决策无关。这就是"可追溯"
和"事后编故事"的分界线。

因此这里做两件事：

1. **冻结**。每条采纳路径连同它的节点快照（实体属性、条款原文、证据片段）
   整体写进 Layer3，成为决策节点的一个不可变属性。图上的数据后续如何变化，
   都不影响这份快照。
2. **可回放**。Layer3 的节点与边严格复用三层本体的既有类和属性
   （``收纳路径``、``包含步骤``、``决策依据条款``……），不另起一套报表结构。
   这意味着决策链本身也是一段图，可以用同一套跨层查询和路径搜索去问它——
   "哪些决策引用了这条已废止的条款"这种监管问询可以直接在图上一跳回答。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from govfin.graph.schema import LAYER_RUNTIME
from govfin.graph.store import GraphStore
from govfin.reasoning.decision import Decision
from govfin.reasoning.path import PathRecord

FROZEN_KEY = "__frozen__"


@dataclass
class ProvenanceReport:
    decision_id: str
    decision_node: str
    step_nodes: list[str] = field(default_factory=list)
    path_nodes: list[str] = field(default_factory=list)
    rejected_nodes: list[str] = field(default_factory=list)
    evidence_bundle: str = ""
    clause_nodes: list[str] = field(default_factory=list)
    entity_nodes: list[str] = field(default_factory=list)
    edges_created: int = 0

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "decision_node": self.decision_node,
            "steps": len(self.step_nodes),
            "accepted_paths": len(self.path_nodes),
            "rejected_paths": len(self.rejected_nodes),
            "evidence_bundle": self.evidence_bundle,
            "clauses": self.clause_nodes,
            "entities": self.entity_nodes,
            "edges_created": self.edges_created,
        }


class ProvenanceRecorder:
    """决策 → Layer3。写入是幂等的：同一决策号重复记录会覆盖同名节点。"""

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def record(self, decision: Decision) -> ProvenanceReport:
        report = ProvenanceReport(
            decision_id=decision.decision_id, decision_node=f"决策:{decision.decision_id}"
        )
        self.store.upsert_node(
            "授信决策",
            node_id=report.decision_node,
            layer=LAYER_RUNTIME,
            props={
                "决策编号": decision.decision_id,
                "决策时点": decision.timestamp,
                "决策类型": decision.decision_type,
                "结论": decision.verdict,
                "决策理由": decision.rationale,
                "置信度": round(decision.confidence, 6),
                "决策主体": decision.subject_label,
                "中止阶段": decision.halted_at,
            },
            validate=False,
        )

        report.entity_nodes.append(decision.subject_node)
        report.edges_created += self._link(report.decision_node, decision.subject_node, "决策涉及实体")

        for judgement in decision.judgements:
            if judgement.clause_node:
                report.clause_nodes.append(judgement.clause_node)
                report.edges_created += self._link(
                    report.decision_node, judgement.clause_node, "决策依据条款"
                )

        for step in decision.steps:
            step_node = self.store.upsert_node(
                "推理步骤",
                layer=LAYER_RUNTIME,
                props={
                    "步骤序号": int(step["序号"]),
                    "操作": str(step["操作"]),
                    "中间结论": str(step["中间结论"]),
                    "引用约束": str(step.get("约束", "")),
                },
                node_id=f"推理步骤:{decision.decision_id}#{step['序号']}",
                validate=False,
            )
            report.step_nodes.append(step_node)
            report.edges_created += self._link(report.decision_node, step_node, "包含步骤")

        # 证据束：把这次决策真正依据的条款、实体、指标打包成一个可独立引用的单元。
        # 决策依据条款边只连到条款，"用到企业哪些字段"这个信息会丢，所以证据束里
        # 必须带实体节点。
        bundle_id = f"证据束:{decision.decision_id}"
        self.store.upsert_node(
            "证据束",
            node_id=bundle_id,
            layer=LAYER_RUNTIME,
            props={
                "束编号": decision.decision_id,
                "证据数": len(decision.accepted_paths),
                "综合置信度": round(decision.confidence, 6),
                "判定依据": [j.describe() for j in decision.judgements],
            },
            validate=False,
        )
        report.evidence_bundle = bundle_id
        for judgement in decision.judgements:
            for indicator in judgement.indicator_nodes:
                report.edges_created += self._link(bundle_id, indicator, "支撑结论")

        by_path_id: dict[str, str] = {}
        for path in decision.accepted_paths:
            node = self._record_path(decision, path)
            by_path_id[path.path_id] = node
            report.path_nodes.append(node)
            report.edges_created += self._link(report.decision_node, node, "收纳路径")

        # 步骤 → 它自己那个阶段产出的路径。少了这批边，"哪条链属于哪一步"
        # 就得靠约束名去猜，审计报告只能把路径平铺出来，讲不成因果顺序。
        for step_node, step in zip(report.step_nodes, decision.steps):
            for path in decision.accepted_paths:
                if path.constraint != step.get("约束"):
                    continue
                target = by_path_id.get(path.path_id)
                if target:
                    report.edges_created += self._link(step_node, target, "步骤使用证据")

        for rejected in decision.rejected_paths[:50]:
            node = self._record_rejected(decision, rejected)
            report.rejected_nodes.append(node)
            report.edges_created += self._link(report.decision_node, node, "收纳路径")

        return report

    # ------------------------------------------------------------------

    def _record_path(self, decision: Decision, path: PathRecord) -> str:
        node_id = f"采纳路径:{decision.decision_id}:{path.path_id}"
        self.store.upsert_node(
            "采纳路径",
            node_id=node_id,
            layer=LAYER_RUNTIME,
            props={
                "路径编号": path.path_id,
                "跳数": len(path.edges),
                "置信度": round(path.confidence.score, 6),
                "是否采纳": True,
                "约束": path.constraint,
                "证据链": " → ".join(n["label"] for n in path.node_snapshots),
                "跨层次数": path.cross_layer_count,
                # 冻结快照：决策作出时这条链上每个节点的完整属性与每条边的证据。
                # 事后图变了不影响它，这是"可追溯"的物理保障。
                FROZEN_KEY: {
                    "nodes": path.node_snapshots,
                    "edges": [
                        {
                            "etype": e["etype"],
                            "evidence_class": e.get("evidence_class"),
                            "confidence": e.get("confidence"),
                            "evidence": e.get("evidence") or {},
                        }
                        for e in path.edges
                    ],
                    "confidence_breakdown": path.confidence.to_dict(),
                    "constraint": path.constraint,
                },
            },
            validate=False,
        )
        self._link(path.node_ids[0], node_id, "路径起点")
        self._link(path.node_ids[-1], node_id, "路径终点")
        return node_id

    def _record_rejected(self, decision: Decision, rejected: dict) -> str:
        path_id = str(rejected.get("path_id") or "unknown")
        node_id = f"被拒路径:{decision.decision_id}:{path_id}"
        self.store.upsert_node(
            "被拒路径",
            node_id=node_id,
            layer=LAYER_RUNTIME,
            props={
                "路径编号": path_id,
                "跳数": 0,
                "置信度": float(rejected.get("confidence") or 0.0),
                "是否采纳": False,
                "拒绝原因": str(rejected.get("reject_reason") or ""),
                "证据链": str(rejected.get("chain") or ""),
            },
            validate=False,
        )
        return node_id

    def _link(self, src: str, dst: str, etype: str) -> int:
        if not src or not dst:
            return 0
        if self.store.get_node(src) is None or self.store.get_node(dst) is None:
            return 0
        self.store.add_edge(src, dst, etype, validate=False)
        return 1


__all__ = ["ProvenanceRecorder", "ProvenanceReport", "FROZEN_KEY"]
