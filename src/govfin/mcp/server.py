"""MCP 工具服务：把领域工具暴露成 ModelEngine Nexent 可编排的 MCP tool。

三层结构在这里汇合：领域逻辑在 ``govfin.tools``，这里只负责把它注册成协议工具，
并写清楚每个工具的**使用时机**。工具的 docstring 会被 Nexus/任意 MCP 客户端
直接读给模型看，因此它是提示词的一部分，不是注释——要写"什么时候该调用我"、
"我失败时意味着什么"，而不是复述函数名。

支持三种传输（``--transport``）：

- ``stdio``：Nexent 本地接入的默认方式，由客户端拉起子进程。
- ``sse``：老版本 MCP 客户端的 HTTP 长连接。
- ``streamable-http``：MCP 1.1+ 的标准 HTTP 传输。

三种传输共用同一个 server 实例定义，避免"stdio 能跑、HTTP 少一个工具"这类
只在部署时才暴露的差异。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

from govfin.runtime import AgentRuntime
from govfin.tools import DomainTools

SERVER_NAME = "govfin-decision-agent"
SERVER_VERSION = "1.0.0"


def build_server(runtime: AgentRuntime, *, host: str = "0.0.0.0", port: int = 8930) -> Any:
    from mcp.server.mcpserver import MCPServer

    tools = DomainTools(runtime)
    server = MCPServer(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        instructions=(
            "金融+政务跨域决策智能体。提供政务侧事实核验、财务数据解析、"
            "受约束的多跳知识推理、授信决策合成与决策审计能力。\n\n"
            "典型编排顺序：gov_business_lookup 核验主体真实性 → gov_social_security / "
            "gov_judicial_scan 采集风险事实 → kg_path_query 做跨域多跳传导分析 → "
            "risk_decision 合成授信结论 → evidence_bundle 取回可追溯依据。\n"
            "**真实性核验不通过时不应继续后续步骤**，结论无依据。"
        ),
    )

    @server.tool(
        name="gov_business_lookup",
        description=(
            "工商登记核验。输入企业名称或统一社会信用代码，返回登记记录、注册资本、"
            "成立日期、经营范围，以及该主体是否登记在册的结论。\n"
            "用途：任何授信流程的**第一步**。返回 found=false 表示登记库中查无此主体，"
            "此时应终止流程而不是继续采集风险数据。"
        ),
    )
    def gov_business_lookup(subject: str) -> dict:
        return tools.gov_business_lookup(subject)

    @server.tool(
        name="gov_social_security",
        description=(
            "社保缴纳核验。返回逐月缴费记录与其中的异常月份（欠缴、断缴）。\n"
            "用途：判断用工规模真实性与经营稳定性。abnormal_count 与具体月份是"
            "'连续异常月数'类监管阈值的观测来源。"
        ),
    )
    def gov_social_security(subject: str) -> dict:
        return tools.gov_social_security(subject)

    @server.tool(
        name="gov_judicial_scan",
        description=(
            "司法与行政处罚扫描。返回行政处罚记录与涉诉案件。\n"
            "用途：条款中'存在行政处罚、重大涉诉情形'的判定依据。"
        ),
    )
    def gov_judicial_scan(subject: str) -> dict:
        return tools.gov_judicial_scan(subject)

    @server.tool(
        name="fin_financial_parser",
        description=(
            "财务数据解析。从财务报表与征信报告中取出已对齐到本体的财务指标"
            "（负债总额、营业收入、逾期次数等），负债总额与资产总额齐备时自动计算资产负债率。\n"
            "用途：偿债能力维度的观测来源，对应'资产负债率高于百分之七十'这类阈值。"
        ),
    )
    def fin_financial_parser(subject: str) -> dict:
        return tools.fin_financial_parser(subject)

    @server.tool(
        name="kg_path_query",
        description=(
            "受约束的多跳知识推理。在实体图、规则图、运行时图三层构成的属性图上，"
            "按指定业务约束搜索推理链。\n"
            "可用约束：企业真实性核验（只认政务原始数据）、关联方风险传导扫描"
            "（企业→关联主体→其政务记录→风险指标）、授信决策链生成"
            "（风险指标→风险维度→监管条款→判定阈值，跨层）、阈值判定。\n"
            "返回的每条路径自带置信度分解与被拒绝路径及理由——被拒绝的链也会返回，"
            "因为审计需要回答'为什么没采纳那条看起来相关的链'。\n"
            "truncated=true 表示展开预算耗尽，结果可能不完整，应收紧约束重试。"
        ),
    )
    def kg_path_query(
        start: str, constraint: str = "关联方风险传导扫描", max_hops: int | None = None
    ) -> dict:
        return tools.kg_path_query(start, constraint, max_hops)

    @server.tool(
        name="evidence_bundle",
        description=(
            "证据束取回。给定决策编号或主体，返回该决策的完整依据链："
            "采纳的推理链及其逐环证据（片段、来源文档、定位）、依据的条款原文、"
            "被拒绝的推理链及理由，以及**依据漂移检测**——决策作出后图上数据若已变化，"
            "会逐项列出'决策时是什么、现在是什么'。\n"
            "drift 非空不代表决策有错，表示该结论需要在今日数据下重新评估。"
        ),
    )
    def evidence_bundle(decision_id: str | None = None, subject: str | None = None) -> dict:
        return tools.evidence_bundle(decision_id, subject)

    @server.tool(
        name="risk_decision",
        description=(
            "授信决策合成。按'真实性闸门 → 关联方风险传导 → 阈值判定'的顺序推理，"
            "输出结论（建议通过/审慎核定/不予受理/证据不足）、决策置信度、"
            "每一步的中间结论，并把完整推理过程写入运行时图形成可追溯的决策链。\n"
            "结论基于监管条款的判定阈值，触发时给出条款编号与原文依据。"
        ),
    )
    def risk_decision(subject: str, persist: bool = True) -> dict:
        return tools.risk_decision(subject, persist=persist)

    @server.tool(
        name="ontology_status",
        description=(
            "本体状态查询。返回当前本体版本、类与属性规模、UNK 储备池中尚未演化"
            "的低资源概念候选及其观测次数、待人工仲裁的提案数。\n"
            "用途：判断当前本体对该领域数据的覆盖度，以及是否需要触发演化。"
        ),
    )
    def ontology_status() -> dict:
        return tools.ontology_status()

    @server.tool(
        name="ontology_evolve",
        description=(
            "触发一轮本体演化（低资源领域知识图谱本体的半自动构建）。\n"
            "流程：UNK 储备池 → 字符 n-gram 聚类 → 符号对齐（优先学别名）→ "
            "LLM 补全语义细节 → 克隆试运行一致性验证 → 置信度仲裁 → 提交并重索引。\n"
            "核心约束：是否需要演化由符号对齐决定，LLM 只能填语义细节，"
            "不能凭空造类；未通过一致性验证的提案会退回人工仲裁而不是强行提交。"
        ),
    )
    def ontology_evolve(commit: bool = True, use_llm: bool | None = None) -> dict:
        return tools.ontology_evolve(commit=commit, use_llm=use_llm)

    @server.tool(
        name="graph_stats",
        description=(
            "图谱与运行时状态。返回节点/边总数、各层规模、跨层边数量、"
            "本体版本与完整性报告。用途：调用前确认图谱已加载。"
        ),
    )
    def graph_stats() -> dict:
        store = runtime.store
        stats = store.stats()
        integrity = store.integrity_report()
        return {
            "ok": True,
            "graph": stats,
            "integrity": {
                "healthy": integrity.get("healthy"),
                "issues": integrity.get("issues", [])[:10],
            },
            "ontology_version": runtime.ontology.version,
        }

    @server.tool(
        name="ingest_document",
        description=(
            "导入一份材料到知识图谱。接受 JSON（政务记录数组）、CSV（表格）、"
            "TXT（正文）、PDF 与营业执照图片。\n"
            "用途：把新出现的材料变成图上可推理的事实。导入后同一批里的实体"
            "会自动归并、规则会自动绑定。\n"
            "返回的 report 里 **skipped 不为空表示有文件没读进去**——那些材料"
            "在图上不存在，后续推理不会用到它们，必须让调用方看见。"
        ),
    )
    def ingest_document(
        content: str,
        filename: str,
        encoding: str = "utf-8",
    ) -> dict:
        """把一份材料写进图。

        content 是文件内容的 base64。为什么用 base64 而不是纯文本：这个工具要收
        的不只是文本——营业执照是二进制图片，财务报表皮是 PDF。用 JSON 传二进制的
        常规做法就是 base64，而 MCP 的参数本来就是 JSON。

        这里刻意做成"一次一份"而不是"一次一批"：批量的失败定位成本高得多——
        一份坏文件混在里面，调用方只知道"这批失败了"，不知道是哪一份、为什么。
        """
        import base64
        import binascii

        from govfin.ingest.parsers import parse_bytes

        try:
            raw = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            return {"ok": False, "error": f"content 不是合法的 base64: {exc}"}

        suffix = pathlib.Path(filename).suffix.lower() or ".txt"
        if suffix not in (".json", ".csv", ".txt", ".pdf", ".png", ".jpg", ".jpeg"):
            return {
                "ok": False,
                "error": f"不支持的文件类型 '{suffix}'",
                "supported": [".json", ".csv", ".txt", ".pdf", ".png", ".jpg", ".jpeg"],
            }

        try:
            doc = parse_bytes(raw, source=filename, suffix=suffix)
        except Exception as exc:  # noqa: BLE001 - 前端拿到原因比拿到 traceback 有用
            return {"ok": False, "error": f"解析失败：{type(exc).__name__}: {exc}"}

        before = runtime.store.stats()
        try:
            result = runtime.extractor.extract(doc)
            report = runtime.loader.load(result)
            consolidation = runtime.loader.consolidate()
            binding = runtime.binder.bind()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"装载失败：{type(exc).__name__}: {exc}"}

        after = runtime.store.stats()
        binding_dict = binding.to_dict() if hasattr(binding, "to_dict") else {}
        return {
            "ok": True,
            "filename": filename,
            "modality": doc.modality,
            "blocks": len(doc.blocks),
            "tuples": len(result.tuples),
            "nodes_created": report.nodes_created,
            "edges_created": report.edges_created,
            "entities_merged": consolidation.merged_count,
            "tuples_routed_to_unk": report.tuples_routed_to_unk,
            "rejected": report.rejected[:10],
            "rejected_count": len(report.rejected),
            "constrained_edges": binding_dict.get("constrained_edges", 0),
            "graph_delta": {
                "nodes": after.get("nodes", 0) - before.get("nodes", 0),
                "edges": after.get("edges", 0) - before.get("edges", 0),
            },
            "graph_total": {"nodes": after.get("nodes", 0), "edges": after.get("edges", 0)},
        }

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="govfin MCP 工具服务")
    parser.add_argument(
        "--transport", default="stdio", choices=["stdio", "sse", "streamable-http"]
    )
    parser.add_argument("--db", default=None, help="图数据库路径；缺省用配置值")
    parser.add_argument("--in-memory", action="store_true", help="临时内存图，退出即丢")
    parser.add_argument("--ingest", default=None, help="启动前先导入该目录下的文档")
    parser.add_argument("--llm", action="store_true", help="启用 LLM（本体演化提案等）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8930)
    args = parser.parse_args(argv)

    runtime = AgentRuntime(
        db_path=args.db, in_memory=args.in_memory, use_llm=args.llm
    )
    if args.ingest:
        summary = runtime.ingest_dir(args.ingest)
        print(json.dumps(summary, ensure_ascii=False), file=sys.stderr)

    server = build_server(runtime, host=args.host, port=args.port)
    kwargs: dict = {}
    if args.transport != "stdio":
        kwargs = {"host": args.host, "port": args.port}
    server.run(args.transport, **kwargs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
