"""命令行入口：``govfin <子命令>``。

存在的理由有三个，缺一个都不值得单独写一个 CLI：

1. **容器构建期要跑导入**。Dockerfile 里需要一条能在镜像构建阶段把样例数据
   灌进图数据库的命令。把这段逻辑塞进 shell 脚本会让它脱离类型检查和测试。
2. **部署后要能自检**。容器起来后第一件事是 ``govfin doctor``——确认图非空、
   本体版本对、MCP 工具注册数正确。没有这条命令，出问题只能靠翻日志。
3. **演示要能一条命令跑通全流程**。评委不会去读 12 个 python -c。

所有子命令都是薄封装：真正的逻辑在 ``AgentRuntime`` 和各层模块里，
这里只负责参数解析、输出格式和退出码。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

EXIT_OK = 0
EXIT_FAILED = 1


def _out(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _runtime(args: argparse.Namespace):
    from govfin.runtime import AgentRuntime

    return AgentRuntime(
        db_path=getattr(args, "db", None),
        in_memory=getattr(args, "in_memory", False),
        use_llm=getattr(args, "llm", False),
    )


# ----------------------------------------------------------------------
# 子命令
# ----------------------------------------------------------------------


def cmd_ingest(args: argparse.Namespace) -> int:
    """把目录下的多模态文档全量导入并绑定规则。

    导入是**追加**语义，不是覆盖。重复执行会把同一份文档再加一遍——
    图的节点层有 ``_ensure_node`` 去重（按提及文本），所以节点不会翻倍，
    但边会。需要干净重来时先删掉 ``GOVFIN_GRAPH_DB`` 指向的文件。
    """
    rt = _runtime(args)
    summary = rt.ingest_dir(args.data)
    summary["graph"] = rt.stats().to_dict()
    _out(summary)
    rt.close()
    return EXIT_OK if summary["documents"] else EXIT_FAILED


def cmd_stats(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    payload = {
        "graph": rt.stats().to_dict(),
        "integrity": rt.store.integrity_report(),
        "ontology": {
            "version": rt.ontology.version,
            "classes": len(getattr(rt.ontology, "classes", {}) or {}),
        },
    }
    _out(payload)
    rt.close()
    return EXIT_OK


def cmd_ask(args: argparse.Namespace) -> int:
    """对单个主体跑一次完整授信决策。"""
    from govfin.tools import DomainTools

    rt = _runtime(args)
    result = DomainTools(rt).risk_decision(args.subject, persist=not args.no_persist)
    if args.brief:
        result = {
            "ok": result.get("ok"),
            "subject": result.get("subject", {}).get("label"),
            "verdict": result.get("verdict"),
            "confidence": result.get("confidence"),
            "judgements": [
                {
                    "threshold": j.get("threshold_name"),
                    "observed": j.get("observed_value"),
                    "triggered": j.get("triggered"),
                    "clause": j.get("clause_label"),
                }
                for j in result.get("judgements", [])
            ],
            "decision_id": (result.get("provenance") or {}).get("decision_id"),
        }
    _out(result)
    ok = bool(result.get("ok"))
    rt.close()
    return EXIT_OK if ok else EXIT_FAILED


def cmd_replay(args: argparse.Namespace) -> int:
    """按决策编号还原一条决策的完整推理链——审计入口。"""
    from govfin.reasoning.audit import AuditTrail

    rt = _runtime(args)
    trail = AuditTrail(rt.store)
    try:
        report = trail.trace(args.decision_id)
    except KeyError:
        print(f"未找到决策 {args.decision_id}", file=sys.stderr)
        rt.close()
        return EXIT_FAILED
    if args.json:
        _out(report.to_dict() if hasattr(report, "to_dict") else report.render())
    else:
        print(report.render())
    drift = trail.detect_drift(args.decision_id)
    if drift:
        print("\n--- 依据漂移 ---", file=sys.stderr)
        for item in drift:
            print(
                f"  {item['node']}.{item['prop']}: "
                f"决策时 {item.get('frozen')!r} → 现在 {item.get('current')!r}",
                file=sys.stderr,
            )
    rt.close()
    return EXIT_OK


def cmd_evolve(args: argparse.Namespace) -> int:
    """跑一轮本体演化。

    ``--dry-run`` 走的是同一条管道，只是最后不提交——因此它验证的是真实的
    提案与一致性检查过程，不是模拟。未通过验证的提案进去多少还是多少，
    会被退回人工仲裁而不是丢弃。
    """
    rt = _runtime(args)
    report = rt.pipeline.run_cycle(commit=not args.dry_run)
    payload = report.to_dict() if hasattr(report, "to_dict") else report
    _out(payload)
    rt.close()
    return EXIT_OK


def _probe_decision_path(rt) -> dict:
    """跑一次**不落盘**的决策，验证整条推理通路可用。

    ``persist=False``：自检不该在图里留下痕迹，否则每次重启都会多出一批
    以自检主体为对象的假决策，污染后续的审计与漂移检测。
    """
    from govfin.reasoning.decision import DecisionSynthesizer

    subject = rt.store.query_nodes(ntype="企业", limit=1)
    if not subject:
        return {"name": "决策通路", "ok": False, "detail": "图中没有企业节点，无法验证"}
    node = subject[0]
    try:
        decision = DecisionSynthesizer(rt.store, engine=rt.engine).synthesize(node["id"])
    except Exception as exc:  # noqa: BLE001 - 自检要报告原因而不是自己崩掉
        return {"name": "决策通路", "ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {
        "name": "决策通路",
        "ok": bool(decision.verdict),
        "detail": (
            f"{node['label']} → {decision.verdict}"
            f"（置信度 {decision.confidence:.4f}，"
            f"{len(decision.accepted_paths)} 条采纳路径 / "
            f"{len(decision.judgements)} 条阈值判定）"
        ),
    }


def cmd_doctor(args: argparse.Namespace) -> int:
    """部署自检。返回非零退出码即表示不该接流量。

    检查项刻意选的是"错了会静默出错"的那些——图空了会返回空结论而不是报错，
    本体版本不匹配会让推理走到不存在的类上，MCP 工具少注册一个会让编排
    在运行到那一步时才失败。这些在日志里都看不出来，只能主动查。
    """
    from govfin.config import get_settings

    rt = _runtime(args)
    checks: list[dict] = []

    stats = rt.stats().to_dict()
    checks.append(
        {
            "name": "图谱非空",
            "ok": stats["nodes"] > 0 and stats["edges"] > 0,
            "detail": f"{stats['nodes']} 节点 / {stats['edges']} 边",
        }
    )

    from govfin.graph.schema import LAYER_ENTITY, LAYER_RULE, LAYER_NAMES

    layers = stats.get("layers") or {}
    # by_layer 是 {层名: {"nodes": n, "edges": e}}——按名字索引，不是 id。
    # 实体图与规则图缺任何一个，跨层推理都会在某条路径上静默失败，因此是硬性检查。
    for layer_id in (LAYER_ENTITY, LAYER_RULE):
        label = LAYER_NAMES[layer_id]
        bucket = layers.get(label) or {}
        size = bucket.get("nodes", 0)
        checks.append(
            {
                "name": f"{label}有内容",
                "ok": size > 0,
                "detail": f"{size} 节点 / {bucket.get('edges', 0)} 边",
            }
        )

    # 运行时图为空是**正常的**：它记录的是决策痕迹，只有跑过决策才会有内容。
    # 把它当作硬性故障会让每一次全新部署都自检失败。
    runtime_layer = layers.get(LAYER_NAMES[3]) or {}
    checks.append(
        {
            "name": "运行时图",
            "ok": True,
            "detail": (
                f"{runtime_layer.get('nodes', 0)} 节点"
                if runtime_layer
                else "尚无决策痕迹（首次决策后填充，属正常状态）"
            ),
        }
    )

    # 真正有价值的自检：跑一遍决策通路。它一次性覆盖路径搜索、置信度传播、
    # 阈值判定与结论合成——比逐个检查组件的存在性更能说明部署是否可用。
    checks.append(_probe_decision_path(rt))

    integrity = rt.store.integrity_report()
    checks.append(
        {
            "name": "图完整性",
            "ok": bool(integrity.get("healthy")),
            "detail": "; ".join(map(str, integrity.get("issues", [])[:3])) or "无异常",
        }
    )

    try:
        from govfin.mcp.server import build_server

        server = build_server(rt)
        import asyncio

        tool_names = [t.name for t in asyncio.run(server.list_tools())]
        checks.append(
            {
                "name": "MCP 工具注册",
                "ok": len(tool_names) >= 10,
                "detail": f"{len(tool_names)} 个: {', '.join(sorted(tool_names))}",
            }
        )
    except Exception as exc:  # noqa: BLE001 - 自检要报告失败原因而不是自己崩掉
        checks.append({"name": "MCP 工具注册", "ok": False, "detail": f"{type(exc).__name__}: {exc}"})

    settings = get_settings()
    usable = settings.llm.usable
    checks.append(
        {
            "name": "LLM 通道",
            "ok": True,  # 没配 key 不算故障——符号路径本就是设计内的降级方案
            "detail": (
                f"{settings.llm.provider}/{settings.llm.model} 已配置"
                if usable
                else "未配置，本体演化将走纯符号路径（设计内降级）"
            ),
        }
    )

    failed = [c for c in checks if not c["ok"]]
    _out({"healthy": not failed, "checks": checks})
    rt.close()
    return EXIT_OK if not failed else EXIT_FAILED


# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="govfin",
        description="金融+政务跨域可进化决策智能体",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  govfin ingest --data data\n"
            "  govfin doctor\n"
            "  govfin ask 91310115MA1K3XYA01 --brief\n"
            "  govfin replay DEC-322EEB4768D5\n"
            "  govfin evolve --dry-run\n"
        ),
    )
    parser.add_argument("--db", default=None, help="图数据库路径；缺省用 GOVFIN_GRAPH_DB")
    parser.add_argument("--in-memory", action="store_true", help="用临时内存图，退出即丢")
    parser.add_argument("--llm", action="store_true", help="启用 LLM 通道")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="导入目录下的多模态文档")
    p.add_argument("--data", default=None, help="数据目录；缺省用 GOVFIN_DATA_DIR")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("stats", help="打印图谱统计与完整性报告")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("ask", help="对单个主体跑一次授信决策")
    p.add_argument("subject", help="企业名称或统一社会信用代码")
    p.add_argument("--brief", action="store_true", help="只输出结论要点")
    p.add_argument("--no-persist", action="store_true", help="不写入运行时图（不留审计痕迹）")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("replay", help="按决策编号还原推理链")
    p.add_argument("decision_id", help="形如 DEC-XXXXXXXXXXXX")
    p.add_argument("--json", action="store_true", help="输出 JSON 而非可读文本")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("evolve", help="跑一轮本体演化")
    p.add_argument("--dry-run", action="store_true", help="只出提案，不提交")
    p.set_defaults(func=cmd_evolve)

    p = sub.add_parser("doctor", help="部署自检")
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底，给出可读错误而非 traceback
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
