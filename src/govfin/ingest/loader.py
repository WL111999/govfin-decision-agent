"""Graph loader：五元组 → 三层图。

这里是把"认知"变成"资产"的一步，有两条硬规则：

1. **UNK 不进图**。任何类型未对齐到本体的元组一律送去储备池。图里出现
   ``UNK-ENTITY`` 节点会让后续所有类型约束、SHACL 校验、跨层边规则失效。
2. **证据不可丢**。每条边都必须携带 ``evidence``（片段 + 模态 + 源文档 + 定位）。
   决策溯源之所以能回溯到"条款原文和实体数据快照"，靠的就是这一步没偷懒。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from govfin.graph.schema import EVIDENCE_DIRECT, LAYER_ENTITY
from govfin.graph.store import GraphStore
from govfin.ingest.multimodal import ExtractionResult, ModalityTuple
from govfin.ontology.model import Ontology

# 强标识符：业务上就是唯一键的那些属性，实体归并只认它们
_IDENTITY_KEYS = ("统一社会信用代码", "身份证号")


def _source_of(tup: ModalityTuple) -> dict:
    """这条断言是从哪儿读来的。节点上的每个字段都要能回答这个问题。"""
    return {"document": tup.source_document, "locator": tup.source_locator}


@dataclass
class LoadReport:
    nodes_created: int = 0
    nodes_merged: int = 0
    edges_created: int = 0
    tuples_routed_to_unk: int = 0
    rejected: list[dict] = field(default_factory=list)
    by_type: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "nodes_created": self.nodes_created,
            "nodes_merged": self.nodes_merged,
            "edges_created": self.edges_created,
            "tuples_routed_to_unk": self.tuples_routed_to_unk,
            "by_type": self.by_type,
            "rejected_count": len(self.rejected),
            "rejected_sample": self.rejected[:20],
        }


@dataclass
class ConsolidationReport:
    """实体归并结果。合并了哪些节点、依据哪个标识、并过来多少条边，都要留痕。"""

    merged: list[dict] = field(default_factory=list)
    edges_rerouted: int = 0
    edges_dropped: int = 0
    conflicts: list[dict] = field(default_factory=list)

    @property
    def merged_count(self) -> int:
        return len(self.merged)

    def to_dict(self) -> dict:
        return {
            "merged_count": self.merged_count,
            "edges_rerouted": self.edges_rerouted,
            "edges_dropped": self.edges_dropped,
            "conflict_count": len(self.conflicts),
            "merged": self.merged,
            "conflicts": self.conflicts[:20],
        }


class GraphLoader:
    def __init__(self, store: GraphStore, ontology: Ontology | None = None) -> None:
        self.store = store
        self.ontology = ontology or store.ontology
        self._node_cache: dict[str, str] = {}

    def unk_tuples(self, result: ExtractionResult) -> list[ModalityTuple]:
        return [t for t in result.tuples if t.is_unk] + list(result.unk_entities) + list(result.unk_relations)

    # ------------------------------------------------------------------

    def consolidate(
        self, *, keys: tuple[str, ...] = _IDENTITY_KEYS, dry_run: bool = False
    ) -> ConsolidationReport:
        """按强标识符做实体归并，把"同一实体的多个化身"收敛成一个节点。

        为什么必须有这一步：一份文档里同一个主体往往既以名称出现、又以标识符出现。
        "甲科技有限公司 2025 年度财务报表 …… 统一社会信用代码：91310115MA1K3XYA01"
        这句话会抽出两个提及，而 ``_ensure_node`` 只按**提及文本**去重——两次调用
        的 mention 一个是名称、一个是信用代码，谁也匹配不上谁，于是图上出现两个
        企业节点，各自拿着一半的事实。后果不只是图难看：真实性核验从"信用代码
        节点"出发时会因为查不到任何登记记录而给出"不予受理"，而该企业其实登记
        齐全。名称节点与标识符节点必须合成一个。

        归并依据是**强标识符**（统一社会信用代码、身份证号），不是名称相似度。
        名称相似度归并会误伤"XX 分公司"这类真实存在的不同主体，而强标识符在
        业务上就是唯一键。冲突（同一信用代码下两个不同的名称）不静默覆盖，
        记进 ``conflicts`` 交由人工核对——它通常意味着上游数据本身有问题。
        """
        report = ConsolidationReport()
        for key in keys:
            groups: dict[str, list[dict]] = {}
            for node in self.store.query_nodes(limit=None):
                value = node.get("props", {}).get(key)
                if value in (None, "", []):
                    continue
                groups.setdefault(str(value), []).append(node)

            for value, members in groups.items():
                if len(members) < 2:
                    continue
                survivor = max(members, key=_survivor_rank)
                for loser in members:
                    if loser["id"] == survivor["id"]:
                        continue
                    self._merge_into(survivor, loser, key, value, report, dry_run=dry_run)

        if not dry_run:
            self._node_cache.clear()
        return report

    def _merge_into(
        self,
        survivor: dict,
        loser: dict,
        key: str,
        value: str,
        report: ConsolidationReport,
        *,
        dry_run: bool,
    ) -> None:
        merged_props: dict = {}
        for prop, val in loser["props"].items():
            if prop == "id":
                continue
            current = survivor["props"].get(prop)
            if current is None:
                merged_props[prop] = val
            elif current != val:
                # 同一个主体、同一个属性、两个不同的值：上游数据自相矛盾，
                # 保留存活节点上的值并如实上报，不替业务方做选择。
                report.conflicts.append(
                    {"key": key, "value": value, "prop": prop, "kept": current, "dropped": val}
                )
        report.merged.append(
            {
                "survivor": survivor["id"],
                "survivor_label": survivor["label"],
                "absorbed": loser["id"],
                "absorbed_label": loser["label"],
                "key": key,
                "value": value,
                "props_absorbed": sorted(merged_props),
            }
        )
        if dry_run:
            return

        if merged_props:
            self.store.update_node_props(survivor["id"], merged_props)

        for edge in self.store.find_edges(src=loser["id"]) + self.store.find_edges(dst=loser["id"]):
            new_src = survivor["id"] if edge["src"] == loser["id"] else edge["src"]
            new_dst = survivor["id"] if edge["dst"] == loser["id"] else edge["dst"]
            self.store.delete_edge(edge["id"])
            if new_src == new_dst:
                report.edges_dropped += 1  # 归并后自环：该边表达的关系已无意义
                continue
            self.store.add_edge(
                new_src,
                new_dst,
                edge["etype"],
                props=edge.get("props"),
                evidence=edge.get("evidence"),
                confidence=edge.get("confidence"),
                evidence_class=edge.get("evidence_class"),
                validate=False,
            )
            report.edges_rerouted += 1
        self.store.delete_node(loser["id"])
        survivor["props"] = {**merged_props, **survivor["props"]}

    def load(self, result: ExtractionResult) -> LoadReport:
        report = LoadReport()
        # 抽取阶段就已经判死的值要在报告里露面。否则报告说"零条被拒"，而实际
        # 有些字段根本没进图——"全部导入成功"和"少了一个字段"在报告上长得一模
        # 一样，没人会去查。
        report.rejected.extend(result.rejected)
        for tup in result.tuples:
            if tup.is_unk:
                report.tuples_routed_to_unk += 1
                continue
            try:
                self._load_tuple(tup, report)
            except Exception as exc:  # noqa: BLE001 - 单条元组失败不应中断整批导入
                report.rejected.append(
                    {
                        "mention": tup.entity_mention,
                        "type": tup.entity_type,
                        "relation": tup.relation_type,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        report.tuples_routed_to_unk += len(result.unk_entities) + len(result.unk_relations)
        return report

    # ------------------------------------------------------------------

    def _load_tuple(self, tup: ModalityTuple, report: LoadReport) -> None:
        cls = self.ontology.get_class(tup.entity_type)
        if cls is None:
            report.tuples_routed_to_unk += 1
            return

        props = dict(tup.attributes)
        props.setdefault("id", tup.entity_mention)
        node_id = self._ensure_node(tup.entity_type, tup.entity_mention, props, tup, report)

        # 数据属性（"注册资本"、"条款原文"…）写回实体自身节点。
        # 必须先于关系边处理且不受 relation_type 是否为 None 影响——
        # 否则"条款原文"这类只有属性、没有关系的元组会被静默丢弃，
        # Layer2 规则图就只剩条款编号，推理时拿不到条文依据。
        if tup.attributes:
            self._apply_datatype_attrs(node_id, tup)

        owner_id: str | None = None
        if tup.relation_candidate and tup.relation_candidate != tup.entity_mention:
            owner_type = self._owner_type(tup)
            if owner_type is None:
                report.tuples_routed_to_unk += 1
                return
            owner_id = self._ensure_node(
                owner_type, tup.relation_candidate, {"id": tup.relation_candidate}, tup, report
            )

        if tup.relation_type is None:
            return

        prop = self.ontology.properties.get(tup.relation_type)
        if prop is None or prop.kind != "object":
            if prop is not None and prop.kind == "datatype" and tup.relation_target:
                _, _, raw = tup.relation_target.partition("=")
                if raw:
                    coerced = _coerce_datatype(raw, prop.rdfs_range)
                    if coerced is not None:
                        self.store.update_node_props(node_id, {prop.name: coerced})
            return

        src_id, dst_id = self._orient(owner_id or node_id, node_id, prop)
        if src_id is None or dst_id is None:
            report.tuples_routed_to_unk += 1
            return

        self.store.add_edge(
            src_id,
            dst_id,
            tup.relation_type,
            props={},
            evidence={
                "snippet": tup.evidence_snippet,
                "modality": tup.modality,
                "source_document": tup.source_document,
                "source_locator": tup.source_locator,
                "confidence": tup.confidence,
                "mention": tup.entity_mention,
            },
            confidence=tup.confidence,
            evidence_class=tup.evidence_class or EVIDENCE_DIRECT,
            validate=False,
        )
        report.edges_created += 1

    def _apply_datatype_attrs(self, node_id: str, tup: ModalityTuple) -> None:
        """把元组里的数据属性写回实体节点。

        走 ``merge_from_source`` 而不是 ``update_node_props``，因为这两个 API 的
        区别就是这一步的要害：``update_node_props`` 是"系统自己决定把字段改成
        什么"，而这些值**来自一份来源文档**，系统并不知道它对不对。语义差别只在
        冲突时才显形，而冲突恰恰是这里唯一要紧的事——已有的社保记录写着"欠缴"，
        后到的一份记录说同一条记录是"正常缴纳"。若让后到的说了算，一份伪造的
        重发记录就足以洗白一条风险信号，而且洗白之后图上再也看不出发生过什么：
        节点的 ``first_seen`` 还指着最初那份文件，审计追到这里会以为这就是原文。
        保留先到的值、把两个说法连同各自的来源都记进 ``__冲突__``，才是多源数据
        在政务场景里唯一站得住的默认。
        """
        patch: dict = {}
        for name, value in tup.attributes.items():
            if name == "id" or value in (None, ""):
                continue
            prop = self.ontology.properties.get(name)
            if prop is None or prop.kind != "datatype":
                continue
            # 抽取器可能已按语义归一（如"70%"→0.7），这里只做类型兜底，
            # 避免把已经是 float 的 5000.0 再解析一次
            if isinstance(value, (int, float, bool)):
                patch[name] = value
                continue
            coerced = _coerce_datatype(str(value), prop.rdfs_range)
            if coerced is not None:
                patch[name] = coerced
        if patch:
            self.store.merge_from_source(node_id, patch, source=_source_of(tup))

    def _orient(self, candidate: str, entity: str, prop) -> tuple[str | None, str | None]:
        """按属性的 domain/range 决定边的方向。

        抽取器只知道"这两个实体有关系"，不知道哪个是头。让 loader 用本体定义
        来定向，避免在抽取器里为每条关系写方向判断分支。
        """
        candidate_node = self.store.get_node(candidate)
        entity_node = self.store.get_node(entity)
        if candidate_node is None or entity_node is None:
            return None, None

        cand_ok_src = self.ontology.is_subclass_of(candidate_node["ntype"], prop.domain)
        cand_ok_dst = prop.kind == "object" and self.ontology.is_subclass_of(candidate_node["ntype"], prop.range)
        ent_ok_src = self.ontology.is_subclass_of(entity_node["ntype"], prop.domain)
        ent_ok_dst = prop.kind == "object" and self.ontology.is_subclass_of(entity_node["ntype"], prop.range)

        if cand_ok_src and ent_ok_dst:
            return candidate, entity
        if ent_ok_src and cand_ok_dst:
            return entity, candidate
        return None, None

    def _owner_type(self, tup: ModalityTuple) -> str | None:
        """关系主体的类型。优先按关系定义反推，其次按 mention 形态猜。"""
        if tup.relation_type:
            prop = self.ontology.properties.get(tup.relation_type)
            if prop is not None:
                domain_cls = self.ontology.get_class(prop.domain)
                if domain_cls is not None:
                    return domain_cls.name
        from govfin.ingest.extractor import _guess_type_by_shape

        return _guess_type_by_shape(tup.relation_candidate or "", self.ontology) or "企业"

    def _ensure_node(
        self, ntype: str, mention: str, props: dict, tup: ModalityTuple, report: LoadReport
    ) -> str:
        cache_key = f"{ntype}::{mention}"
        cached = self._node_cache.get(cache_key)
        if cached and self.store.get_node(cached) is not None:
            # 缓存只该省掉"这个提及指向哪个节点"的查找，不能把**合并**一并省掉。
            # 抽取器是"一个字段一个元组"，同一个提及在一份文档里要走十几次，
            # 因此缓存命中才是常态而不是例外——在这里直接返回，等于让同一批里
            # 后到的每一个字段值都绕过来源归属与冲突留痕，直接改写节点。
            self.store.merge_from_source(cached, props, source=_source_of(tup))
            return cached

        existing = self._find_existing(ntype, mention)
        if existing is None and _is_identity_mention(mention):
            # 标识符形态的提及（条款编号、记录编号、信用代码）在图上必须唯一。
            # 抽取器给的是最具体的类型（"授信指引条款"），而关系定义里的 domain
            # 是上位类型（"监管条款"）——不跨类型复用就会同号两节点，Layer2 断链。
            existing = self._find_existing_across_types(mention)
        provenance = {
            "first_seen": {
                "document": tup.source_document,
                "locator": tup.source_locator,
                "modality": tup.modality,
                "snippet": tup.evidence_snippet,
            },
            "modality": tup.modality,
        }
        if existing is not None:
            # 带上来源：合并后的节点属性可能来自多份文档，节点上必须能回答
            # "这个字段是谁给的"。同字段取值冲突由存储层留痕（保留先到的值）。
            self.store.merge_from_source(existing["id"], props, source=_source_of(tup))
            self._node_cache[cache_key] = existing["id"]
            report.nodes_merged += 1
            return existing["id"]

        payload = dict(props)
        node_id = None
        try:
            node_id = self.store.add_node(
                ntype, label=mention, props=payload, provenance=provenance, validate=False
            )
        except Exception:
            # SHACL 校验失败时降级为不带属性的裸节点：宁可少一个字段，
            # 也不要因为一个缺字段的节点让整份文档的导入失败
            node_id = self.store.add_node(
                ntype, label=mention, props={"id": mention}, provenance=provenance, validate=False
            )
        self._node_cache[cache_key] = node_id
        report.nodes_created += 1
        report.by_type[ntype] = report.by_type.get(ntype, 0) + 1
        return node_id

    _IDENTITY_PROPS = (
        "统一社会信用代码",
        "记录编号",
        "条款编号",
        "决策编号",
        "提案编号",
        "身份证号",
    )

    def _find_existing(self, ntype: str, mention: str) -> dict | None:
        for key in self._IDENTITY_PROPS:
            hits = self.store.find_by_prop(key, mention, ntype=ntype, limit=1)
            if hits:
                return hits[0]
        for hit in self.store.query_nodes(ntype=ntype, label_like=mention, limit=5):
            if hit["label"] == mention:
                return hit
        return None

    def _find_existing_across_types(self, mention: str) -> dict | None:
        for key in self._IDENTITY_PROPS:
            hits = self.store.find_by_prop(key, mention, limit=1)
            if hits:
                return hits[0]
        for hit in self.store.query_nodes(label_like=mention, limit=10):
            if hit["label"] == mention:
                return hit
        return None


def _survivor_rank(node: dict) -> tuple:
    """归并时谁留下：信息更全的留下。

    决策链上引用的是节点 ID，而节点 ID 在建图时由首个提及生成，可能是信用代码。
    因此不单纯按属性数量排——还要看 label 是不是标识符形态：以名称为 label 的
    节点对人类和下游报表都是更可读的载体，平手时让它胜出。
    """
    label_is_identifier = _is_identity_mention(str(node.get("label") or ""))
    return (
        0 if label_is_identifier else 1,
        len(node.get("props") or {}),
    )


_IDENTITY_MENTION = re.compile(
    r"^(?:"
    r"[0-9A-HJ-NPQRTUWXY]{18}"                       # 统一社会信用代码
    r"|(?:SS|GS|CF|SF|SW|JC|TZ)-\d{4}-?\d{2,4}-?\d{0,4}"  # 政务记录编号
    r"|.{0,12}[〔\[(]\s*\d{4}\s*[〕\])]\s*\d+\s*号.*"      # 条款编号
    r")$"
)


def _is_identity_mention(mention: str) -> bool:
    """标识符形态的提及全局唯一，可以跨节点类型复用。人名/企业名则不可以
    ——"张三"同时是股东和法定代表人时不能合并成一个类型。"""
    return bool(_IDENTITY_MENTION.match((mention or "").strip()))


def _coerce_datatype(raw: str, rdfs_range: str | None):
    if rdfs_range == "integer":
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return None
    if rdfs_range == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    if rdfs_range == "boolean":
        if str(raw).strip().lower() in ("true", "1", "是", "yes"):
            return True
        if str(raw).strip().lower() in ("false", "0", "否", "no"):
            return False
        return None
    if rdfs_range in ("string", "date"):
        return str(raw)
    return str(raw)
