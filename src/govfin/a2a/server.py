"""A2A 服务端：Agent Card 发现 + JSON-RPC 2.0 任务接口。

协议面按 A2A 0.2.x 实现，落在两个端点上：

- ``GET /.well-known/agent-card.json``：主控 Agent 的 Card。客户端靠它发现能力，
  因此 Card 里的 ``skills`` 要写清楚**能回答什么问题**，而不是罗列函数名。
- ``POST /``：JSON-RPC 2.0，支持 ``message/send``、``tasks/get``。

另外挂了一组 ``/agents`` 下的只读端点，用于展示完整的协作拓扑——
A2A 协议本身只要求暴露主控的 Card，但比赛的评分点是"协作拓扑"，
让人能一眼看到六个 worker 各自的口径边界，比只给一个 orchestrator 有用。

任务状态存在内存里，进程重启即丢。这对比赛演示是够的：真正的决策依据不在
任务记录里，而在图存储的 Layer3 里，那部分是持久化的。
"""

from __future__ import annotations

import argparse
import json
import threading
import uuid
from typing import Any

from govfin.a2a.topology import ALL_CARDS, ORCHESTRATOR, Orchestrator, card_for
from govfin.runtime import AgentRuntime

SERVER_VERSION = "1.0.0"


def build_app(runtime: AgentRuntime, *, base_url: str = "http://localhost:8940"):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse

    app = FastAPI(
        title="govfin-decision-agent (A2A)",
        version=SERVER_VERSION,
        description="金融+政务跨域可进化决策智能体 — A2A 协作拓扑",
    )
    orchestrator = Orchestrator(runtime)
    tasks: dict[str, dict] = {}
    tasks_lock = threading.Lock()

    # ---------------- Agent Card 发现 ----------------

    @app.get("/.well-known/agent-card.json")
    def orchestrator_card() -> dict:
        return ORCHESTRATOR.to_dict(url=base_url)

    @app.get("/agents")
    def list_agents() -> dict:
        return {
            "orchestrator": ORCHESTRATOR.name,
            "count": len(ALL_CARDS),
            "agents": [
                {
                    "name": c.name,
                    "role": c.role,
                    "description": c.description,
                    "skills": [s.id for s in c.skills],
                }
                for c in ALL_CARDS
            ],
        }

    @app.get("/agents/{name}/.well-known/agent-card.json")
    def agent_card(name: str) -> dict:
        try:
            card = card_for(name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return card.to_dict(url=f"{base_url}/agents/{name}")

    # ---------------- JSON-RPC ----------------

    @app.post("/")
    async def rpc(request: dict) -> JSONResponse:
        rpc_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        def ok(payload: Any) -> JSONResponse:
            return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": payload})

        def err(code: int, message: str) -> JSONResponse:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}
            )

        if method == "message/send":
            text = _extract_text(params)
            if not text:
                return err(-32602, "message/send 的 parts 中没有可解析的文本")
            task = _run_task(text)
            with tasks_lock:
                tasks[task["id"]] = task
            return ok(task)

        if method == "tasks/get":
            task_id = str(params.get("id") or params.get("taskId") or "")
            with tasks_lock:
                task = tasks.get(task_id)
            if task is None:
                return err(-32001, f"未知任务 {task_id!r}")
            return ok(task)

        if method == "tasks/cancel":
            task_id = str(params.get("id") or params.get("taskId") or "")
            with tasks_lock:
                task = tasks.get(task_id)
                if task is not None:
                    task["status"]["state"] = "canceled"
            if task is None:
                return err(-32001, f"未知任务 {task_id!r}")
            return ok(task)

        if method == "agent/getAuthenticatedExtendedCard":
            return ok(ORCHESTRATOR.to_dict(url=base_url))

        return err(-32601, f"不支持的方法 {method!r}")

    def _run_task(text: str) -> dict:
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        decision_id = _extract_decision_id(text)
        with runtime.lock:
            if decision_id:
                result = orchestrator.explain_decision(decision_id, task_id=task_id)
            else:
                subject = _extract_subject(text)
                result = orchestrator.assess_credit_risk(subject, task_id=task_id)
        return {
            "id": task_id,
            "kind": "task",
            "status": {"state": "completed"},
            "artifacts": [
                {
                    "artifactId": f"{task_id}-art-1",
                    "name": "orchestration-result",
                    "parts": [{"kind": "data", "data": result.to_dict()}],
                }
            ],
            "metadata": {
                "phaseReached": result.phase_reached,
                "agentsInvolved": sorted({s.agent for s in result.steps}),
                "orchestrator": ORCHESTRATOR.name,
            },
        }

    @app.get("/health")
    def health() -> dict:
        stats = runtime.stats()
        with tasks_lock:
            task_count = len(tasks)
        return {
            "status": "ok",
            "version": SERVER_VERSION,
            "graph": stats.to_dict(),
            "ontology_version": runtime.ontology.version,
            "agents": len(ALL_CARDS),
            "tasks": task_count,
        }

    @app.get("/")
    def index() -> dict:
        return {
            "service": "govfin-decision-agent A2A",
            "version": SERVER_VERSION,
            "agent_card": f"{base_url}/.well-known/agent-card.json",
            "agents": f"{base_url}/agents",
            "health": f"{base_url}/health",
            "rpc_methods": ["message/send", "tasks/get", "tasks/cancel"],
        }

    return app


def _extract_text(params: dict) -> str:
    message = params.get("message") or params
    for part in message.get("parts") or []:
        if part.get("kind") == "text" or part.get("type") == "text":
            return str(part.get("text") or "")
    if isinstance(params.get("text"), str):
        return params["text"]
    return ""


def _extract_decision_id(text: str) -> str:
    import re

    match = re.search(r"DEC-[0-9A-F]{8,}", text.upper())
    return match.group(0) if match else ""


def _extract_subject(text: str) -> str:
    """从自然语言里抠出主体。抠不到就把整句交回去，让 resolve 自己报错。

    信用代码优先于企业名。两者都出现在同一句时（"核对 91310115MA1K3XYA01
    甲科技有限公司"），信用代码是唯一键，拿名称去查可能撞上同名主体。
    这里也不能用 ``\\b`` 划边界——中文是 \\w，'查一下91310…' 里"下"与"9"
    之间不存在词边界，加了 \\b 反而一个都匹配不上。
    """
    import re

    uscc = re.search(r"([0-9A-HJ-NPQRTUWXY]{18})", text.upper())
    if uscc:
        return uscc.group(1)
    company = re.search(r"((?:[一-龥A-Za-z0-9（）()]{2,40}?)公司)", text)
    if company:
        return company.group(1)
    return text.strip()


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description="govfin A2A 服务端")
    parser.add_argument("--db", default=None)
    parser.add_argument("--in-memory", action="store_true")
    parser.add_argument("--ingest", default=None)
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8940)
    args = parser.parse_args(argv)

    runtime = AgentRuntime(db_path=args.db, in_memory=args.in_memory, use_llm=args.llm)
    if args.ingest:
        print(json.dumps(runtime.ingest_dir(args.ingest), ensure_ascii=False))
    app = build_app(runtime, base_url=f"http://localhost:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
