"""决策与溯源测试：结论正确性、置信度不虚高、冻结快照、漂移检测。"""

from __future__ import annotations

import pytest

from govfin.reasoning.audit import AuditTrail
from govfin.reasoning.decision import VERDICT_REJECTED, VERDICT_REVIEW, DecisionSynthesizer
from govfin.reasoning.provenance import FROZEN_KEY, ProvenanceRecorder

SUBJECT = "甲科技有限公司"


@pytest.fixture(scope="module")
def synthesized(runtime):
    node = runtime.store.find_by_prop("名称", SUBJECT, ntype="企业")[0]
    decision = DecisionSynthesizer(runtime.store).synthesize(node["id"])
    return decision


def test_decision_triggers_threshold(synthesized):
    assert synthesized.verdict == VERDICT_REVIEW
    assert synthesized.triggered, "社保连续异常应触发监管阈值"
    judgement = synthesized.triggered[0]
    assert judgement.observed_value >= judgement.threshold_value
    assert judgement.clause_node, "触发结论必须回指到具体条款"


def test_decision_confidence_not_saturated(synthesized):
    """置信度不能被路径数量顶到 1.0。

    同一个结论往往有十几条走法，它们是同一份证据的不同路线，不是互相印证。
    早期版本把全部采纳路径喂进噪声或合成，得到 0.9994 —— 结论看起来铁证如山，
    实际依据只有一份社保记录加一条监管条款。
    """
    assert 0.0 < synthesized.confidence < 0.95


def test_decision_halted_without_registration(runtime):
    """真实性闸门：登记查无此主体时必须终止，不产生后续结论。"""
    node = runtime.store.find_by_prop("名称", "上海某钢材贸易有限公司", ntype="企业")[0]
    decision = DecisionSynthesizer(runtime.store).synthesize(node["id"])
    assert decision.verdict == VERDICT_REJECTED
    assert decision.halted_at == "企业真实性核验"
    assert len(decision.steps) == 1, "终止后不应产生后续步骤"


def test_provenance_freezes_evidence(runtime, synthesized):
    recorder = ProvenanceRecorder(runtime.store)
    report = recorder.record(synthesized)
    assert report.decision_node
    assert report.path_nodes, "应记录采纳路径"

    frozen = runtime.store.get_node(report.path_nodes[0])["props"].get(FROZEN_KEY)
    assert isinstance(frozen, dict)
    assert frozen["nodes"], "冻结快照必须包含节点快照"
    assert frozen["edges"], "冻结快照必须包含逐环证据"
    assert frozen["confidence_breakdown"]["edge_weights"]


def _abnormal_record(store):
    return next(
        n for n in store.query_nodes(ntype="社保缴纳记录", limit=None)
        if n["props"].get("缴纳状态") in ("断缴", "欠缴")
    )


def test_provenance_survives_upstream_mutation(mutable_runtime):
    """图上的原始数据被改写后，冻结快照必须原样保留。"""
    node = mutable_runtime.store.find_by_prop("名称", SUBJECT, ntype="企业")[0]
    decision = DecisionSynthesizer(mutable_runtime.store).synthesize(node["id"])
    recorder = ProvenanceRecorder(mutable_runtime.store)
    report = recorder.record(decision)

    before = mutable_runtime.store.get_node(report.path_nodes[0])["props"][FROZEN_KEY]
    target = _abnormal_record(mutable_runtime.store)
    mutable_runtime.store.update_node_props(target["id"], {"缴纳状态": "正常"})
    after = mutable_runtime.store.get_node(report.path_nodes[0])["props"][FROZEN_KEY]
    assert after == before, "冻结快照不该随上游数据变化"


def test_drift_detection(mutable_runtime):
    node = mutable_runtime.store.find_by_prop("名称", SUBJECT, ntype="企业")[0]
    decision = DecisionSynthesizer(mutable_runtime.store).synthesize(node["id"])
    ProvenanceRecorder(mutable_runtime.store).record(decision)

    target = _abnormal_record(mutable_runtime.store)
    mutable_runtime.store.update_node_props(target["id"], {"缴纳状态": "正常"})

    drift = AuditTrail(mutable_runtime.store).detect_drift(decision.decision_id)
    assert drift, "上游变化应被检出"
    assert any(d["prop"] == "缴纳状态" for d in drift)
    keys = [(d["node"], d["prop"]) for d in drift]
    assert len(keys) == len(set(keys)), "同一变化不应重复报告"


def test_audit_report_renders(runtime, synthesized):
    ProvenanceRecorder(runtime.store).record(synthesized)
    report = AuditTrail(runtime.store).trace(synthesized.decision_id)
    text = report.render()
    assert synthesized.decision_id in text
    assert report.steps and report.accepted_chains
    assert report.clauses, "审计报告必须带条款原文"


def test_reverse_lookup_by_clause(runtime, synthesized):
    """反向查询：引用某条款的决策有哪些。监管问询的高频问题。"""
    ProvenanceRecorder(runtime.store).record(synthesized)
    clause = synthesized.judgements[0].clause_node
    found = AuditTrail(runtime.store).decisions_referencing(clause)
    assert any(d["decision_id"] == synthesized.decision_id for d in found)


def test_audit_unknown_decision_raises(runtime):
    with pytest.raises(KeyError):
        AuditTrail(runtime.store).trace("DEC-DOES-NOT-EXIST")
