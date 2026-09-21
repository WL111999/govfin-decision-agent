"""推理层测试：约束路径、置信度模型、决策合成、溯源与漂移。"""

from __future__ import annotations

import pytest

from govfin.graph.confidence import ConfidenceModel
from govfin.reasoning.constraints import AUTHENTICITY, CONTAGION, CREDIT_CHAIN, THRESHOLD_CHAIN
from govfin.reasoning.path import PathEngine

SUBJECT = "甲科技有限公司"


@pytest.fixture(scope="module")
def engine(runtime):
    return PathEngine(runtime.store)


def test_authenticity_accepts_registration(engine):
    node = engine.store.find_by_prop("名称", SUBJECT, ntype="企业")[0]
    result = engine.search(node["id"], AUTHENTICITY)
    assert result.paths, "工商登记核验应命中登记记录"
    assert all(p.confidence.score <= 1.0 for p in result.paths)


def test_authenticity_excludes_llm_edges(engine):
    """真实性约束的证据口径必须把 LLM 边排除在外，否则口径形同虚设。"""
    assert AUTHENTICITY.evidence_classes == ("direct",)


def test_contagion_reaches_risk_indicator(runtime):
    engine = PathEngine(runtime.store)
    node = runtime.store.find_by_prop("名称", SUBJECT, ntype="企业")[0]
    result = engine.search(node["id"], CONTAGION)
    assert result.paths, "关联方传导应能走到风险指标"
    for path in result.paths:
        assert path.node_snapshots[-1]["ntype"] == "风险指标"
        assert "触发风险信号" in {e["etype"] for e in path.edges}


def test_credit_chain_crosses_layers(runtime):
    """授信决策链必须真的跨层：起点在实体图，终点落在规则图上。"""
    engine = PathEngine(runtime.store)
    indicator = runtime.store.query_nodes(ntype="风险指标", limit=1)[0]
    result = engine.search(indicator["id"], CREDIT_CHAIN)
    assert result.paths, "应能生成授信决策链"
    path = result.paths[0]
    assert path.cross_layer_count >= 1
    assert 1 in path.layers_visited and 2 in path.layers_visited
    assert path.node_snapshots[-1]["ntype"] == "阈值"


def test_threshold_chain_extracts_boundary(runtime):
    """阈值判定链要取到判定边界本身。

    起点必须选**确实带阈值**的条款——不是每条监管条款都规定了可量化的边界，
    "应当穿透识别实际控制人"这类要求没有数值阈值，取不到是正确行为。
    """
    engine = PathEngine(runtime.store)
    clauses = [
        c
        for c in runtime.store.query_nodes(ntype="授信指引条款", limit=None)
        if runtime.store.find_edges(src=c["id"], etype="设定阈值")
    ]
    assert clauses, "样例数据中应至少有一条带阈值的条款"
    for clause in clauses:
        result = engine.search(clause["id"], THRESHOLD_CHAIN)
        assert result.paths, f"{clause['label']} 应能解出阈值判定链"
        threshold = result.paths[0].node_snapshots[-1]
        assert threshold["ntype"] == "阈值"
        assert isinstance(threshold["props"].get("阈值"), (int, float))


def test_threshold_chain_empty_for_clause_without_threshold(runtime):
    """没有数值阈值的条款取不到边界，应当是空结果而不是编造一个阈值。"""
    engine = PathEngine(runtime.store)
    clauses = [
        c
        for c in runtime.store.query_nodes(ntype="授信指引条款", limit=None)
        if not runtime.store.find_edges(src=c["id"], etype="设定阈值")
    ]
    if not clauses:
        pytest.skip("样例数据中所有条款都带阈值")
    result = engine.search(clauses[0]["id"], THRESHOLD_CHAIN)
    assert not result.paths
    assert not result.truncated


def test_rejected_paths_are_retained(runtime):
    """低置信度路径必须保留：审计要能回答"为什么没采纳那条看起来相关的链"。"""
    engine = PathEngine(runtime.store, confidence=ConfidenceModel())
    node = runtime.store.find_by_prop("名称", SUBJECT, ntype="企业")[0]
    result = engine.search(node["id"], AUTHENTICITY)
    payload = result.to_dict()
    assert "rejected_paths" in payload
    for rejected in payload["rejected_paths"]:
        assert rejected["reject_reason"], "被拒路径必须带拒绝原因"


def test_expansion_budget_reports_truncation(runtime):
    """预算耗尽要如实上报，不能静默返回残缺结果。"""
    from dataclasses import replace

    engine = PathEngine(runtime.store)
    node = runtime.store.find_by_prop("名称", SUBJECT, ntype="企业")[0]
    tight = replace(CONTAGION, expansion_budget=3)
    result = engine.search(node["id"], tight)
    assert result.truncated is True
    assert "预算" in result.truncated_reason


def test_confidence_monotonic_in_quality():
    """置信度必须随证据质量单调：直接证据链应高于掺入 LLM 边的同构链。"""
    model = ConfidenceModel()
    direct = [
        {"id": "e1", "etype": "拥有工商登记", "confidence": 0.95, "evidence_class": "direct"},
        {"id": "e2", "etype": "缴纳社保", "confidence": 0.95, "evidence_class": "direct"},
    ]
    llm = [
        {"id": "e1", "etype": "拥有工商登记", "confidence": 0.95, "evidence_class": "direct"},
        {"id": "e2", "etype": "缴纳社保", "confidence": 0.95, "evidence_class": "llm"},
    ]
    assert model.path_confidence(direct).score > model.path_confidence(llm).score


def test_confidence_penalises_bottleneck():
    """瓶颈保护要能区分"一个环节不可信"和"整体都一般"。"""
    model = ConfidenceModel()
    lopsided = [
        {"id": "a", "etype": "r", "confidence": 0.99, "evidence_class": "direct"},
        {"id": "b", "etype": "r", "confidence": 0.2, "evidence_class": "direct"},
    ]
    even = [
        {"id": "a", "etype": "r", "confidence": 0.45, "evidence_class": "direct"},
        {"id": "b", "etype": "r", "confidence": 0.45, "evidence_class": "direct"},
    ]
    assert model.path_confidence(lopsided).score < model.path_confidence(even).score


def test_cycle_avoidance(runtime):
    """默认禁环：带环的路径在置信度传播里没有意义。"""
    engine = PathEngine(runtime.store)
    assert CONTAGION.allow_cycles is False
    node = runtime.store.find_by_prop("名称", SUBJECT, ntype="企业")[0]
    result = engine.search(node["id"], CONTAGION)
    for path in result.paths:
        assert len(path.node_ids) == len(set(path.node_ids)), "路径中不应出现重复节点"
