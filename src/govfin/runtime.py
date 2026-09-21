"""Agent 运行时：把各层装配成一个可被 MCP / A2A / CLI 共享的实例。

三种入口（命令行、MCP server、A2A server）需要的是同一套装配：本体、图存储、
抽取器、绑定器、路径引擎、决策合成器、溯源记录器、审计器。装配逻辑散在三处
就会漂移——某天有人给决策合成器加了个参数，只改了 MCP 那条路径，A2A 那条就
静默用不上。因此集中在这里装配一次。

同时这里是**并发安全边界**。MCP 的 stdio server 会并发处理工具调用，而
SQLite 连接、LLM 熔断器、图存储的邻接缓存都不是可重入的。这里给每个工具调用
套一把可重入锁，把并发压成串行：图谱规模在这个量级（万级节点）下，串行的
吞吐远高于为并发而引入的锁粒度设计与竞态排查成本。真要上并发，正确的做法是
按需复制只读的图快照，而不是在这里加细粒度锁。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from govfin.config import get_settings
from govfin.evolution.pipeline import OntologyEvolutionPipeline
from govfin.graph.binder import RuleBinder
from govfin.graph.store import GraphStore
from govfin.ingest.extractor import Extractor
from govfin.ingest.loader import GraphLoader
from govfin.ontology.model import Ontology
from govfin.ontology.seed import build_seed_ontology
from govfin.reasoning.audit import AuditTrail
from govfin.reasoning.decision import DecisionSynthesizer
from govfin.reasoning.path import PathEngine
from govfin.reasoning.provenance import ProvenanceRecorder


@dataclass
class RuntimeStats:
    nodes: int = 0
    edges: int = 0
    ontology_version: str = ""
    layers: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "nodes": self.nodes,
            "edges": self.edges,
            "ontology_version": self.ontology_version,
            "layers": self.layers,
        }


class AgentRuntime:
    """一次装配、多处复用。所有对外工具都从 ``self`` 上取依赖。"""

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        in_memory: bool = False,
        ontology: Ontology | None = None,
        use_llm: bool = False,
    ) -> None:
        self.lock = threading.RLock()
        self.ontology = ontology or build_seed_ontology()
        settings = get_settings()
        if in_memory:
            self.store = GraphStore(in_memory=True, ontology=self.ontology)
        else:
            path = Path(db_path or settings.graph.db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.store = GraphStore(db_path=str(path), ontology=self.ontology)

        self.loader = GraphLoader(self.store, self.ontology)
        self.extractor = Extractor(self.ontology, use_llm=use_llm)
        self.binder = RuleBinder(self.store, self.ontology)
        self.engine = PathEngine(self.store)
        self.synthesizer = DecisionSynthesizer(self.store, engine=self.engine)
        self.recorder = ProvenanceRecorder(self.store)
        self.trail = AuditTrail(self.store)
        self._pipeline: OntologyEvolutionPipeline | None = None
        self._use_llm = use_llm

    # ------------------------------------------------------------------

    @property
    def pipeline(self) -> OntologyEvolutionPipeline:
        """本体演化管道按需构建——它会尝试建 LLM 客户端，不该在只读查询时发生。"""
        if self._pipeline is None:
            from govfin.llm.factory import build_client

            client = None
            if self._use_llm:
                try:
                    client = build_client()
                except Exception:  # noqa: BLE001 - 无 key 时退化为符号路径
                    client = None
            self._pipeline = OntologyEvolutionPipeline(
                self.store,
                ontology=self.ontology,
                client=client,
                use_llm=client is not None,
            )
        return self._pipeline

    def ingest_dir(self, directory: str | Path | None = None) -> dict:
        """把一个目录下的多模态文档全量导入，并做实体归并与规则绑定。"""
        from govfin.ingest.parsers import parse

        root = Path(directory or get_settings().data_dir)
        files = sorted(
            p
            for p in list(root.rglob("*.json")) + list(root.rglob("*.csv"))
            + list(root.rglob("*.txt")) + list(root.rglob("*.pdf")) + list(root.rglob("*.png"))
            if not p.name.endswith(".ocr.json")
        )
        totals = {"documents": 0, "nodes_created": 0, "edges_created": 0, "tuples_routed_to_unk": 0}
        for path in files:
            try:
                result = self.extractor.extract(parse(str(path)))
            except Exception:  # noqa: BLE001 - 单份文档解析失败不应中断整批导入
                continue
            report = self.loader.load(result)
            totals["documents"] += 1
            totals["nodes_created"] += report.nodes_created
            totals["edges_created"] += report.edges_created
            totals["tuples_routed_to_unk"] += report.tuples_routed_to_unk
        consolidation = self.loader.consolidate()
        binding = self.binder.bind()
        totals["entities_merged"] = consolidation.merged_count
        totals["binding"] = binding.to_dict() if hasattr(binding, "to_dict") else {}
        return totals

    def stats(self) -> RuntimeStats:
        raw = self.store.stats()
        return RuntimeStats(
            nodes=raw.get("nodes", 0),
            edges=raw.get("edges", 0),
            ontology_version=self.ontology.version,
            layers=raw.get("by_layer", {}),
        )

    def close(self) -> None:
        self.store.close()


__all__ = ["AgentRuntime", "RuntimeStats"]
