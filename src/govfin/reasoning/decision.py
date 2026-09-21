"""决策合成：把若干条被采纳的推理链收敛成一个可解释的结论。

这一层要回答的不是"图上有没有关系"，而是"给定这些事实和这些监管条款，
这笔业务该怎么办"。因此它的输出必须同时包含**结论**和**结论的完整依据**——
一个不带依据的结论在信贷场景里没有价值，风控人员无从复核，监管也无从追责。

合成过程刻意做成**有顺序的漏斗**，而不是把所有路径加权平均：

1. **真实性闸门**。政务登记数据都核不实的对象，后面无论算出多低的关联风险
   都不该进入授信流程。这一关不过就直接终止，不产生后续步骤——审计记录里
   会明确留下"在哪一步停下的、为什么"。
2. **关联方传导**。沿实际控制人、参股关系走到关联企业的政务记录上，
   把该主体自己看不见的风险暴露出来。
3. **阈值判定**。把上一步拿到的风险指标上溯到监管条款，取出**判定边界**，
   再把观测值与边界比较，得出"触发/未触发"。

阈值节点的语义在实现上有一个容易搞错的地方：``比较方向`` 描述的是条款里
被观测对象的方向（"资产负债率**高于**百分之七十"、"参保人数**低于**合理区间"），
但它并不总是等于触发条件。中文监管条款大量使用"上限/下限"这类量词命名阈值，
量词本身才是触发语义的载体——"连续异常月数**上限**为三"的含义是触达三即触发，
而不是必须超过三。因此这里优先按名称量词判定，量词缺位时才回落到比较方向。
"""

from __future__ import annotations

import hashlib
import itertools
import time
from dataclasses import dataclass, field

from govfin.graph.confidence import ConfidenceModel, PathConfidence
from govfin.graph.store import GraphStore
from govfin.reasoning.constraints import AUTHENTICITY, CONTAGION, CREDIT_CHAIN
from govfin.reasoning.path import PathEngine, PathRecord

# 名称里带这些量词的阈值，被观测对象是"事件发生的规模"而不是指标取值本身。
# 图谱里的事件就是挂在同一风险维度下的风险指标，因此观测值取指标条数。
_COUNT_UNITS = ("月数", "次数", "个数", "家数", "笔数", "天数", "条数", "项数")

VERDICT_REJECTED = "不予受理"
VERDICT_REVIEW = "审慎核定"
VERDICT_PASS = "建议通过"
VERDICT_INSUFFICIENT = "证据不足"


@dataclass
class ThresholdJudgement:
    """一次"观测值 vs 判定边界"的比较及其全部依据。"""

    threshold_node: str
    threshold_name: str
    threshold_value: float
    direction: str
    observed_value: float
    observed_basis: str
    triggered: bool
    clause_node: str | None = None
    clause_label: str = ""
    indicator_nodes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "threshold_node": self.threshold_node,
            "threshold_name": self.threshold_name,
            "threshold_value": self.threshold_value,
            "direction": self.direction,
            "observed_value": self.observed_value,
            "observed_basis": self.observed_basis,
            "triggered": self.triggered,
            "clause_node": self.clause_node,
            "clause_label": self.clause_label,
            "indicator_nodes": self.indicator_nodes,
        }

    def describe(self) -> str:
        sign = "≥" if self.triggered else "<"
        return (
            f"{self.threshold_name}={self.threshold_value:g}，"
            f"观测值为 {self.observed_value:g}（{self.observed_basis}），"
            f"{'触发' if self.triggered else '未触发'}"
            f"（依据：{self.observed_value:g} {sign} {self.threshold_value:g}，"
            f"条款 {self.clause_label or '—'}）"
        )


@dataclass
class Decision:
    decision_id: str
    decision_type: str
    subject_node: str
    subject_label: str
    verdict: str
    confidence: float
    rationale: str
    accepted_paths: list[PathRecord] = field(default_factory=list)
    rejected_paths: list[dict] = field(default_factory=list)
    judgements: list[ThresholdJudgement] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    halted_at: str = ""
    timestamp: str = ""
    path_confidences: list[PathConfidence] = field(default_factory=list)

    @property
    def triggered(self) -> list[ThresholdJudgement]:
        return [j for j in self.judgements if j.triggered]

    def to_dict(self, *, include_paths: bool = True) -> dict:
        out = {
            "decision_id": self.decision_id,
            "decision_type": self.decision_type,
            "subject": {"node": self.subject_node, "label": self.subject_label},
            "verdict": self.verdict,
            "confidence": round(self.confidence, 6),
            "rationale": self.rationale,
            "halted_at": self.halted_at,
            "accepted_path_count": len(self.accepted_paths),
            "rejected_path_count": len(self.rejected_paths),
            "judgements": [j.to_dict() for j in self.judgements],
            "steps": self.steps,
        }
        if include_paths:
            out["accepted_paths"] = [p.to_dict() for p in self.accepted_paths]
            out["rejected_paths"] = self.rejected_paths[:20]
        return out


class DecisionSynthesizer:
    def __init__(
        self,
        store: GraphStore,
        *,
        engine: PathEngine | None = None,
        confidence: ConfidenceModel | None = None,
    ) -> None:
        self.store = store
        self.engine = engine or PathEngine(store)
        self.confidence = confidence or self.engine.confidence

    # ------------------------------------------------------------------

    def synthesize(self, subject_node: str, *, decision_type: str = "授信决策") -> Decision:
        subject = self.store.get_node(subject_node)
        label = subject["label"] if subject else subject_node
        decision = Decision(
            decision_id="",
            decision_type=decision_type,
            subject_node=subject_node,
            subject_label=label,
            verdict=VERDICT_INSUFFICIENT,
            confidence=0.0,
            rationale="",
        )
        if subject is None:
            decision.rationale = f"主体节点不存在: {subject_node}"
            decision.halted_at = "主体解析"
            return decision
        decision.timestamp = _now()
        decision.decision_id = _decision_id(decision_type, subject_node, decision.timestamp)

        auth = self.engine.search(subject_node, AUTHENTICITY)
        self._absorb(decision, auth)
        decision.steps.append(
            {
                "序号": 1,
                "操作": "企业真实性核验",
                "约束": AUTHENTICITY.name,
                "采纳路径": len(auth.paths),
                "被拒路径": len(auth.rejected_paths),
                "中间结论": (
                    f"命中 {len(auth.paths)} 条政务登记证据"
                    if auth.paths
                    else "未取到有效政务登记证据"
                ),
            }
        )
        if not auth.paths:
            decision.halted_at = "企业真实性核验"
            decision.rationale = (
                "未取到可采信的政务登记证据，真实性核验未通过，"
                "按流程终止于此，不进入风险传导与额度判定。"
            )
            decision.verdict = VERDICT_REJECTED
            return decision

        contagion = self.engine.search(subject_node, CONTAGION)
        self._absorb(decision, contagion)
        indicator_ids = self._terminal_nodes(contagion.paths, "风险指标")
        decision.steps.append(
            {
                "序号": 2,
                "操作": "关联方风险传导扫描",
                "约束": CONTAGION.name,
                "采纳路径": len(contagion.paths),
                "被拒路径": len(contagion.rejected_paths),
                "中间结论": f"命中 {len(indicator_ids)} 个风险指标",
            }
        )

        # 同一个阈值可能被同维度下的多个指标各自走到（"连续三个月异常"这件事
        # 天然有三条入口）。它们是同一条判定依据的三种走法，不是三条独立证据，
        # 因此按阈值节点去重，只保留置信度最高的那条链作为该判定的代表。
        best_by_threshold: dict[str, tuple[ThresholdJudgement, PathRecord]] = {}
        for indicator_id in indicator_ids:
            chain = self.engine.search(indicator_id, CREDIT_CHAIN)
            self._absorb(decision, chain)
            for path in chain.paths:
                judgement = self._judge(path)
                if judgement is None:
                    continue
                current = best_by_threshold.get(judgement.threshold_node)
                if current is None or path.confidence.score > current[1].confidence.score:
                    best_by_threshold[judgement.threshold_node] = (judgement, path)

        decision.judgements = [j for j, _ in best_by_threshold.values()]
        decision.judgements.sort(key=lambda j: (-int(j.triggered), -j.threshold_value))
        decision.steps.append(
            {
                "序号": 3,
                "操作": "授信决策链生成",
                "约束": CREDIT_CHAIN.name,
                "采纳路径": len(decision.judgements),
                "被拒路径": len(decision.rejected_paths),
                "中间结论": f"形成 {len(decision.judgements)} 条阈值判定依据",
            }
        )

        stages = self._stage_confidences(decision.accepted_paths)
        decision.path_confidences = list(stages.values())
        decision.confidence = self._combine(stages)
        self._conclude(decision)
        return decision

    # ------------------------------------------------------------------

    def _absorb(self, decision: Decision, result) -> None:
        decision.accepted_paths.extend(result.paths)
        decision.rejected_paths.extend(result.rejected_paths)

    def _stage_confidences(self, paths: list[PathRecord]) -> dict[str, PathConfidence]:
        """每个推理阶段取一条**代表链**，而不是把所有采纳路径都算作证据。

        路径搜索天然会给同一批事实返回多条走法：一个指标可以经实际控制人走到，
        也可以经社保记录走到；"甲科技真实存在"这个结论会因为登记表与营业执照
        各有一条记录而返回两条链。它们要么是同一份证据的不同走法，要么是**并列
        的不同事实**——无论哪种，都不是"多份互证"。

        ``ConfidenceModel.aggregate`` 的噪声或形式会把这些统统当成独立证据，
        6 条路径就足以把置信度顶到 0.9994。这个数字在信贷场景里是危险的假象：
        结论看起来铁证如山，实际依据的只是一份社保记录加一条监管条款。
        因此这里先收敛到"每个阶段一条最强代表链"，再交给合成规则。
        """
        best: dict[str, PathConfidence] = {}
        for path in paths:
            current = best.get(path.constraint)
            if current is None or path.confidence.score > current.score:
                best[path.constraint] = path.confidence
        return best

    def _combine(self, stages: dict[str, PathConfidence]) -> float:
        """把各阶段的代表置信度合成为决策置信度：几何平均。

        阶段之间是**串联**关系——真实性、风险传导、阈值判定缺一不可，任一环不可信
        整个结论就不可信，所以不能取最大或做噪声或。但也不能直接连乘：连乘会让
        "有几个阶段"本身成为主导因素，加一个阶段就必然掉一档，这与
        ``ConfidenceModel`` 反对"跳数主导"的理由同源，只是换了个层面。

        几何平均（等价于在对数域取算术平均）保留了串联语义，又不会被阶段数量
        本身主导。它天然落在 [min, max] 区间内，因此最弱环节始终压制整体置信度，
        无需再叠一层瓶颈保护。
        """
        scores = [c.score for c in stages.values() if c.score > 0]
        if not scores:
            return 0.0
        product = 1.0
        for score in scores:
            product *= score
        return min(1.0, product ** (1.0 / len(scores)))

    def _terminal_nodes(self, paths: list[PathRecord], ntype: str) -> list[str]:
        """取采纳路径的终点中属于指定类型的节点，按出现频次降序、去重。"""
        counts: dict[str, int] = {}
        for path in paths:
            last = path.node_snapshots[-1]
            if last.get("ntype") == ntype:
                counts[last["id"]] = counts.get(last["id"], 0) + 1
        return [nid for nid, _ in sorted(counts.items(), key=lambda kv: -kv[1])]

    def _judge(self, chain: PathRecord) -> ThresholdJudgement | None:
        """把一条 风险指标→维度→条款→阈值 的链翻译成一次阈值判定。"""
        snaps = chain.node_snapshots
        if len(snaps) < 4:
            return None
        dimension, clause, threshold = snaps[-3], snaps[-2], snaps[-1]
        props = threshold.get("props") or {}
        value = props.get("阈值")
        if not isinstance(value, (int, float)):
            return None

        name = str(props.get("阈值名称") or threshold.get("label") or "阈值")
        direction = str(props.get("比较方向") or "")
        indicator_ids = self._indicators_of(dimension["id"])

        if any(unit in name for unit in _COUNT_UNITS):
            observed = float(len(indicator_ids))
            basis = f"同维度风险指标出现 {len(indicator_ids)} 次"
        else:
            values = [
                float(self.store.get_node(i)["props"].get("指标值"))
                for i in indicator_ids
                if self.store.get_node(i)
                and isinstance(self.store.get_node(i)["props"].get("指标值"), (int, float))
            ]
            if not values:
                return None
            observed = max(values)
            basis = f"同维度风险指标最大取值（{len(values)} 项）"

        return ThresholdJudgement(
            threshold_node=threshold["id"],
            threshold_name=name,
            threshold_value=float(value),
            direction=direction,
            observed_value=observed,
            observed_basis=basis,
            triggered=_is_triggered(name, direction, observed, float(value)),
            clause_node=clause.get("id"),
            clause_label=str(clause.get("label") or ""),
            indicator_nodes=indicator_ids,
        )

    def _indicators_of(self, dimension_id: str) -> list[str]:
        return sorted(
            edge["dst"] for edge in self.store.find_edges(src=dimension_id, etype="对应指标")
        )

    def _conclude(self, decision: Decision) -> None:
        triggered = decision.triggered
        if triggered:
            decision.verdict = VERDICT_REVIEW
            detail = "；".join(j.describe() for j in triggered)
            decision.rationale = (
                f"真实性核验通过。关联方传导命中 {len(decision.judgements)} 条判定依据，"
                f"其中 {len(triggered)} 条触发监管阈值：{detail}。"
                "按条款要求应审慎核定授信额度并追加担保。"
            )
        elif decision.judgements:
            decision.verdict = VERDICT_PASS
            decision.rationale = (
                f"真实性核验通过，{len(decision.judgements)} 条判定依据均未触发监管阈值，"
                "可正常受理。"
            )
        elif decision.accepted_paths:
            decision.verdict = VERDICT_PASS
            decision.rationale = (
                "真实性核验通过，关联方传导未命中风险指标，"
                "且无可比对的监管阈值，按无异常受理。"
            )
        else:
            decision.verdict = VERDICT_INSUFFICIENT
            decision.halted_at = "关联方风险传导扫描"
            decision.rationale = "未取得任何可采信的推理链，无法形成结论。"


def _is_triggered(name: str, direction: str, observed: float, threshold: float) -> bool:
    """判定观测值是否触发了该阈值。

    "上限"是不得触及的上界，触达即触发（``observed >= threshold``）；
    "下限"反之。量词缺位时才回落到条款抽取出的比较方向。
    这里用 >= 而非 > 是有意的：监管条款写"连续三个月"时，第三个月就已经构成
    触发条件，用严格大于会把临界情形漏判——而临界情形恰恰是风控最关心的那批。
    """
    if "上限" in name:
        return observed >= threshold
    if "下限" in name:
        return observed <= threshold
    if direction == "高于":
        return observed > threshold
    if direction == "低于":
        return observed < threshold
    return observed >= threshold


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


# 进程内单调递增的决策序号。见 ``_decision_id``：编号必须一次决策一个。
_DECISION_SEQUENCE = itertools.count(1)


def _decision_id(decision_type: str, subject: str, salt: str) -> str:
    """由"决策类型 + 主体 + 时点"派生一个决策编号。

    时点只精确到秒，因此这里**必须**再掺入两个唯一性来源，否则同一秒内对同一
    主体的两次决策会得到同一个编号。那不是"恰好重名"那么轻——编号是审计里
    决策的唯一身份，而 ``ProvenanceRecorder.record`` 是按编号幂等写入的：
    编号相同，后一次决策会把前一次的 Layer3 记录整体覆盖掉，包括它冻结下来的
    证据快照。一次毫秒级的时间巧合，就足以让某次决策的审计记录从此变成另一次
    的，而且全程没有任何异常可捕获。审计记录被静默改写，正是本项目要消灭的
    那类失效。

    两个来源分工明确：进程内序号保证**结构上**唯一（连时钟回拨都不影响），
    纳秒时间戳负责区分同机多进程（多 worker 部署时两个进程的序号会撞）。
    """
    nonce = next(_DECISION_SEQUENCE)
    payload = f"{decision_type}|{subject}|{salt}|{time.time_ns()}|{nonce}"
    return "DEC-" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12].upper()


__all__ = [
    "Decision",
    "DecisionSynthesizer",
    "ThresholdJudgement",
    "VERDICT_REJECTED",
    "VERDICT_REVIEW",
    "VERDICT_PASS",
    "VERDICT_INSUFFICIENT",
]
