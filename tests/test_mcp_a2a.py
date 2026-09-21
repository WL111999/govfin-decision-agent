"""协议层测试：MCP 工具契约与 A2A 编排拓扑。"""

from __future__ import annotations

import json

import pytest

from govfin.a2a.topology import ALL_CARDS, ORCHESTRATOR, WORKER_CARDS, Orchestrator, card_for

SUBJECT = "甲科技有限公司"


def _payload(result):
    """从 MCP 的 CallToolResult 里取出结构化结果。"""
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured
    for part in getattr(result, "content", None) or []:
        if getattr(part, "type", None) == "text":
            return json.loads(part.text)
    raise AssertionError(f"无法从 {result!r} 解析出结构化结果")


# ---------------------------------------------------------------- MCP


def test_mcp_registers_all_tools(runtime):
    import asyncio

    from govfin.mcp.server import build_server

    server = build_server(runtime)
    listed = asyncio.run(server.list_tools())
    names = {t.name for t in (listed[0] if isinstance(listed, tuple) else listed)}
    expected = {
        "gov_business_lookup",
        "gov_social_security",
        "gov_judicial_scan",
        "fin_financial_parser",
        "kg_path_query",
        "evidence_bundle",
        "risk_decision",
        "ontology_status",
        "ontology_evolve",
        "graph_stats",
    }
    assert expected <= names


def test_mcp_tools_never_raise_on_bad_input(runtime):
    """工具对坏输入必须返回 ok=false，而不是抛异常。

    Agent 拿到结构化失败可以换一条路继续决策；拿到 traceback 就只能放弃。
    """
    import asyncio

    from govfin.mcp.server import build_server

    server = build_server(runtime)
    cases = [
        ("gov_business_lookup", {"subject": ""}),
        ("gov_business_lookup", {"subject": "完全不存在的企业ZZZ"}),
        ("kg_path_query", {"start": SUBJECT, "constraint": "不存在的约束"}),
        ("evidence_bundle", {}),
        ("fin_financial_parser", {"subject": "完全不存在的企业ZZZ"}),
    ]
    for tool, args in cases:
        payload = _payload(asyncio.run(server.call_tool(tool, args)))
        assert payload.get("ok") is False, f"{tool}{args} 应返回 ok=false，实际 {payload}"


def test_mcp_business_lookup_returns_evidence(runtime):
    """凡返回事实的工具，每条事实都要带出处。"""
    import asyncio

    from govfin.mcp.server import build_server

    payload = _payload(
        asyncio.run(build_server(runtime).call_tool("gov_business_lookup", {"subject": SUBJECT}))
    )
    assert payload["ok"] and payload["found"]
    for record in payload["records"]:
        assert record["source_document"], "事实记录必须带来源文档"
        assert record["snippet"], "事实记录必须带证据片段"


def test_mcp_risk_decision_persists_provenance(runtime):
    import asyncio

    from govfin.mcp.server import build_server

    server = build_server(runtime)
    payload = _payload(asyncio.run(server.call_tool("risk_decision", {"subject": SUBJECT})))
    assert payload["ok"]
    assert payload["provenance"]["decision_id"] == payload["decision_id"]

    bundle = _payload(
        asyncio.run(
            server.call_tool("evidence_bundle", {"decision_id": payload["decision_id"]})
        )
    )
    assert bundle["ok"]
    assert bundle["clauses"], "证据束必须带条款原文"


# ---------------------------------------------------------------- A2A


def test_agent_cards_are_complete():
    assert len(ALL_CARDS) == 7
    names = [c.name for c in ALL_CARDS]
    assert len(names) == len(set(names))
    for card in ALL_CARDS:
        assert card.skills, f"{card.name} 必须声明 skill"
        assert card.evidence_policy, f"{card.name} 必须声明证据口径"
        for skill in card.skills:
            assert skill.description and skill.tags


def test_worker_cards_distinguish_evidence_policy():
    """各 worker 的证据口径必须互不相同，否则拆分没有意义。"""
    policies = [c.evidence_policy for c in WORKER_CARDS]
    assert len(policies) == len(set(policies)), "worker 的证据口径不应重复"


def test_card_lookup():
    assert card_for("kg-reasoning-agent").role == "reasoning"
    with pytest.raises(KeyError):
        card_for("no-such-agent")


def test_orchestrator_runs_all_four_phases(runtime):
    result = Orchestrator(runtime).assess_credit_risk(SUBJECT)
    phases = {s.phase for s in result.steps}
    assert {"检索", "推理", "决策", "溯源"} <= phases
    assert result.phase_reached == "溯源"
    assert result.decision["verdict"] == "审慎核定"
    assert result.provenance["decision_id"]


def test_orchestrator_halts_when_authenticity_fails(runtime):
    """真实性闸门未通过时，编排必须停在检索阶段并如实记录被跳过的 Agent。"""
    result = Orchestrator(runtime).assess_credit_risk("上海某钢材贸易有限公司")
    assert "真实性闸门未通过" in result.phase_reached
    assert result.decision["verdict"] == "不予受理"
    assert not result.chains, "终止后不应产生推理链"
    skipped = [s for s in result.steps if s.status == "skipped"]
    assert skipped, "被跳过的阶段要写进编排日志"


def test_orchestrator_records_failures_without_crashing(runtime):
    """单个 worker 故障不应炸掉整个任务。"""
    result = Orchestrator(runtime).assess_credit_risk("完全不存在的企业ZZZ")
    assert result.decision["verdict"] == "不予受理"
    assert result.steps, "失败路径同样要留下编排日志"
    assert all(s.status in {"ok", "empty", "failed", "skipped"} for s in result.steps)


def test_orchestrator_can_explain_decision(runtime):
    made = Orchestrator(runtime).assess_credit_risk(SUBJECT)
    decision_id = made.decision["decision_id"]
    explained = Orchestrator(runtime).explain_decision(decision_id)
    assert explained.provenance["decision_id"] == decision_id
    assert explained.provenance["report"]


def test_a2a_endpoints(runtime):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from govfin.a2a.server import build_app

    client = fastapi_testclient.TestClient(build_app(runtime))

    card = client.get("/.well-known/agent-card.json").json()
    assert card["name"] == ORCHESTRATOR.name
    assert card["skills"]

    agents = client.get("/agents").json()
    assert agents["count"] == len(ALL_CARDS)

    worker = client.get("/agents/kg-reasoning-agent/.well-known/agent-card.json").json()
    assert worker["x-govfin-role"] == "reasoning"

    assert client.get("/agents/nope/.well-known/agent-card.json").status_code == 404

    rpc = client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {"message": {"parts": [{"kind": "text", "text": f"评估 {SUBJECT} 的授信风险"}]}},
        },
    ).json()
    assert rpc["result"]["status"]["state"] == "completed"
    assert rpc["result"]["metadata"]["phaseReached"] == "溯源"
    task_id = rpc["result"]["id"]

    fetched = client.post(
        "/", json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": task_id}}
    ).json()
    assert fetched["result"]["id"] == task_id

    missing = client.post(
        "/", json={"jsonrpc": "2.0", "id": 3, "method": "tasks/get", "params": {"id": "nope"}}
    ).json()
    assert "error" in missing

    unknown = client.post(
        "/", json={"jsonrpc": "2.0", "id": 4, "method": "bogus/method", "params": {}}
    ).json()
    assert unknown["error"]["code"] == -32601


def test_subject_extraction():
    from govfin.a2a.server import _extract_decision_id, _extract_subject

    assert _extract_subject("评估 甲科技有限公司 的授信风险") == "甲科技有限公司"
    assert _extract_subject("查一下91310115MA1K3XYA01这家") == "91310115MA1K3XYA01"
    assert _extract_decision_id("回放 DEC-44043DC0AB98") == "DEC-44043DC0AB98"
    assert _extract_decision_id("没有决策号") == ""
