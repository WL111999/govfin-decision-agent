"""三层图 schema 常量与跨层边规则。"""

from __future__ import annotations

from dataclasses import dataclass

LAYER_ENTITY = 1
LAYER_RULE = 2
LAYER_RUNTIME = 3
LAYER_META = 0

LAYER_NAMES = {
    LAYER_ENTITY: "实体图",
    LAYER_RULE: "规则图",
    LAYER_RUNTIME: "运行时图",
    LAYER_META: "演化元层",
}

# 证据类别直接决定置信度传播时的惩罚系数，见 config.ConfidenceConfig
EVIDENCE_DIRECT = "direct"      # 政务/金融系统的原始登记数据
EVIDENCE_DERIVED = "derived"    # 由规则条款推导出来的边
EVIDENCE_LLM = "llm"            # LLM 抽取或推断产生的边

EVIDENCE_CLASSES = (EVIDENCE_DIRECT, EVIDENCE_DERIVED, EVIDENCE_LLM)

# 模态来源，对应认知层的五元组
MODALITY_TEXT = "text"
MODALITY_TABLE = "table"
MODALITY_IMAGE = "image"
MODALITY_JSON = "json"
MODALITY_PDF = "pdf"

MODALITIES = (MODALITY_TEXT, MODALITY_TABLE, MODALITY_IMAGE, MODALITY_JSON, MODALITY_PDF)


@dataclass(frozen=True)
class CrossLayerRule:
    """跨层边的合法组合。写入时强制校验，避免图结构被脏数据污染。"""

    src_layer: int
    dst_layer: int
    etype: str
    direction: str  # up = 下层指向上层, down = 上层指向下层
    description: str


CROSS_LAYER_RULES: tuple[CrossLayerRule, ...] = (
    CrossLayerRule(1, 2, "受约束于", "up", "实体所适用的监管条款"),
    CrossLayerRule(2, 1, "适用于", "down", "条款所约束的主体范围"),
    CrossLayerRule(2, 1, "对应指标", "down", "风险维度在主体上的指标实例化"),
    CrossLayerRule(3, 2, "决策依据条款", "up", "决策援引的条款"),
    CrossLayerRule(3, 2, "步骤引用条款", "up", "推理步骤援引的条款"),
    CrossLayerRule(3, 1, "决策涉及实体", "up", "决策激活的实体"),
    CrossLayerRule(3, 1, "步骤引用实体", "up", "推理步骤引用的实体"),
    CrossLayerRule(1, 3, "证据由决策采信", "down", "实体证据被某决策采信"),
)

# 边类型 → 默认证据类别。写入时若未显式指定则按此推断。
DEFAULT_EVIDENCE_CLASS: dict[str, str] = {
    "受约束于": EVIDENCE_DERIVED,
    "适用于": EVIDENCE_DERIVED,
    "映射维度": EVIDENCE_DERIVED,
    "对应指标": EVIDENCE_DERIVED,
    "设定阈值": EVIDENCE_DIRECT,
    "细化": EVIDENCE_DIRECT,
    "冲突于": EVIDENCE_DERIVED,
    "决策依据条款": EVIDENCE_DERIVED,
    "决策涉及实体": EVIDENCE_DIRECT,
    "步骤引用条款": EVIDENCE_DERIVED,
    "步骤引用实体": EVIDENCE_DIRECT,
    "触发风险信号": EVIDENCE_DERIVED,
}

DEFAULT_EDGE_CONFIDENCE: dict[str, float] = {
    "法定代表人": 1.0,
    "实际控制人": 0.95,
    "控股": 0.95,
    "担保": 1.0,
    "缴纳社保": 1.0,
    "受到处罚": 1.0,
    "涉及诉讼": 1.0,
    "拥有工商登记": 1.0,
    "受约束于": 0.85,
    "映射维度": 0.8,
    "触发风险信号": 0.7,
}


def is_cross_layer(src_layer: int, dst_layer: int) -> bool:
    return src_layer != dst_layer and {src_layer, dst_layer} != {LAYER_META}


def cross_layer_rule_for(src_layer: int, dst_layer: int, etype: str) -> CrossLayerRule | None:
    for rule in CROSS_LAYER_RULES:
        if rule.src_layer == src_layer and rule.dst_layer == dst_layer and rule.etype == etype:
            return rule
    return None
