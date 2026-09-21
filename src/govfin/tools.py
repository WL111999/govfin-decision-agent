"""领域工具集：智能体能调用的原子能力，与传输协议无关。

MCP 和 A2A 是两种传输，不该有两套业务实现。所有工具在这里定义一次，
``govfin.mcp.server`` 把它们注册成 MCP tool，``govfin.a2a.server`` 把它们
挂到 Agent Card 的 skill 上。

每个工具都遵守三条约定，这三条是给**调用它的模型**看的，不是给人看的：

1. **返回结构固定**。命中与否都返回 ``{"ok": bool, ...}``，不抛异常。
   Agent 拿到 ``ok=false`` 能继续决策（换一个主体、换一条约束），
   拿到一个 Python traceback 就只能放弃。
2. **证据随行**。凡是返回事实的工具，每条事实都带 ``source_document`` 与
   ``snippet``。没有出处的结论在授信场景里等于没有结论。
3. **空结果要区分"没有"和"不知道"**。``found=false`` 与 ``ok=false`` 是两回事：
   前者是"查到了，确实没有这条记录"，后者是"查不了"。把两者混为一谈会让
   Agent 把系统故障当成企业清白，这是最危险的一类错误。
"""

from __future__ import annotations

from typing import Any

from govfin.reasoning.constraints import BUILTIN_CONSTRAINTS, PathConstraint
from govfin.reasoning.decision import Decision
from govfin.runtime import AgentRuntime

MAX_RECORDS = 40


class DomainTools:
    def __init__(self, runtime: AgentRuntime) -> None:
        self.rt = runtime

    # ------------------------------------------------------------------
    # 主体解析
    # ------------------------------------------------------------------

    def resolve(self, subject: str) -> dict:
        """把企业名称或统一社会信用代码解析成图上的节点。

        检索顺序是刻意的：先按强标识符（信用代码）精确匹配，再按名称精确匹配，
        最后才放宽到包含匹配。包含匹配放在最后是因为它最容易误伤——"甲科技"
        会同时命中"甲科技有限公司"和"甲科技服务有限公司"，两家完全不同的主体。
        一旦走到模糊匹配，结果里会带 ``fuzzy=true``，调用方必须自己确认。
        """
        query = (subject or "").strip()
        if not query:
            return {"ok": False, "error": "subject 为空", "candidates": []}

        store = self.rt.store
        for key in ("统一社会信用代码", "身份证号"):
            hits = store.find_by_prop(key, query, limit=5)
            if hits:
                return self._subject_payload(hits[0], fuzzy=False)

        exact = [
            n
            for n in store.query_nodes(label_like=query, limit=20)
            if n["label"] == query and n["ntype"] in ("企业", "自然人")
        ]
        if exact:
            return self._subject_payload(exact[0], fuzzy=False)

        fuzzy = store.query_nodes(label_like=query, limit=10)
        fuzzy = [n for n in fuzzy if n["ntype"] in ("企业", "自然人")]
        if len(fuzzy) == 1:
            return self._subject_payload(fuzzy[0], fuzzy=True)
        if fuzzy:
            return {
                "ok": False,
                "error": f"'{query}' 匹配到多个主体，请用统一社会信用代码精确指定",
                "fuzzy": True,
                "candidates": [
                    {"node": n["id"], "label": n["label"], "ntype": n["ntype"]} for n in fuzzy[:10]
                ],
            }
        return {"ok": False, "error": f"未找到主体 '{query}'", "candidates": []}

    def _subject_payload(self, node: dict, *, fuzzy: bool) -> dict:
        return {
            "ok": True,
            "fuzzy": fuzzy,
            "node": node["id"],
            "label": node["label"],
            "ntype": node["ntype"],
            "attributes": {
                k: v for k, v in node["props"].items() if not k.startswith("__") and k != "id"
            },
        }

    # ------------------------------------------------------------------
    # 政务侧事实
    # ------------------------------------------------------------------

    def gov_business_lookup(self, subject: str) -> dict:
        """工商登记核验：登记记录、注册资本、经营范围、成立日期。"""
        resolved = self.resolve(subject)
        if not resolved["ok"]:
            return resolved
        records = self._along(resolved["node"], ("拥有工商登记",), ("工商登记",))
        return {
            "ok": True,
            "subject": resolved["label"],
            "node": resolved["node"],
            "found": bool(records),
            "record_count": len(records),
            "records": records,
            "conclusion": (
                f"命中 {len(records)} 条工商登记记录，主体登记在册"
                if records
                else "未命中任何工商登记记录：该主体在登记库中查无记录，真实性核验不通过"
            ),
        }

    def gov_social_security(self, subject: str) -> dict:
        """社保缴纳核验：缴费记录与异常月份（欠缴/断缴）。"""
        resolved = self.resolve(subject)
        if not resolved["ok"]:
            return resolved
        records = self._along(resolved["node"], ("缴纳社保",), ("社保缴纳记录",))
        abnormal = [
            r for r in records if str(r["attributes"].get("缴纳状态") or "") not in ("正常", "")
        ]
        return {
            "ok": True,
            "subject": resolved["label"],
            "found": bool(records),
            "record_count": len(records),
            "abnormal_count": len(abnormal),
            "abnormal_records": abnormal,
            "records": records,
            "conclusion": (
                f"{len(records)} 条缴费记录中 {len(abnormal)} 条异常"
                if records
                else "未命中社保缴纳记录"
            ),
        }

    def gov_judicial_scan(self, subject: str) -> dict:
        """司法与处罚扫描：行政处罚、涉诉案件。"""
        resolved = self.resolve(subject)
        if not resolved["ok"]:
            return resolved
        penalties = self._along(resolved["node"], ("受到处罚",), ("行政处罚",))
        lawsuits = self._along(resolved["node"], (), ("诉讼案件", "民事判决书", "判决书"))
        return {
            "ok": True,
            "subject": resolved["label"],
            "found": bool(penalties or lawsuits),
            "penalty_count": len(penalties),
            "lawsuit_count": len(lawsuits),
            "penalties": penalties,
            "lawsuits": lawsuits,
            "conclusion": (
                f"命中 {len(penalties)} 条行政处罚、{len(lawsuits)} 条涉诉记录"
                if (penalties or lawsuits)
                else "未命中行政处罚或涉诉记录"
            ),
        }

    def fin_financial_parser(self, subject: str) -> dict:
        """财务数据解析：从财务报表与征信报告中取出本体已对齐的财务指标。"""
        resolved = self.resolve(subject)
        if not resolved["ok"]:
            return resolved
        attrs = resolved["attributes"]
        metrics = {
            key: attrs[key]
            for key in ("注册资本", "负债总额", "资产总额", "营业收入", "逾期次数", "资产负债率")
            if key in attrs
        }
        if "资产负债率" not in metrics and "负债总额" in metrics and metrics.get("资产总额"):
            metrics["资产负债率"] = round(
                float(metrics["负债总额"]) / float(metrics["资产总额"]) * 100, 2
            )
        evidence = self._provenance_of(resolved["node"])
        return {
            "ok": True,
            "subject": resolved["label"],
            "found": bool(metrics),
            "metrics": metrics,
            "evidence": evidence,
            "conclusion": (
                "已解析财务指标：" + "、".join(f"{k}={v}" for k, v in metrics.items())
                if metrics
                else "该主体无可用财务数据"
            ),
        }

    # ------------------------------------------------------------------
    # 图推理
    # ------------------------------------------------------------------

    def kg_path_query(
        self, start: str, constraint: str = "关联方风险传导扫描", max_hops: int | None = None
    ) -> dict:
        """受约束的多跳路径查询。约束决定"什么样的链能回答问题"。"""
        if constraint not in BUILTIN_CONSTRAINTS:
            return {
                "ok": False,
                "error": f"未定义的路径约束 '{constraint}'",
                "available": sorted(BUILTIN_CONSTRAINTS),
            }
        resolved = self.resolve(start)
        if not resolved["ok"]:
            return resolved

        spec: PathConstraint = BUILTIN_CONSTRAINTS[constraint]
        if max_hops is not None:
            from dataclasses import replace

            spec = replace(spec, max_hops=max(1, min(int(max_hops), 8)))

        with self.rt.lock:
            result = self.rt.engine.search(resolved["node"], spec)
        payload = result.to_dict()
        payload["ok"] = True
        payload["subject"] = resolved["label"]
        payload["constraint_spec"] = spec.to_dict()
        return payload

    def risk_decision(self, subject: str, *, persist: bool = True) -> dict:
        """完整授信决策：真实性闸门 → 关联方传导 → 阈值判定 → 溯源落库。"""
        resolved = self.resolve(subject)
        if not resolved["ok"]:
            return resolved
        with self.rt.lock:
            decision: Decision = self.rt.synthesizer.synthesize(resolved["node"])
            payload = decision.to_dict(include_paths=False)
            if persist:
                report = self.rt.recorder.record(decision)
                payload["provenance"] = report.to_dict()
        payload["ok"] = True
        payload["judgements"] = [j.to_dict() for j in decision.judgements]
        return payload

    # ------------------------------------------------------------------
    # 证据与审计
    # ------------------------------------------------------------------

    def evidence_bundle(
        self, decision_id: str | None = None, subject: str | None = None
    ) -> dict:
        """证据束：取回一次决策的完整依据链，或某主体最近的决策依据。"""
        if not decision_id and not subject:
            return {"ok": False, "error": "必须提供 decision_id 或 subject 之一"}

        if not decision_id:
            resolved = self.resolve(subject or "")
            if not resolved["ok"]:
                return resolved
            found = self.rt.trail.decisions_referencing(resolved["node"])
            if not found:
                return {
                    "ok": False,
                    "error": f"主体 '{resolved['label']}' 尚无已记录的决策，请先调用 risk_decision",
                }
            decision_id = str(found[0]["decision_id"])

        try:
            report = self.rt.trail.trace(decision_id)
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}
        payload = report.to_dict()
        payload["ok"] = True
        payload["report"] = report.render()
        return payload

    # ------------------------------------------------------------------
    # 本体演化与运维
    # ------------------------------------------------------------------

    def ontology_status(self) -> dict:
        """本体状态：版本、类/属性规模、UNK 储备池中待演化的候选。"""
        with self.rt.lock:
            pool = self.rt.store.query_nodes(ntype="UNK候选", limit=None)
            proposals = self.rt.store.query_nodes(ntype="本体提案", limit=None)
        pending = [n for n in proposals if n["props"].get("__status__") == "pending"]
        unresolved = [
            {"text": n["props"].get("候选文本"), "observations": n["props"].get("观测次数")}
            for n in pool
            if not n["props"].get("__resolved__")
        ]
        unresolved.sort(key=lambda x: -(x["observations"] or 0))
        return {
            "ok": True,
            "ontology_version": self.rt.ontology.version,
            "classes": len(self.rt.ontology.classes),
            "properties": len(self.rt.ontology.properties),
            "unk_candidates": len(unresolved),
            "unk_top": unresolved[:20],
            "pending_proposals": len(pending),
            "graph": self.rt.stats().to_dict(),
        }

    def ontology_evolve(self, *, commit: bool = True, use_llm: bool | None = None) -> dict:
        """跑一轮本体演化：储备池 → 聚类 → 符号对齐 → 提案 → 验证 → 提交。"""
        with self.rt.lock:
            pipeline = self.rt.pipeline
            if use_llm is not None:
                pipeline.use_llm = use_llm and pipeline.client is not None
            report = pipeline.run_cycle(commit=commit)
        payload = report.to_dict() if hasattr(report, "to_dict") else dict(report)
        payload["ok"] = True
        return payload

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _along(self, node_id: str, etypes: tuple[str, ...], ntypes: tuple[str, ...]) -> list[dict]:
        """沿指定边类型走到指定类型的邻居，并把边的证据一起带回来。"""
        seen: set[str] = set()
        out: list[dict] = []
        for edge in self.rt.store.neighbors(node_id, direction="both"):
            if etypes and edge["etype"] not in etypes:
                continue
            other_id = edge["dst"] if edge["src"] == node_id else edge["src"]
            if other_id in seen:
                continue
            other = self.rt.store.get_node(other_id)
            if other is None or (ntypes and other["ntype"] not in ntypes):
                continue
            seen.add(other_id)
            evidence = edge.get("evidence") or {}
            out.append(
                {
                    "node": other["id"],
                    "ntype": other["ntype"],
                    "label": other["label"],
                    "attributes": {
                        k: v
                        for k, v in other["props"].items()
                        if not k.startswith("__") and k != "id"
                    },
                    "relation": edge["etype"],
                    "confidence": edge.get("confidence"),
                    "evidence_class": edge.get("evidence_class"),
                    "source_document": evidence.get("source_document"),
                    "source_locator": evidence.get("source_locator"),
                    "snippet": evidence.get("snippet"),
                }
            )
            if len(out) >= MAX_RECORDS:
                break
        return out

    def _provenance_of(self, node_id: str) -> list[dict]:
        node = self.rt.store.get_node(node_id)
        if node is None:
            return []
        provenance = node.get("provenance") or {}
        first = provenance.get("first_seen") or {}
        return [
            {
                "source_document": first.get("document"),
                "source_locator": first.get("locator"),
                "modality": first.get("modality"),
                "snippet": first.get("snippet"),
            }
        ]


__all__ = ["DomainTools", "MAX_RECORDS"]
