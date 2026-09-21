"""认知层：模态无关的中间表征。

核心设计——**所有模态先塌缩到同一个五元组**：

    (实体提及, 关系候选, 证据片段, 模态来源, 置信度)

下游的图构建、路径推理、决策溯源只消费这一层，完全不感知上游是 PDF 表格、
社保 JSON 还是营业执照扫描件。这样做的收益是"每接入一种新模态，只需要写一个
解析器把它们变成五元组"——而不是让新模态的语义渗进推理引擎。

多模态最容易被做成"三个 if-else 分支拼起来的假统一"。这里的判据是：
``ModalityTuple`` 不含任何模态特有字段（没有 pdf_page_range、没有 json_pointer、
没有 image_bbox），模态差异全部被压缩进 ``source_locator`` 这个字符串里。
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator, Sequence

from govfin.graph.schema import EVIDENCE_DIRECT, EVIDENCE_LLM, MODALITIES, MODALITY_TEXT

UNK_ENTITY = "UNK-ENTITY"
UNK_RELATION = "UNK-RELATION"


@dataclass
class Block:
    """一个解析出的文本块或表格块。locator 是模态无关的定位串。"""

    kind: str  # text | table | kv | image_caption
    text: str
    locator: str
    page: int | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ParsedDocument:
    """解析结果：文档元信息 + 块序列。"""

    source: str
    modality: str
    blocks: list[Block] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def text(self, separator: str = "\n") -> str:
        return separator.join(b.text for b in self.blocks if b.text)

    def tables(self) -> list[Block]:
        return [b for b in self.blocks if b.kind == "table"]

    def add(self, kind: str, text: str, *, locator: str, page: int | None = None, **metadata) -> Block:
        block = Block(kind=kind, text=text, locator=locator, page=page, metadata=metadata)
        self.blocks.append(block)
        return block

    def __len__(self) -> int:
        return len(self.blocks)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "modality": self.modality,
            "metadata": self.metadata,
            "warnings": self.warnings,
            "blocks": [b.to_dict() for b in self.blocks],
        }


@dataclass
class ModalityTuple:
    """模态无关五元组。图构建层的唯一输入。"""

    entity_mention: str
    entity_type: str  # 本体内类型名，或 UNK_ENTITY
    relation_candidate: str | None
    relation_target: str | None
    relation_type: str  # 本体内属性名，或 UNK_RELATION
    evidence_snippet: str
    modality: str
    source_document: str
    source_locator: str
    confidence: float
    evidence_class: str = EVIDENCE_DIRECT
    attributes: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @property
    def is_unk_entity(self) -> bool:
        return self.entity_type == UNK_ENTITY

    @property
    def is_unk_relation(self) -> bool:
        return self.relation_type == UNK_RELATION

    @property
    def is_unk(self) -> bool:
        return self.is_unk_entity or self.is_unk_relation

    @property
    def mention_key(self) -> str:
        """UNK 储备池的聚合键。"""
        return f"{self.entity_type}::{self.entity_mention.strip()}"

    @property
    def relation_key(self) -> str | None:
        if self.relation_type is None:
            return None
        return f"{self.relation_type}::{self.relation_candidate}→{self.relation_target}"

    def fingerprint(self) -> str:
        payload = f"{self.entity_mention}|{self.entity_type}|{self.relation_type}|{self.source_document}|{self.source_locator}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExtractionResult:
    """一次抽取的产出：五元组 + 未归类统计。"""

    tuples: list[ModalityTuple] = field(default_factory=list)
    unk_entities: list[ModalityTuple] = field(default_factory=list)
    unk_relations: list[ModalityTuple] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.tuples)

    def __iter__(self) -> Iterator[ModalityTuple]:
        return iter(self.tuples)

    def merge(self, other: "ExtractionResult") -> "ExtractionResult":
        combined = ExtractionResult(
            tuples=self.tuples + other.tuples,
            unk_entities=self.unk_entities + other.unk_entities,
            unk_relations=self.unk_relations + other.unk_relations,
            rejected=self.rejected + other.rejected,
        )
        combined.stats = _merge_stats(self.stats, other.stats)
        return combined

    def to_dict(self) -> dict:
        return {
            "tuple_count": len(self.tuples),
            "unk_entity_count": len(self.unk_entities),
            "unk_relation_count": len(self.unk_relations),
            "rejected_count": len(self.rejected),
            "stats": self.stats,
            "tuples": [t.to_dict() for t in self.tuples[:200]],
        }


def _merge_stats(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, (int, float)) and isinstance(out.get(k), (int, float)):
            out[k] = out[k] + v
        elif k not in out:
            out[k] = v
    return out


def make_tuple(
    *,
    entity_mention: str,
    entity_type: str,
    evidence_snippet: str,
    modality: str,
    source_document: str,
    source_locator: str,
    confidence: float,
    relation_candidate: str | None = None,
    relation_target: str | None = None,
    relation_type: str | None = None,
    evidence_class: str = EVIDENCE_DIRECT,
    attributes: dict | None = None,
    raw: dict | None = None,
) -> ModalityTuple:
    if modality not in MODALITIES:
        modality = MODALITY_TEXT
    return ModalityTuple(
        entity_mention=entity_mention.strip(),
        entity_type=entity_type,
        relation_candidate=relation_candidate,
        relation_target=relation_target,
        relation_type=relation_type,
        evidence_snippet=_snippet(evidence_snippet),
        modality=modality,
        source_document=source_document,
        source_locator=source_locator,
        confidence=max(0.0, min(1.0, confidence)),
        evidence_class=evidence_class,
        attributes=attributes or {},
        raw=raw or {},
    )


def _snippet(text: str, limit: int = 240) -> str:
    """证据片段截断。审计报告里要展示原文，但不能让单个片段撑爆存储。"""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def llm_evidence_class(used_llm: bool) -> str:
    return EVIDENCE_LLM if used_llm else EVIDENCE_DIRECT


def parse_table_rows(text: str, separator: str = "|") -> list[list[str]]:
    """把解析器输出的表格文本还原成行列。多模态解析器的公共出口。"""
    rows: list[list[str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        cells = [c.strip() for c in line.split(separator)]
        rows.append(cells)
    return rows


def iter_cells(rows: Sequence[Sequence[str]]) -> Iterator[tuple[int, int, str]]:
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            if cell:
                yield r, c, cell
