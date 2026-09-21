"""低资源域本体半自动演化。

管道：``UNK 储备池 → 语义聚类 → 符号对齐 → LLM 提案 → 一致性验证 → 仲裁 → 提交 → 图重索引``。
每一环都可以单独调用，方便测试与手工介入。
"""

from govfin.evolution.aligner import AlignmentResult, SymbolicAligner
from govfin.evolution.clustering import UnkCluster, UnkClusterer
from govfin.evolution.pipeline import (
    AUTO_APPROVE_THRESHOLD,
    EvolutionReport,
    OntologyEvolutionPipeline,
    VerificationResult,
    load_ontology,
)
from govfin.evolution.proposer import EditProposal, EditProposer
from govfin.evolution.unk_pool import KIND_ENTITY, KIND_RELATION, UnkCandidate, UnkPool

__all__ = [
    "AlignmentResult",
    "SymbolicAligner",
    "UnkCluster",
    "UnkClusterer",
    "UnkPool",
    "UnkCandidate",
    "KIND_ENTITY",
    "KIND_RELATION",
    "EditProposal",
    "EditProposer",
    "OntologyEvolutionPipeline",
    "EvolutionReport",
    "VerificationResult",
    "AUTO_APPROVE_THRESHOLD",
    "load_ontology",
]
