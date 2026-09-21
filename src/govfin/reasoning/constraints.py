"""路径约束：把"要查什么"翻译成图搜索能执行的边界条件。

不受约束的多跳搜索在信贷场景里没有意义——从"甲科技"出发，四跳之内几乎必然
能走到某个风险指标，"有关系"于是变成一句废话。约束的作用是让每一条返回的
路径都回答一个**具体的业务问题**，并且这个问题的边界能被审计复现。

三类约束必须同时具备，缺一条结论就不能用：

- **结构约束**（edge_types / layers / hops）：路径形状是否符合该业务问题的推理范式。
- **证据约束**（evidence_classes / min_confidence）：这条链上的每一环能不能拿得出证据。
  允许 LLM 抽取的边参与结论，还是只认政务原始登记数据，是两种完全不同的决策口径。
- **跨层约束**（must_cross_layer）：政务侧的证据必须真的走到金融侧的规则上，
  否则"跨域决策"只是把两个域的结论并排放着。
"""

from __future__ import annotations

from dataclasses import dataclass

from govfin.graph.schema import (
    EVIDENCE_DERIVED,
    EVIDENCE_DIRECT,
    EVIDENCE_LLM,
    LAYER_ENTITY,
    LAYER_RULE,
    LAYER_RUNTIME,
)


@dataclass(frozen=True)
class PathConstraint:
    """一次约束路径搜索的完整边界。"""

    name: str
    description: str
    # 端点类型：起点可接受的本体类型（空表示不限）
    start_types: tuple[str, ...] = ()
    end_types: tuple[str, ...] = ()
    # 允许经过的边类型；None 表示不限（不推荐，会使"有关系"退化为废话）
    allowed_edge_types: tuple[str, ...] | None = None
    # 必须至少出现一次的边类型
    required_edge_types: tuple[str, ...] = ()
    forbidden_edge_types: tuple[str, ...] = ()
    # 必须访问到的图层（跨层推理的硬要求）
    required_layers: tuple[int, ...] = ()
    max_hops: int = 4
    min_edge_confidence: float = 0.0
    # 只允许参与结论的证据类别
    evidence_classes: tuple[str, ...] = (EVIDENCE_DIRECT, EVIDENCE_DERIVED, EVIDENCE_LLM)
    # 端点必须落在这些图层上
    end_layers: tuple[int, ...] = ()
    # 本类结论的**证据充分性门槛**。None 表示沿用全局 decision_threshold。
    #
    # 为什么必须能按约束单独设：路径分数里的 geo 与 hop 两个因子对多跳链有
    # **结构性**折损——一条三跳、每跳都能逐条回指条款原文的链，分数天然只有 0.35
    # 上下。若所有约束共用一个门槛，那么"能不能被采纳"实际上由**跳数**决定，
    # 而不是由证据质量决定。这恰恰是 ConfidenceModel 开篇批评朴素乘积的那个错误，
    # 只是换了个位置复现。门槛表达的应当是"这一类结论需要多强的证据"，
    # 而不同业务问题的答案本来就不一样。
    accept_threshold: float | None = None
    # 允许重复经过同一节点（默认禁止：环路会让置信度衰减失去意义）
    allow_cycles: bool = False
    # 单次搜索最多展开多少条边，超出即截断并如实上报
    expansion_budget: int = 20000

    def allows_layer(self, layer: int) -> bool:
        return not self.required_layers or layer in self.required_layers

    def allows_edge_type(self, etype: str) -> bool:
        if etype in self.forbidden_edge_types:
            return False
        return self.allowed_edge_types is None or etype in self.allowed_edge_types

    def allows_evidence(self, evidence_class: str) -> bool:
        return evidence_class in self.evidence_classes

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "start_types": list(self.start_types),
            "end_types": list(self.end_types),
            "allowed_edge_types": list(self.allowed_edge_types or ()),
            "required_edge_types": list(self.required_edge_types),
            "required_layers": list(self.required_layers),
            "max_hops": self.max_hops,
            "min_edge_confidence": self.min_edge_confidence,
            "accept_threshold": self.accept_threshold,
            "evidence_classes": list(self.evidence_classes),
            "end_layers": list(self.end_layers),
            "allow_cycles": self.allow_cycles,
        }


# --------------------------------------------------------------------------
# 三个业务意图 = 三份 Skill 模板的推理内核
# --------------------------------------------------------------------------

# 企业真实性核验：从企业走到政务登记记录，只认政务原始数据。
# 刻意把 LLM 抽取的边排除在外——"这家企业是否真实存在"这个问题的答案
# 必须来自登记机关，不能来自某个模型觉得它像存在。
AUTHENTICITY = PathConstraint(
    name="企业真实性核验",
    description="从申请主体出发，沿政务登记关系核验其登记状态、社保缴纳与行政处罚事实",
    start_types=("企业",),
    end_types=("工商登记", "社保缴纳记录", "行政处罚", "税务缴纳记录"),
    allowed_edge_types=("拥有工商登记", "缴纳社保", "受到处罚", "申报税务"),
    required_edge_types=("拥有工商登记",),
    max_hops=2,
    min_edge_confidence=0.5,
    evidence_classes=(EVIDENCE_DIRECT,),
    # 门槛定得高：这条链只有 1~2 跳直接证据，分数天然在 0.85 以上。
    # 高门槛在这里的作用不是"卡住长链"，而是当政务数据缺项、证据不完整时
    # 拒绝对企业真实性下结论。
    accept_threshold=0.8,
)

# 关联方风险传导：企业 → 关联主体 → 关联企业的政务记录 → 风险指标。
#
# 边类型白名单必须同时包含"走出去"（实际控制人、参股）和"落到证据上"
# （政务关系 + 触发风险信号）两类，否则这条约束永远无法终止——终点类型是
# 风险指标，而通向风险指标的唯一关系就是政务记录触发的风险信号。
CONTAGION = PathConstraint(
    name="关联方风险传导扫描",
    description="沿实际控制人、法定代表人、参股关系走到关联企业，再落到其政务记录触发的风险指标上",
    start_types=("企业",),
    end_types=("风险指标",),
    allowed_edge_types=(
        "实际控制人",
        "法定代表人",
        "企业参股",
        "控股",
        "被控股",
        "任职于",
        "拥有工商登记",
        "缴纳社保",
        "受到处罚",
        "涉及诉讼",
        "申报税务",
        "遭受环保核查",
        "触发风险信号",
    ),
    required_edge_types=("触发风险信号",),
    max_hops=4,
    min_edge_confidence=0.4,
    evidence_classes=(EVIDENCE_DIRECT, EVIDENCE_DERIVED),
    # 四跳链上每跳都有一份政务原始记录，0.5 表达的是"关联推断可以进画像"。
    accept_threshold=0.5,
)

# 授信决策链：风险指标 → 风险维度 → 监管条款 → 判定阈值。
#
# 这是跨域决策的核心路径：起点在政务/财务事实，终点落在金融监管规则上，
# required_layers 强制它必须跨到规则图，否则这条链就只是内部画像。
#
# 终点刻意定成「阈值」而不是「监管规范」：止步于条款只能证明"存在一条监管要求"，
# 取到阈值才能回答"这条业务的判定边界是多少"。前者不可执行，不配叫决策依据。
# 因此没有可解析阈值的条款不会进入结果集，而会连同原因留在 rejected_paths 里。
CREDIT_CHAIN = PathConstraint(
    name="授信决策链生成",
    description="从风险指标上溯到风险维度与监管条款，并取到判定阈值，形成可追溯、可执行的授信依据链",
    start_types=("风险指标",),
    end_types=("阈值",),
    allowed_edge_types=("对应指标", "映射维度", "设定阈值", "约束维度", "细化"),
    required_edge_types=("映射维度", "设定阈值"),
    required_layers=(LAYER_ENTITY, LAYER_RULE),
    end_layers=(LAYER_RULE,),
    max_hops=4,
    min_edge_confidence=0.5,
    evidence_classes=(EVIDENCE_DIRECT, EVIDENCE_DERIVED),
    # 三跳，每跳都能逐条回指条款原文与指标原文。门槛低不是因为要求松，
    # 而是因为分数里的多跳折损已经扣过了；再叠一个高门槛等于重复惩罚跳数。
    accept_threshold=0.35,
)

# 阈值判定链：条款 → 阈值 → 维度，用于回答"这条指标的判定边界是多少"。
THRESHOLD_CHAIN = PathConstraint(
    name="阈值判定",
    description="从监管条款取出判定阈值及其约束的风险维度",
    start_types=("监管规范", "监管条款", "授信指引条款", "定价规则条款", "政策条款", "数据规范"),
    end_types=("阈值",),
    allowed_edge_types=("设定阈值", "约束维度", "映射维度"),
    required_edge_types=("设定阈值",),
    max_hops=2,
    end_layers=(LAYER_RULE,),
    evidence_classes=(EVIDENCE_DIRECT, EVIDENCE_DERIVED),
    accept_threshold=0.35,
)

BUILTIN_CONSTRAINTS: dict[str, PathConstraint] = {
    c.name: c for c in (AUTHENTICITY, CONTAGION, CREDIT_CHAIN, THRESHOLD_CHAIN)
}


def constraint_for(name: str) -> PathConstraint:
    if name not in BUILTIN_CONSTRAINTS:
        raise KeyError(f"未定义的路径约束: {name!r}；可用: {sorted(BUILTIN_CONSTRAINTS)}")
    return BUILTIN_CONSTRAINTS[name]


__all__ = [
    "PathConstraint",
    "AUTHENTICITY",
    "CONTAGION",
    "CREDIT_CHAIN",
    "THRESHOLD_CHAIN",
    "BUILTIN_CONSTRAINTS",
    "constraint_for",
]
