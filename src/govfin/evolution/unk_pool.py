"""UNK 储备池：装不下现有本体的提及，先存起来，不进图。

为什么必须要有这个池子，而不是让抽取器硬凑一个类型：

低资源域里，抽取器遇到没见过的东西是常态。如果强制它从现有类里挑一个最近的，
错误分类会被后续所有推理当成事实使用——一条"社保断缴"被误判成"行政处罚"，
整条决策链的推理依据就错了，而且错得无法察觉。储备池把这种不确定性**显式化**：
承认现在不认识，带着次数、来源、上下文记下来，等观测够了再通过演化管道变成
本体的一部分。

池子本身就是图里的内容（元层 ``UNK候选`` 节点），不是旁挂的一张表。这样
"某个概念被观测了 7 次才被纳入本体"这件事可查询、可审计、可作为演化证据引用。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date

from govfin.graph.schema import LAYER_META
from govfin.graph.store import GraphStore
from govfin.ontology.model import Ontology

UNK_ENTITY = "UNK-ENTITY"
UNK_RELATION = "UNK-RELATION"

KIND_ENTITY = "entity"
KIND_RELATION = "relation"

MAX_CONTEXTS = 12
MAX_TUPLES = 40  # 重放用的原始五元组上限，超出只记计数


@dataclass
class UnkCandidate:
    """一个尚未被本体承认的概念，连同它被观测到的全部痕迹。"""

    candidate_id: str
    text: str
    kind: str
    normalized: str
    observations: int = 1
    source_documents: list[str] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)
    hint_type: str | None = None
    first_seen: str = ""
    last_seen: str = ""
    resolved_type: str | None = None
    tuples: list[dict] = field(default_factory=list)
    truncated: bool = False

    @property
    def is_resolved(self) -> bool:
        return bool(self.resolved_type)

    def document_count(self) -> int:
        return len(self.source_documents)

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "text": self.text,
            "kind": self.kind,
            "normalized": self.normalized,
            "observations": self.observations,
            "document_count": self.document_count(),
            "source_documents": self.source_documents[:10],
            "contexts": self.contexts[:MAX_CONTEXTS],
            "hint_type": self.hint_type,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "resolved_type": self.resolved_type,
            "tuple_count": len(self.tuples),
            "truncated": self.truncated,
        }


def normalize_text(text: str) -> str:
    """归一化：去空白、全角转半角、剥离包裹符号。

    不做同义词归并——那是演化管道该干的事（聚类 + 别名学习）。
    这里只保证"同一个字符串的不同排版"合并成同一条候选。
    """
    out: list[str] = []
    for ch in (text or "").strip():
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:  # 全角 ASCII
            out.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    cleaned = "".join(out)
    cleaned = "".join(ch for ch in cleaned if not ch.isspace())
    return cleaned.strip("《》〈〉「」『』【】\"'`.,;:!?、。，；：！？")


def candidate_id(kind: str, normalized: str) -> str:
    digest = hashlib.sha1(f"{kind}|{normalized}".encode("utf-8")).hexdigest()[:12]
    return f"unk:{kind}:{digest}"


class UnkPool:
    def __init__(self, store: GraphStore, ontology: Ontology | None = None) -> None:
        self.store = store
        self.ontology = ontology or store.ontology

    # ------------------------------------------------------------------

    def observe(
        self,
        text: str,
        kind: str,
        *,
        source_document: str = "",
        context: str = "",
        hint_type: str | None = None,
        seen_on: str | None = None,
        tuple_payload: dict | None = None,
    ) -> str:
        """记录一次观测。同一概念重复出现只累加计数，不新建节点。"""
        normalized = normalize_text(text)
        if not normalized:
            return ""
        cid = candidate_id(kind, normalized)
        existing = self.store.get_node(cid)
        today = seen_on or date.today().isoformat()

        if existing is None:
            props = {
                "id": cid,
                "候选文本": normalized,
                "观测次数": 1,
                "首次观测": today,
                "来源文档": [source_document] if source_document else [],
                "__kind__": kind,
                "__hint__": hint_type or "",
                "__resolved__": "",
                "__contexts__": [context] if context else [],
                "__tuples__": [tuple_payload] if tuple_payload else [],
                "__truncated__": False,
            }
            self.store.add_node(
                "UNK候选",
                layer=LAYER_META,
                label=normalized,
                props=props,
                provenance={
                    "first_seen": {
                        "document": source_document,
                        "modality": "",
                        "snippet": context,
                    },
                    "origin": "unk_pool",
                },
                node_id=cid,
                validate=False,
            )
            return cid

        self._accumulate(cid, existing, source_document, context, tuple_payload, today)
        return cid

    def _accumulate(
        self,
        cid: str,
        node: dict,
        source_document: str,
        context: str,
        tuple_payload: dict | None,
        today: str,
    ) -> None:
        props = node["props"]
        docs = list(props.get("来源文档") or [])
        if source_document and source_document not in docs:
            docs.append(source_document)
        contexts = list(props.get("__contexts__") or [])
        if context and context not in contexts:
            contexts.append(context)
        tuples = list(props.get("__tuples__") or [])
        truncated = bool(props.get("__truncated__"))
        if tuple_payload is not None:
            if len(tuples) < MAX_TUPLES:
                tuples.append(tuple_payload)
            else:
                truncated = True
        self.store.update_node_props(
            cid,
            {
                "观测次数": int(props.get("观测次数") or 0) + 1,
                "来源文档": docs,
                "__contexts__": contexts[-MAX_CONTEXTS:],
                "__tuples__": tuples[-MAX_TUPLES:],
                "__truncated__": truncated,
            },
        )
        self.store.update_node_props(cid, {"首次观测": props.get("首次观测") or today})

    # ------------------------------------------------------------------

    def get(self, cid: str) -> UnkCandidate | None:
        node = self.store.get_node(cid)
        return self._to_candidate(node) if node else None

    def candidates(
        self,
        *,
        kind: str | None = None,
        unresolved_only: bool = True,
        min_observations: int = 1,
    ) -> list[UnkCandidate]:
        out: list[UnkCandidate] = []
        for node in self.store.query_nodes(ntype="UNK候选", limit=None):
            cand = self._to_candidate(node)
            if cand is None:
                continue
            if kind is not None and cand.kind != kind:
                continue
            if unresolved_only and cand.is_resolved:
                continue
            if cand.observations < min_observations:
                continue
            out.append(cand)
        out.sort(key=lambda c: (-c.observations, c.text))
        return out

    def _to_candidate(self, node: dict) -> UnkCandidate | None:
        props = node.get("props") or {}
        text = str(props.get("候选文本") or node.get("label") or "")
        if not text:
            return None
        return UnkCandidate(
            candidate_id=node["id"],
            text=text,
            kind=str(props.get("__kind__") or KIND_ENTITY),
            normalized=text,
            observations=int(props.get("观测次数") or 1),
            source_documents=list(props.get("来源文档") or []),
            contexts=list(props.get("__contexts__") or []),
            hint_type=str(props.get("__hint__") or "") or None,
            first_seen=str(props.get("首次观测") or ""),
            last_seen=str(node.get("updated_at") or ""),
            resolved_type=str(props.get("__resolved__") or "") or None,
            tuples=list(props.get("__tuples__") or []),
            truncated=bool(props.get("__truncated__")),
        )

    def mark_resolved(self, cid: str, resolved_type: str, *, note: str = "") -> None:
        """概念被本体接纳后，把它从待处理队列里摘出去。

        不删除节点：这条例的观测历史是"本体为何演进"的证据，
        决策溯源时会回溯到"该概念于 v0.3.0 由 UNK-ENTITY 升格为 XX"。
        """
        patch = {"__resolved__": resolved_type}
        if note:
            patch["__resolution_note__"] = note
        self.store.update_node_props(cid, patch)

    def stats(self) -> dict:
        all_cands = self.candidates(unresolved_only=False)
        unresolved = [c for c in all_cands if not c.is_resolved]
        by_kind: dict[str, int] = {}
        for cand in unresolved:
            by_kind[cand.kind] = by_kind.get(cand.kind, 0) + 1
        return {
            "total": len(all_cands),
            "unresolved": len(unresolved),
            "resolved": len(all_cands) - len(unresolved),
            "unresolved_by_kind": by_kind,
            "observations_pending": sum(c.observations for c in unresolved),
            "truncated_replay_payloads": sum(1 for c in all_cands if c.truncated),
        }
