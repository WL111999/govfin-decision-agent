"""约束路径搜索：在带约束的图上做有界 DFS，并对解出的路径做置信度传播。

用 DFS 而不是 BFS：决策场景关心的是"有没有一条满足约束的推理链"，而不是
"最短的那条链"。BFS 会优先返回跳数最少但业务上无意义的路径（企业—法定代表人—
自然人，两跳就到底了），而真正能用的是"企业—社保记录—风险指标—风险维度—条款"
这条更长的链。DFS 配合显式的端点类型与必需边类型约束，才能把有价值的链逼出来。

三个硬性设计：

1. **环路禁止是默认的**。带环的路径在置信度传播里会把 hop 因子乘成 0，
   却仍在结果里占位置、拖慢搜索。真需要环路时显式开 ``allow_cycles``。
2. **展开预算触顶时如实上报**，不抛异常也不静默截断。智能体拿到
   ``truncated=True`` 才知道该收紧约束重来，而不是把残缺结果当成全集。
3. **路径自带节点快照**。决策一旦作出，这条链上的实体数据、条款原文、证据片段
   就被固化进 Layer3 运行时图。事后图上的社保记录被更新了，也不影响
   已经在案的决策依据——这是"可追溯"和"事后编故事"的分界线。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from govfin.graph.confidence import ConfidenceModel, PathConfidence
from govfin.graph.schema import EVIDENCE_DERIVED
from govfin.graph.store import GraphStore
from govfin.reasoning.constraints import PathConstraint

MAX_RESULTS = 60  # 单次搜索返回的路径上限，防止调用方被路径洪水淹没


@dataclass
class PathRecord:
    """一条解出的推理链，自包含到可以脱离图独立审计。"""

    path_id: str
    constraint: str
    node_ids: list[str]
    edges: list[dict]
    node_snapshots: list[dict]
    confidence: PathConfidence
    layers_visited: list[int] = field(default_factory=list)
    cross_layer_count: int = 0

    def to_dict(self, *, full_snapshots: bool = True) -> dict:
        return {
            "path_id": self.path_id,
            "constraint": self.constraint,
            "hops": len(self.edges),
            "node_ids": self.node_ids,
            "layers_visited": self.layers_visited,
            "cross_layer_count": self.cross_layer_count,
            "confidence": self.confidence.to_dict(),
            "edges": [
                {
                    "id": e["id"],
                    "etype": e["etype"],
                    "src": e["src"],
                    "dst": e["dst"],
                    "layer": e.get("layer"),
                    "cross_layer": bool(e.get("cross_layer")),
                    "confidence": e.get("confidence"),
                    "evidence_class": e.get("evidence_class"),
                    "evidence": e.get("evidence") or {},
                }
                for e in self.edges
            ],
            "nodes": (self.node_snapshots if full_snapshots else [
                {"id": n["id"], "ntype": n["ntype"], "label": n["label"]}
                for n in self.node_snapshots
            ]),
        }

    def summary(self) -> str:
        chain = " → ".join(
            f"{n['label']}({n['ntype']})" for n in self.node_snapshots
        )
        return f"[{self.constraint}] {chain}  置信度 {self.confidence.score:.4f}"


@dataclass
class PathSearchResult:
    constraint: str
    start_node: str
    paths: list[PathRecord] = field(default_factory=list)
    explored_edges: int = 0
    truncated: bool = False
    truncated_reason: str = ""
    rejected_paths: list[dict] = field(default_factory=list)

    @property
    def best(self) -> PathRecord | None:
        return self.paths[0] if self.paths else None

    def to_dict(self, *, include_rejected: bool = True) -> dict:
        out = {
            "constraint": self.constraint,
            "start_node": self.start_node,
            "path_count": len(self.paths),
            "explored_edges": self.explored_edges,
            "truncated": self.truncated,
            "truncated_reason": self.truncated_reason,
            "accepted_path_count": sum(1 for p in self.paths if p.confidence.accepted),
            "paths": [p.to_dict() for p in self.paths],
        }
        if include_rejected:
            out["rejected_path_count"] = len(self.rejected_paths)
            out["rejected_paths"] = self.rejected_paths[:20]
        return out


class PathEngine:
    def __init__(
        self,
        store: GraphStore,
        *,
        confidence: ConfidenceModel | None = None,
        max_results: int = MAX_RESULTS,
    ) -> None:
        self.store = store
        self.confidence = confidence or ConfidenceModel()
        self.max_results = max_results
        self._clarity_generation = -1
        self._clarity_cache: dict[str, float] = {}

    @property
    def _rule_clarity(self) -> dict[str, float]:
        """派生边 ID → 所援引条款的「条款明确程度」。按写代数惰性重建。

        这里**不能**在 ``__init__`` 里算。引擎由 ``AgentRuntime`` 在启动时构造，
        那一刻图还是空的（数据是构造之后才导进去的），索引会永久停在空集上；
        之后每一次查询都失去条款明确程度加权，派生边的惩罚变轻，长链的分数被
        系统性高估，「措辞越模糊、推导越不可靠」这条设计整个失效。失效方式是
        无声的：分数照算，只是算错了。同理，本体演化后新增的条款也必须能被纳入。
        """
        generation = self.store.write_generation()
        if generation != self._clarity_generation:
            self._clarity_cache = self._index_rule_clarity()
            self._clarity_generation = generation
        return self._clarity_cache

    def _index_rule_clarity(self) -> dict[str, float]:
        """索引必须**按边 ID** 建。

        条款明确程度挂在条款节点上，而查表发生在 ``ConfidenceModel.path_confidence``
        里——那里手上只有边 ID 和边自带的证据，没有节点。早期版本按节点 ID 建索引，
        于是每次查表都 miss。这里沿边的 ``evidence.source_document`` 把两边接起来。
        """
        clause_clarity: dict[str, float] = {}
        for node in self.store.query_nodes(layer=2, limit=None):
            value = node["props"].get("条款明确程度")
            if not isinstance(value, (int, float)):
                continue
            clause_clarity[node["id"]] = float(value)
            number = node["props"].get("条款编号")
            if number:
                clause_clarity[str(number)] = float(value)
        if not clause_clarity:
            return {}

        out: dict[str, float] = {}
        for edge in self.store.all_edges():
            if edge.get("evidence_class") != EVIDENCE_DERIVED:
                continue
            source = (edge.get("evidence") or {}).get("source_document")
            if source is None:
                continue
            clarity = clause_clarity.get(str(source))
            if clarity is not None:
                out[edge["id"]] = clarity
        return out

    # ------------------------------------------------------------------

    def search(self, start_node: str, constraint: PathConstraint) -> PathSearchResult:
        result = PathSearchResult(constraint=constraint.name, start_node=start_node)
        start = self.store.get_node(start_node)
        if start is None:
            result.truncated_reason = f"起点节点不存在: {start_node}"
            return result
        if constraint.start_types and not self._type_ok(start["ntype"], constraint.start_types):
            result.truncated_reason = (
                f"起点类型 '{start['ntype']}' 不在约束允许的起点类型内"
            )
            return result

        feedback = self.store.all_edge_feedback()
        budget = constraint.expansion_budget
        # 栈元素：(当前节点, 已走节点, 已走边)
        stack: list[tuple[str, list[str], list[dict]]] = [(start_node, [start_node], [])]
        found: list[tuple[list[str], list[dict]]] = []

        while stack:
            current, node_path, edge_path = stack.pop()
            if len(edge_path) >= constraint.max_hops:
                continue
            for edge in self._step_edges(current, constraint):
                if result.explored_edges >= budget:
                    result.truncated = True
                    result.truncated_reason = (
                        f"展开边数达到预算 {budget}，结果可能不完整；"
                        "请收紧 allowed_edge_types 或降低 max_hops 后重试"
                    )
                    stack.clear()
                    break
                result.explored_edges += 1

                nxt = edge["dst"] if edge["src"] == current else edge["src"]
                if not constraint.allow_cycles and nxt in node_path:
                    continue

                new_nodes = node_path + [nxt]
                new_edges = edge_path + [edge]
                if self._is_complete(new_edges, new_nodes, constraint, start):
                    found.append((new_nodes, new_edges))
                    continue
                stack.append((nxt, new_nodes, new_edges))

        records: list[PathRecord] = []
        for node_path, edge_path in found:
            pc = self.confidence.path_confidence(
                edge_path,
                rule_clarity=self._rule_clarity,
                feedback=feedback,
                threshold=constraint.accept_threshold,
            )
            record = PathRecord(
                path_id="",
                constraint=constraint.name,
                node_ids=node_path,
                edges=edge_path,
                node_snapshots=[self._snapshot(nid) for nid in node_path],
                confidence=pc,
                layers_visited=sorted(
                    {
                        n["layer"]
                        for n in (self._snapshot(nid) for nid in node_path)
                        if n.get("layer") is not None
                    }
                ),
                cross_layer_count=sum(1 for e in edge_path if e.get("cross_layer")),
            )
            record.path_id = _path_id(constraint.name, edge_path)
            records.append(record)

        # 低置信度路径**保留**在 rejected_paths 里而不是丢弃：
        # 审计时要能回答"为什么最后没采纳那条看起来相关的链"。
        accepted = [r for r in records if r.confidence.accepted]
        rejected = [r for r in records if not r.confidence.accepted]
        accepted.sort(key=lambda r: -r.confidence.score)
        rejected.sort(key=lambda r: -r.confidence.score)

        result.paths = accepted[: self.max_results]
        result.rejected_paths = [
            {
                "path_id": r.path_id,
                "chain": " → ".join(f"{n['label']}" for n in r.node_snapshots),
                "confidence": round(r.confidence.score, 6),
                "reject_reason": r.confidence.reject_reason,
            }
            for r in rejected[:50]
        ]
        return result

    def search_many(self, start_nodes: list[str], constraint: PathConstraint) -> PathSearchResult:
        """多个起点合并搜索：同一主体的多条入口（不同记录类型）应当合起来看。"""
        merged = PathSearchResult(constraint=constraint.name, start_node=",".join(start_nodes[:5]))
        for node_id in start_nodes:
            sub = self.search(node_id, constraint)
            merged.paths.extend(sub.paths)
            merged.rejected_paths.extend(sub.rejected_paths)
            merged.explored_edges += sub.explored_edges
            merged.truncated = merged.truncated or sub.truncated
            if sub.truncated and not merged.truncated_reason:
                merged.truncated_reason = sub.truncated_reason
        merged.paths.sort(key=lambda r: -r.confidence.score)
        merged.paths = merged.paths[: self.max_results]
        return merged

    # ------------------------------------------------------------------

    def _step_edges(self, node_id: str, constraint: PathConstraint) -> list[dict]:
        edges = self.store.neighbors(
            node_id,
            direction="both",
            etypes=constraint.allowed_edge_types,
            min_confidence=constraint.min_edge_confidence,
        )
        out: list[dict] = []
        for edge in edges:
            if not constraint.allows_edge_type(edge["etype"]):
                continue
            if not constraint.allows_evidence(edge.get("evidence_class", "direct")):
                continue
            out.append(edge)
        # 按置信度降序展开，保证预算吃紧时优先拿到高置信度的链
        out.sort(key=lambda e: -float(e.get("confidence") or 0.0))
        return out

    def _is_complete(
        self,
        edge_path: list[dict],
        node_path: list[str],
        constraint: PathConstraint,
        start_node: dict,
    ) -> bool:
        if not edge_path:
            return False
        if constraint.required_layers:
            # 图层覆盖按**节点**统计，不按边统计。跨层边在存储上只归属一个图层
            # （源节点的层），按边统计会让"必须跨到 Layer1"这类约束永远判不成立，
            # 结果是一条合法的跨域推理链被静默丢弃。
            if not set(constraint.required_layers).issubset(self._node_layers(node_path, start_node)):
                return False
        if constraint.required_edge_types:
            present = {e["etype"] for e in edge_path}
            if not set(constraint.required_edge_types).issubset(present):
                return False

        end = self.store.get_node(node_path[-1])
        if end is None:
            return False
        if constraint.end_types and not self._type_ok(end["ntype"], constraint.end_types):
            return False
        if constraint.end_layers and end.get("layer") not in constraint.end_layers:
            return False
        return True

    def _node_layers(self, node_path: list[str], start_node: dict) -> set[int]:
        layers = {
            layer
            for layer in (start_node.get("layer"),)
            if layer is not None
        }
        for node_id in node_path:
            node = self.store.get_node(node_id)
            if node is not None and node.get("layer") is not None:
                layers.add(node["layer"])
        return layers

    def _type_ok(self, ntype: str, allowed: tuple[str, ...]) -> bool:
        for target in allowed:
            if self.store.ontology.is_subclass_of(ntype, target):
                return True
        return False

    def _snapshot(self, node_id: str) -> dict:
        node = self.store.get_node(node_id)
        if node is None:
            return {"id": node_id, "ntype": "?", "label": "?", "props": {}}
        return {
            "id": node["id"],
            "ntype": node["ntype"],
            "label": node["label"],
            "layer": node.get("layer"),
            "props": node.get("props") or {},
        }


def _path_id(constraint_name: str, edges: list[dict]) -> str:
    import hashlib

    payload = constraint_name + "|" + "|".join(e["id"] for e in edges)
    return "path:" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


__all__ = ["PathEngine", "PathRecord", "PathSearchResult", "MAX_RESULTS"]
