"""置信度传播：修正衰减模型。

朴素做法是沿路径把边权相乘。这有两个致命缺陷，在信贷决策场景下都不能接受：

1. **乘积对跳数过度敏感**——4 跳 0.95 的边相乘只剩 0.81，但 4 跳 0.6 的边相乘
   只剩 0.13；同为"4 跳"，两者差距被放大到 6 倍以上，跳数本身而不是证据质量
   成了主导因素。
2. **弱边可被强边掩盖**——一条 0.99 的边乘以一条 0.2 的边得 0.198，和两条
   0.45 的边乘积相同。但前者是"一个环节完全不可信"，后者是"整体都一般"，
   合规审核上必须区分。

因此本模型把置信度拆成三个独立因子再合成：

    conf = geo × hop × btn

    geo = exp(-Σ λ_class(eᵢ) · (1 - wᵢ))   证据质量衰减，λ 按证据类别取值
    hop = β^(n-1)                          跳数衰减，模拟语义漂移
    btn = γ + (1-γ) · min(wᵢ)              瓶颈保护，锚定最弱环节

三个因子各自有界，乘出来仍在 [0,1]，且对"弱边"和"长链"分别惩罚、互不掩盖。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from govfin.config import ConfidenceConfig, get_settings
from govfin.graph.schema import EVIDENCE_DERIVED, EVIDENCE_DIRECT, EVIDENCE_LLM


@dataclass
class EdgeWeight:
    """一条边在传播中的实际权重及其来源解释。审计需要知道"这个 0.62 是怎么来的"。"""

    edge_id: str
    etype: str
    weight: float
    evidence_class: str
    source: str  # 权重来源: explicit | rule_clarity | feedback_calibrated | default
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "edge_id": self.edge_id,
            "etype": self.etype,
            "weight": round(self.weight, 6),
            "evidence_class": self.evidence_class,
            "source": self.source,
            "detail": self.detail,
        }


@dataclass
class PathConfidence:
    score: float
    hop_count: int
    geometry_factor: float
    hop_factor: float
    bottleneck_factor: float
    min_edge_weight: float
    threshold: float
    accepted: bool
    reject_reason: str | None
    edge_weights: list[EdgeWeight] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 6),
            "hop_count": self.hop_count,
            "geometry_factor": round(self.geometry_factor, 6),
            "hop_factor": round(self.hop_factor, 6),
            "bottleneck_factor": round(self.bottleneck_factor, 6),
            "min_edge_weight": round(self.min_edge_weight, 6),
            "threshold": self.threshold,
            "accepted": self.accepted,
            "reject_reason": self.reject_reason,
            "edge_weights": [w.to_dict() for w in self.edge_weights],
        }


class ConfidenceModel:
    """把边和路径映射到置信度。无状态，唯一的外部依赖是图存储提供的反馈计数。"""

    def __init__(self, config: ConfidenceConfig | None = None) -> None:
        self.config = config or get_settings().confidence

    # ---------- 单边 ----------

    def lambda_for(self, evidence_class: str) -> float:
        return {
            EVIDENCE_DIRECT: self.config.lambda_direct,
            EVIDENCE_DERIVED: self.config.lambda_derived,
            EVIDENCE_LLM: self.config.lambda_llm,
        }.get(evidence_class, self.config.lambda_derived)

    def edge_weight(
        self,
        edge: dict,
        *,
        rule_clarity: float | None = None,
        feedback: tuple[int, int] | None = None,
    ) -> EdgeWeight:
        """解析一条边的传播权重。

        - direct：直接用登记数据自带的置信度，登记数据本身不带不确定性。
        - derived：权重由所援引 Layer2 条款的"条款明确程度"决定。条款措辞越模糊，
          推导越不可靠——这是金融监管语境下的真实性质，不是拍脑袋的权重。
        - llm：用 Beta 后验随历史验证反馈校准。cold start 时回退到先验均值。
        """
        etype = edge["etype"]
        cls = edge.get("evidence_class", EVIDENCE_DIRECT)
        explicit = float(edge.get("confidence", 1.0))

        if cls == EVIDENCE_DERIVED and rule_clarity is not None:
            clarity = _clamp(rule_clarity)
            # 条款明确程度与边自带置信度做几何平均：两者都是对这次推导的独立约束
            w = math.sqrt(max(clarity, 1e-9) * max(explicit, 1e-9))
            return EdgeWeight(
                edge_id=edge["id"],
                etype=etype,
                weight=_clamp(w),
                evidence_class=cls,
                source="rule_clarity",
                detail={"rule_clarity": round(clarity, 6), "edge_confidence": explicit},
            )

        if cls == EVIDENCE_LLM:
            succ, fail = feedback if feedback is not None else (0, 0)
            a = self.config.llm_prior_alpha + succ
            b = self.config.llm_prior_beta + fail
            posterior = a / (a + b)
            w = math.sqrt(max(posterior, 1e-9) * max(explicit, 1e-9))
            return EdgeWeight(
                edge_id=edge["id"],
                etype=etype,
                weight=_clamp(w),
                evidence_class=cls,
                source="feedback_calibrated",
                detail={
                    "successes": succ,
                    "failures": fail,
                    "posterior_mean": round(posterior, 6),
                    "edge_confidence": explicit,
                },
            )

        return EdgeWeight(
            edge_id=edge["id"],
            etype=etype,
            weight=_clamp(explicit),
            evidence_class=cls,
            source="explicit",
            detail={"edge_confidence": explicit},
        )

    # ---------- 路径 ----------

    def path_confidence(
        self,
        edges: Sequence[dict],
        *,
        rule_clarity: dict[str, float] | None = None,
        feedback: dict[str, tuple[int, int]] | None = None,
        threshold: float | None = None,
    ) -> PathConfidence:
        clarity_map = rule_clarity or {}
        feedback_map = feedback or {}
        thr = self.config.decision_threshold if threshold is None else threshold

        weights = [
            self.edge_weight(
                e,
                rule_clarity=clarity_map.get(e["id"]),
                feedback=feedback_map.get(e["etype"]),
            )
            for e in edges
        ]
        n = len(weights)
        if n == 0:
            return PathConfidence(
                score=1.0,
                hop_count=0,
                geometry_factor=1.0,
                hop_factor=1.0,
                bottleneck_factor=1.0,
                min_edge_weight=1.0,
                threshold=thr,
                accepted=True,
                reject_reason=None,
                edge_weights=[],
            )

        penalty = sum(self.lambda_for(w.evidence_class) * (1.0 - w.weight) for w in weights)
        geo = math.exp(-penalty)
        hop = self.config.hop_decay ** (n - 1)
        min_w = min(w.weight for w in weights)
        btn = self.config.bottleneck_floor + (1.0 - self.config.bottleneck_floor) * min_w

        score = _clamp(geo * hop * btn)
        accepted = score >= thr
        reason = None
        if not accepted:
            weakest = min(weights, key=lambda w: w.weight)
            reason = (
                f"置信度 {score:.4f} 低于阈值 {thr:.4f}；"
                f"最弱环节为「{weakest.etype}」(权重 {weakest.weight:.4f}, 来源 {weakest.source})"
            )

        return PathConfidence(
            score=score,
            hop_count=n,
            geometry_factor=geo,
            hop_factor=hop,
            bottleneck_factor=btn,
            min_edge_weight=min_w,
            threshold=thr,
            accepted=accepted,
            reject_reason=reason,
            edge_weights=weights,
        )

    # ---------- 决策级合成 ----------

    def aggregate(self, accepted: Sequence[PathConfidence]) -> float:
        """多条采纳路径合成一个决策置信度。

        用 1-Π(1-c) 的噪声或形式而非取最大值：多条独立证据链同时指向同一结论时，
        整体可信度应当上升。取最大值会浪费掉"多路互证"这个信号。
        """
        if not accepted:
            return 0.0
        product = 1.0
        for pc in accepted:
            product *= 1.0 - _clamp(pc.score)
        return _clamp(1.0 - product)


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if math.isnan(value):
        return lo
    return max(lo, min(hi, value))
