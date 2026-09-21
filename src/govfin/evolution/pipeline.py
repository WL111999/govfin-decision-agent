"""本体演化管道：UNK 储备池 → 聚类 → 符号对齐 → LLM 提案 → 一致性验证 → 仲裁 → 提交 → 图重索引。

整条管道只有两个出口：**拒绝**和**带着证据的版本递增**。没有"先加上去再说"
这一档——本体一旦被污染，后面所有类型约束、跨层边规则、路径搜索都会建立在
错误的结构上，且症状会出现在离病因很远的地方。

验证环节用的是**克隆体上的试运行**而不是静态检查：把候选编辑在一份本体副本上
真的执行一遍，再拿新本体去校验现网已存在的图节点。静态检查只能发现"公理自己
矛盾"，发现不了"这条编辑会让图谱里 37 个已存在的节点不再合法"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from govfin.evolution.aligner import AlignmentResult, SymbolicAligner
from govfin.evolution.clustering import UnkCluster, UnkClusterer
from govfin.evolution.proposer import EditProposal, EditProposer
from govfin.evolution.unk_pool import (
    KIND_ENTITY,
    KIND_RELATION,
    UNK_RELATION,
    UnkCandidate,
    UnkPool,
)
from govfin.graph.store import GraphStore
from govfin.ingest.loader import GraphLoader
from govfin.ingest.multimodal import ExtractionResult, ModalityTuple
from govfin.llm.base import LLMClient
from govfin.ontology.model import Ontology, bump_version
from govfin.ontology.seed import build_seed_ontology

# 自动采纳门槛。高于此置信度且验证通过的提案直接生效，其余进人工仲裁队列。
# 0.6 是刻意的：启发式提案被压到 0.45 以下，因此**没有 LLM 参与时不会有任何
# 编辑自动落地**——低资源域里"没把握就别改本体"应该是一条硬规则。
AUTO_APPROVE_THRESHOLD = 0.6

_STATUS_PENDING = "pending"
_STATUS_APPROVED = "approved"
_STATUS_REJECTED = "rejected"


@dataclass
class VerificationResult:
    safe: bool
    axiom_violations: list[dict] = field(default_factory=list)
    instance_violations: list[dict] = field(default_factory=list)
    disjoint_conflicts: list[dict] = field(default_factory=list)
    replayable_tuples: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "safe": self.safe,
            "axiom_violations": self.axiom_violations[:10],
            "instance_violations": self.instance_violations[:10],
            "disjoint_conflicts": self.disjoint_conflicts[:10],
            "replayable_tuples": self.replayable_tuples,
            "notes": self.notes,
        }


@dataclass
class EvolutionReport:
    cycle: int = 0
    version_before: str = ""
    version_after: str = ""
    pool_size: int = 0
    clustered: int = 0
    alias_learned: int = 0
    classes_added: list[str] = field(default_factory=list)
    properties_added: list[str] = field(default_factory=list)
    auto_approved: list[str] = field(default_factory=list)
    pending_arbitration: list[str] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    replayable_tuples: int = 0
    reindexed_nodes: int = 0
    reindexed_edges: int = 0
    deferred: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "cycle": self.cycle,
            "version_before": self.version_before,
            "version_after": self.version_after,
            "pool_size": self.pool_size,
            "clustered": self.clustered,
            "alias_learned": self.alias_learned,
            "classes_added": self.classes_added,
            "properties_added": self.properties_added,
            "auto_approved": self.auto_approved,
            "pending_arbitration": self.pending_arbitration,
            "rejected": self.rejected[:20],
            "replayable_tuples": self.replayable_tuples,
            "reindexed_nodes": self.reindexed_nodes,
            "reindexed_edges": self.reindexed_edges,
            "deferred": self.deferred,
        }


class OntologyEvolutionPipeline:
    def __init__(
        self,
        store: GraphStore,
        *,
        ontology: Ontology | None = None,
        client: LLMClient | None = None,
        use_llm: bool = True,
        pool: UnkPool | None = None,
        clusterer: UnkClusterer | None = None,
        min_observations: int = 2,
        auto_approve_threshold: float = AUTO_APPROVE_THRESHOLD,
        persist_path: str | Path | None = None,
    ) -> None:
        self.store = store
        self.ontology = ontology or store.ontology
        self.pool = pool or UnkPool(store, self.ontology)
        self.clusterer = clusterer or UnkClusterer()
        self.min_observations = min_observations
        self.auto_approve_threshold = auto_approve_threshold
        self.persist_path = Path(persist_path) if persist_path else None
        self._proposer = EditProposer(self.ontology, client, use_llm=use_llm)
        self._cycle = 0

    # ------------------------------------------------------------------
    # 1. 观测入口
    # ------------------------------------------------------------------

    def observe_from(self, result: ExtractionResult) -> int:
        """把一次抽取里的 UNK 提及灌进储备池。返回新登记的候选数。"""
        seen: set[str] = set()
        for tup in result.tuples:
            if not tup.is_unk:
                continue
            cid = self._observe_tuple(tup)
            if cid:
                seen.add(cid)
        for tup in result.unk_entities:
            cid = self.pool.observe(
                tup.entity_mention,
                KIND_ENTITY,
                source_document=tup.source_document,
                context=tup.evidence_snippet,
                hint_type=self._hint_for(tup),
                tuple_payload=tup.to_dict(),
            )
            if cid:
                seen.add(cid)
        for tup in result.unk_relations:
            cid = self.pool.observe(
                tup.relation_candidate or tup.entity_mention,
                KIND_RELATION,
                source_document=tup.source_document,
                context=tup.evidence_snippet,
                tuple_payload=tup.to_dict(),
            )
            if cid:
                seen.add(cid)
        return len(seen)

    def _observe_tuple(self, tup: ModalityTuple) -> str:
        if tup.is_unk_entity:
            return self.pool.observe(
                tup.entity_mention,
                KIND_ENTITY,
                source_document=tup.source_document,
                context=tup.evidence_snippet,
                hint_type=self._hint_for(tup),
                tuple_payload=tup.to_dict(),
            )
        return self.pool.observe(
            tup.relation_candidate or tup.entity_mention or "",
            KIND_RELATION,
            source_document=tup.source_document,
            context=tup.evidence_snippet,
            tuple_payload=tup.to_dict(),
        )

    def _hint_for(self, tup: ModalityTuple) -> str | None:
        """从"这个未知提及在图中占了谁的位置"反推类型提示。

        比让抽取器猜类型可靠得多：抽取器说"这是 UNK-ENTITY"时，它仍然知道
        这条提及是作为哪个属性的范围出现的。一个总是出现在"拥有工商登记"
        目标位置的未知提及，属于工商登记的可能性显然高于属于自然人的可能性。
        """
        if not tup.relation_type:
            return None
        prop = self.ontology.properties.get(tup.relation_type)
        if prop is None:
            return None
        if prop.kind == "object":
            return prop.range
        return None

    # ------------------------------------------------------------------
    # 2. 一轮演化
    # ------------------------------------------------------------------

    def run_cycle(self, *, commit: bool = True) -> EvolutionReport:
        self._cycle += 1
        report = EvolutionReport(cycle=self._cycle, version_before=self.ontology.version)

        candidates = self.pool.candidates(min_observations=self.min_observations)
        report.pool_size = len(candidates)
        if not candidates:
            report.version_after = self.ontology.version
            report.deferred.append({"reason": "储备池中没有达到观测门槛的候选"})
            return report

        clusters = self.clusterer.cluster(candidates)
        report.clustered = len(clusters)

        aligner = SymbolicAligner(self.ontology)
        proposals: list[EditProposal] = []
        by_candidate: dict[str, UnkCluster] = {}
        for cluster in clusters:
            alignment = aligner.align(cluster)
            proposal = self._proposer.propose(alignment, contexts=cluster.sample_contexts)
            proposals.append(proposal)
            for cid in cluster.member_ids:
                by_candidate[cid] = cluster

        ambiguous = self._find_ambiguity(proposals)

        records: list[dict] = []
        for proposal in proposals:
            cluster = next((c for c in clusters if c.cluster_id == proposal.cluster_id), None)
            verification = self._verify(proposal, cluster)
            status, reason = self._decide(proposal, verification, ambiguous)
            records.append(
                {
                    "proposal": proposal,
                    "cluster": cluster,
                    "verification": verification,
                    "status": status,
                    "status_reason": reason,
                }
            )
            report.replayable_tuples += verification.replayable_tuples
            if status == _STATUS_APPROVED:
                report.auto_approved.append(proposal.canonical)
            elif status == _STATUS_PENDING:
                report.pending_arbitration.append(proposal.canonical)
            else:
                report.rejected.append(
                    {
                        "canonical": proposal.canonical,
                        "reason": reason or proposal.rejected_reason,
                    }
                )

        self._enqueue(records)

        if commit and any(r["status"] == _STATUS_APPROVED for r in records):
            applied = self._commit(records, report)
            report.version_after = applied
            self._reindex(records, report)
        else:
            report.version_after = self.ontology.version

        if self.persist_path is not None:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            self.persist_path.write_text(self.ontology.to_json(), encoding="utf-8")
        return report

    # ------------------------------------------------------------------

    def _find_ambiguity(self, proposals: list[EditProposal]) -> dict[str, str]:
        """同一轮里两条提案抢占同一个类名/属性名 → 判定为歧义，一律转人工。

        这种冲突通常意味着聚类阈值在这一批数据上偏松（两个本该分开的概念
        被并成一类，或反之）。自动挑一个提交会掩盖聚类质量问题，而这正是
        最需要被看见的信号。
        """
        owners: dict[str, str] = {}
        ambiguous: dict[str, str] = {}
        for proposal in proposals:
            if proposal.decision == "reject":
                continue
            for edit in proposal.edits:
                name = edit.get("class_name") or edit.get("property_name")
                if not name or edit.get("action") not in ("add_class", "add_property"):
                    continue
                if name in owners and owners[name] != proposal.proposal_id:
                    ambiguous[proposal.proposal_id] = f"名称「{name}」被多条提案同时声明"
                    ambiguous[owners[name]] = f"名称「{name}」被多条提案同时声明"
                else:
                    owners[name] = proposal.proposal_id
        return ambiguous

    def _verify(self, proposal: EditProposal, cluster: UnkCluster | None) -> VerificationResult:
        replayable = self._count_replayable(cluster) if proposal.actionable else 0
        if not proposal.actionable:
            return VerificationResult(safe=False, replayable_tuples=replayable)

        trial = Ontology.from_dict(self.ontology.to_dict())
        try:
            trial.apply_edits(
                proposal.edits,
                new_version=bump_version(self.ontology.version),
                rationale="试运行",
                actor="verifier",
            )
        except Exception as exc:  # noqa: BLE001 - 任何失败都等价于"此提案不安全"
            return VerificationResult(
                safe=False,
                axiom_violations=[{"error": f"{type(exc).__name__}: {exc}"}],
                replayable_tuples=replayable,
                notes=["候选编辑在本体副本上执行失败，已判定为不安全"],
            )

        notes: list[str] = []
        instance_violations: list[dict] = []
        pre_existing = 0
        touched = self._touched_classes(proposal)
        for class_name in touched:
            baseline = self._validate_existing_instances(self.ontology, class_name)
            # 判定标准是"这条编辑**新引入**了多少违规"，不是"改动涉及的类下现在有
            # 多少违规"。现网图里本来就存在历史脏节点（导入期降级写入的裸节点），
            # 拿绝对违规数当判据会让每一条提案都被自己的历史否决掉。
            after = self._validate_existing_instances(trial, class_name)
            baseline_keys = {_violation_key(v) for v in baseline}
            pre_existing += len(baseline)
            instance_violations.extend(
                v for v in after if _violation_key(v) not in baseline_keys
            )

        disjoint_conflicts = self._check_disjoint(trial, proposal)
        if instance_violations:
            notes.append(f"该编辑新引入 {len(instance_violations)} 处图节点违规")
        if pre_existing:
            notes.append(f"另有 {pre_existing} 处历史违规（编辑前已存在，不计入判定）")
        if disjoint_conflicts:
            notes.append("拟声明的互斥类之间存在共同实例，本体将不一致")

        return VerificationResult(
            safe=not instance_violations and not disjoint_conflicts,
            instance_violations=instance_violations,
            disjoint_conflicts=disjoint_conflicts,
            replayable_tuples=replayable,
            notes=notes,
        )

    def _touched_classes(self, proposal: EditProposal) -> set[str]:
        touched: set[str] = set()
        for edit in proposal.edits:
            for key in ("class_name", "parent_class", "domain", "range", "class_a", "class_b"):
                value = edit.get(key)
                if value and value in self.ontology.classes:
                    touched.add(value)
        return touched

    def _validate_existing_instances(self, trial: Ontology, class_name: str) -> list[dict]:
        """用新本体复核现网已存在的节点。

        只查"该类的直接实例 + 其子类实例"。全图扫描在这里是不可接受的：
        一轮演化可能触发几十次验证，每次全扫会把图规模变成演化速度的上限。
        """
        targets = [class_name, *trial.descendants(class_name)]
        out: list[dict] = []
        for ntype in targets:
            for node in self.store.query_nodes(ntype=ntype, limit=500):
                for violation in trial.validate_instance(ntype, node["props"]):
                    if violation.severity == "Violation":
                        out.append(
                            {
                                "node_id": node["id"],
                                "ntype": ntype,
                                "property": violation.property_name,
                                "message": violation.message,
                            }
                        )
        return out

    def _check_disjoint(self, trial: Ontology, proposal: EditProposal) -> list[dict]:
        pairs = [
            (e["class_a"], e["class_b"]) for e in proposal.edits if e.get("action") == "add_disjoint"
        ]
        out: list[dict] = []
        for a, b in pairs:
            for ntype in [a, *trial.descendants(a)]:
                for node in self.store.query_nodes(ntype=ntype, limit=200):
                    if trial.is_subclass_of(ntype, b):
                        out.append({"node_id": node["id"], "conflict": f"{a} vs {b}"})
        return out

    def _count_replayable(self, cluster: UnkCluster | None) -> int:
        if cluster is None:
            return 0
        total = 0
        for cid in cluster.member_ids:
            cand = self.pool.get(cid)
            if cand is not None:
                total += len(cand.tuples)
        return total

    def _decide(
        self,
        proposal: EditProposal,
        verification: VerificationResult,
        ambiguous: dict[str, str],
    ) -> tuple[str, str]:
        if proposal.decision == "reject" or not proposal.actionable:
            return _STATUS_REJECTED, proposal.rejected_reason or "无可执行编辑"
        if not verification.safe:
            return _STATUS_REJECTED, "一致性验证未通过：" + "；".join(verification.notes)
        if proposal.proposal_id in ambiguous:
            return _STATUS_PENDING, ambiguous[proposal.proposal_id]
        if proposal.confidence < self.auto_approve_threshold:
            return (
                _STATUS_PENDING,
                f"置信度 {proposal.confidence:.2f} 低于自动采纳门槛 "
                f"{self.auto_approve_threshold:.2f}，转人工仲裁",
            )
        return _STATUS_APPROVED, f"验证通过且置信度 {proposal.confidence:.2f} 达标，自动采纳"

    # ------------------------------------------------------------------

    def _enqueue(self, records: list[dict]) -> None:
        """提案落图：即便被拒绝也留痕。

        "某概念在本体 v0.2.0 被判定为噪声而拒绝"本身是有价值的资产——
        下一轮它再次大量出现时，仲裁者能看到这不是新问题。
        """
        for record in records:
            proposal: EditProposal = record["proposal"]
            node_id = f"proposal:{proposal.proposal_id.split(':')[-1]}"
            props = {
                "id": node_id,
                "提案编号": proposal.proposal_id,
                "动作": "+".join(sorted({e.get("action", "?") for e in proposal.edits})) or "none",
                "置信度": float(proposal.confidence),
                "__status__": record["status"],
                "__status_reason__": record["status_reason"],
                "__canonical__": proposal.canonical,
                "__decision__": proposal.decision,
                "__origin__": proposal.origin,
                "__rationale__": proposal.rationale,
                "__edits__": proposal.edits,
                "__verification__": record["verification"].to_dict(),
                "__cluster__": record["cluster"].to_dict() if record["cluster"] else {},
            }
            self.store.upsert_node(
                "本体提案",
                label=f"{proposal.canonical}（{record['status']}）",
                props=props,
                node_id=node_id,
                validate=False,
            )
            cluster = record["cluster"]
            if cluster is not None:
                for cid in cluster.member_ids:
                    if self.store.get_node(cid) is None:
                        continue
                    if self.store.find_edges(src=node_id, dst=cid, etype="提案针对候选"):
                        continue
                    self.store.add_edge(
                        node_id,
                        cid,
                        "提案针对候选",
                        evidence={
                            "snippet": f"提案「{proposal.canonical}」聚合自 {len(cluster.members)} 条候选提及",
                            "modality": "meta",
                            "source_document": ",".join(cluster.documents[:3]),
                            "source_locator": "evolution:cluster",
                            "cluster_members": cluster.members[:10],
                        },
                        confidence=cluster.cohesion,
                        validate=False,
                    )

    # ------------------------------------------------------------------

    def _commit(self, records: list[dict], report: EvolutionReport) -> str:
        approved = [r for r in records if r["status"] == _STATUS_APPROVED]
        all_edits: list[dict] = []
        for record in approved:
            all_edits.extend(record["proposal"].edits)

        new_version = bump_version(self.ontology.version, kind="minor")
        rationale = "；".join(
            f"{r['proposal'].canonical}({r['proposal'].proposal_id})" for r in approved[:6]
        )
        applied_proposals: list[str] = []
        try:
            self.ontology.apply_edits(
                all_edits, new_version=new_version, rationale=rationale, actor="auto"
            )
            applied_proposals = [r["proposal"].proposal_id for r in approved]
        except Exception as exc:  # noqa: BLE001
            # 整批失败时逐条重试：一条坏提案不该让同批的其他提案一起陪葬。
            report.deferred.append({"batch_error": str(exc), "fallback": "逐条提交"})
            for record in approved:
                proposal = record["proposal"]
                try:
                    self.ontology.apply_edits(
                        proposal.edits,
                        new_version=bump_version(self.ontology.version, kind="minor"),
                        rationale=f"{proposal.canonical}: {proposal.rationale[:120]}",
                        actor="auto",
                    )
                    applied_proposals.append(proposal.proposal_id)
                except Exception as inner:  # noqa: BLE001
                    record["status"] = _STATUS_REJECTED
                    record["status_reason"] = f"提交阶段失败并已回滚：{inner}"
                    report.rejected.append(
                        {"canonical": proposal.canonical, "reason": record["status_reason"]}
                    )
            new_version = self.ontology.version

        self.store.ontology = self.ontology
        self.store.record_ontology_version(
            new_version,
            rationale=rationale,
            actor="auto",
        )
        version_node = f"ontology:{new_version}"
        self.store.upsert_node(
            "本体版本",
            label=f"本体 {new_version}",
            props={
                "id": version_node,
                "版本号": new_version,
                "生效时间": _today(),
                "__change_summary__": rationale,
                "__class_count__": len(self.ontology.classes),
                "__property_count__": len(self.ontology.properties),
                "__origin__": "auto" if applied_proposals else "manual",
            },
            node_id=version_node,
            validate=False,
        )
        for record in approved:
            if record["proposal"].proposal_id not in applied_proposals:
                continue
            pid = f"proposal:{record['proposal'].proposal_id.split(':')[-1]}"
            if self.store.get_node(pid) is None:
                continue
            if self.store.find_edges(src=version_node, dst=pid, etype="版本包含提案"):
                continue
            self.store.add_edge(
                version_node,
                pid,
                "版本包含提案",
                evidence={
                    "snippet": f"本体 {new_version} 合并了提案「{record['proposal'].canonical}」",
                    "modality": "meta",
                    "source_document": new_version,
                    "source_locator": "evolution:commit",
                },
                confidence=1.0,
                validate=False,
            )

        for record in records:
            proposal: EditProposal = record["proposal"]
            if proposal.proposal_id not in applied_proposals:
                continue
            self._record_outcome(record, report)

        report.version_after = new_version
        return new_version

    def _record_outcome(self, record: dict, report: EvolutionReport) -> None:
        proposal: EditProposal = record["proposal"]
        cluster: UnkCluster | None = record["cluster"]
        resolved_name = ""
        for edit in proposal.edits:
            if edit.get("action") == "add_class":
                resolved_name = edit["class_name"]
                report.classes_added.append(resolved_name)
            elif edit.get("action") == "add_property":
                resolved_name = edit["property_name"]
                report.properties_added.append(resolved_name)
            elif edit.get("action") == "add_alias":
                resolved_name = edit["class_name"]
                report.alias_learned += 1
            elif edit.get("action") == "add_property_alias":
                resolved_name = edit["property_name"]
                report.alias_learned += 1
        if cluster is not None:
            for cid in cluster.member_ids:
                if self.store.get_node(cid) is not None:
                    self.pool.mark_resolved(cid, resolved_name or "alias", note=proposal.rationale[:200])

    # ------------------------------------------------------------------

    def _reindex(self, records: list[dict], report: EvolutionReport) -> None:
        """新概念落地后，把当初无家可归的证据重新送进图。

        这是演化唯一说得清的收益：本体变得更宽了，原来被丢掉的观测现在有了
        类型归属，进而变成可参与路径搜索的节点和边。不做这一步，"本体演化"
        就只是改了一个 JSON 文件。
        """
        loader = GraphLoader(self.store, self.ontology)
        replayed: list[ModalityTuple] = []
        for record in records:
            proposal: EditProposal = record["proposal"]
            cluster: UnkCluster | None = record["cluster"]
            if cluster is None or not proposal.actionable:
                continue
            resolved = {
                edit.get("class_name") or edit.get("property_name")
                for edit in proposal.edits
                if edit.get("action")
                in ("add_class", "add_property", "add_alias", "add_property_alias")
            }
            resolved.discard(None)
            for cid in cluster.member_ids:
                cand = self.pool.get(cid)
                if cand is None or not cand.resolved_type:
                    continue
                replayed.extend(self._rebuild_tuples(cand, resolved))

        if not replayed:
            return
        load_report = loader.load(ExtractionResult(tuples=replayed))
        report.reindexed_nodes = load_report.nodes_created
        report.reindexed_edges = load_report.edges_created

    def _rebuild_tuples(self, cand: UnkCandidate, resolved: set[str]) -> list[ModalityTuple]:
        out: list[ModalityTuple] = []
        for payload in cand.tuples:
            data = dict(payload)
            if cand.kind == KIND_ENTITY:
                data["entity_type"] = cand.resolved_type
            else:
                replacement = next(
                    (r for r in resolved if self.ontology.properties.get(r) is not None), None
                )
                if replacement is None:
                    continue
                data["relation_type"] = replacement
            # 实体类型已解决、但关系仍未知的元组不能整条丢掉：实体的属性
            # （注册资本、缴纳月份……）本身就是证据，把它们一起扔了等于
            # 为了一个还不认识的关系动词放弃已经认出来的事实。
            if data.get("relation_type") == UNK_RELATION:
                data["relation_type"] = None
            try:
                tup = ModalityTuple(**data)
            except TypeError:
                continue
            if tup.is_unk:
                continue
            out.append(tup)
        return out

    # ------------------------------------------------------------------
    # 人工仲裁接口
    # ------------------------------------------------------------------

    def pending_proposals(self) -> list[dict]:
        out = []
        for node in self.store.query_nodes(ntype="本体提案", limit=None):
            props = node["props"]
            if props.get("__status__") != _STATUS_PENDING:
                continue
            out.append(
                {
                    "proposal_node": node["id"],
                    "提案编号": props.get("提案编号"),
                    "候选": props.get("__canonical__"),
                    "动作": props.get("动作"),
                    "置信度": props.get("置信度"),
                    "依据": props.get("__rationale__"),
                    "等待原因": props.get("__status_reason__"),
                    "编辑": props.get("__edits__"),
                    "验证": props.get("__verification__"),
                }
            )
        return out

    def arbitrate(self, proposal_node: str, *, approve: bool, actor: str = "human", note: str = "") -> str:
        node = self.store.get_node(proposal_node)
        if node is None:
            raise KeyError(f"提案节点不存在: {proposal_node}")
        props = node["props"]
        if props.get("__status__") != _STATUS_PENDING:
            raise ValueError(f"提案 {proposal_node} 状态为 {props.get('__status__')}，不在待仲裁队列")

        if not approve:
            self.store.update_node_props(
                proposal_node,
                {
                    "__status__": _STATUS_REJECTED,
                    "__status_reason__": f"人工驳回（{actor}）：{note}",
                    "__actor__": actor,
                },
            )
            return _STATUS_REJECTED

        edits = list(props.get("__edits__") or [])
        new_version = bump_version(self.ontology.version, kind="minor")
        self.ontology.apply_edits(
            edits,
            new_version=new_version,
            rationale=f"人工仲裁通过：{props.get('__canonical__')} {note}".strip(),
            actor=actor,
        )
        self.store.ontology = self.ontology
        self.store.record_ontology_version(new_version, rationale=note, actor=actor)
        self.store.update_node_props(
            proposal_node,
            {
                "__status__": _STATUS_APPROVED,
                "__status_reason__": f"人工仲裁通过（{actor}）",
                "__actor__": actor,
                "__committed_version__": new_version,
            },
        )
        version_node = f"ontology:{new_version}"
        self.store.upsert_node(
            "本体版本",
            label=f"本体 {new_version}",
            props={
                "id": version_node,
                "版本号": new_version,
                "生效时间": _today(),
                "__change_summary__": props.get("__canonical__"),
                "__class_count__": len(self.ontology.classes),
                "__property_count__": len(self.ontology.properties),
                "__origin__": "manual",
            },
            node_id=version_node,
            validate=False,
        )
        self.store.add_edge(
            version_node,
            proposal_node,
            "版本包含提案",
            evidence={
                "snippet": f"人工仲裁合并提案「{props.get('__canonical__')}」",
                "modality": "meta",
                "source_document": new_version,
                "source_locator": "evolution:arbitration",
            },
            confidence=1.0,
            validate=False,
        )
        resolved_name = next(
            (
                e.get("class_name") or e.get("property_name")
                for e in edits
                if e.get("action") in ("add_class", "add_property", "add_alias", "add_property_alias")
            ),
            "alias",
        )
        member_ids: list[str] = []
        for edge in self.store.find_edges(src=proposal_node, etype="提案针对候选"):
            cand = self.pool.get(edge["dst"])
            if cand is not None:
                self.pool.mark_resolved(
                    edge["dst"], resolved_name, note=f"人工仲裁 by {actor}"
                )
                member_ids.append(edge["dst"])

        # 人工仲裁走的是与自动提交**同一套**后处理，不能只改本体就收工：
        # 演化唯一的可观测收益是"此前无家可归的证据重新进图"，漏掉重放
        # 会让人工路径看起来什么都没发生。
        reindex = self._reindex_tuples(resolved_name, member_ids)
        self.store.update_node_props(
            proposal_node,
            {
                "__reindexed_nodes__": reindex[0],
                "__reindexed_edges__": reindex[1],
            },
        )

        if self.persist_path is not None:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            self.persist_path.write_text(self.ontology.to_json(), encoding="utf-8")
        return _STATUS_APPROVED

    def _reindex_tuples(self, resolved_name: str, member_ids: list[str]) -> tuple[int, int]:
        loader = GraphLoader(self.store, self.ontology)
        replayed: list[ModalityTuple] = []
        for cid in member_ids:
            cand = self.pool.get(cid)
            if cand is not None and cand.resolved_type:
                replayed.extend(self._rebuild_tuples(cand, {resolved_name}))
        if not replayed:
            return 0, 0
        load_report = loader.load(ExtractionResult(tuples=replayed))
        return load_report.nodes_created, load_report.edges_created

    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "ontology": self.ontology.to_dict(),
            "pool": self.pool.stats(),
            "versions": self.store.ontology_version_history(),
        }


def _violation_key(violation: dict) -> tuple:
    return (violation.get("node_id"), violation.get("property"), violation.get("message"))


def _today() -> str:
    return date.today().isoformat()


def load_ontology(path: str | Path) -> Ontology:
    """从磁盘恢复本体；文件缺失或损坏时回到种子本体，绝不静默用半截数据。"""
    p = Path(path)
    if not p.exists():
        return build_seed_ontology()
    try:
        return Ontology.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return build_seed_ontology()


__all__ = [
    "OntologyEvolutionPipeline",
    "EvolutionReport",
    "VerificationResult",
    "AUTO_APPROVE_THRESHOLD",
    "load_ontology",
]
