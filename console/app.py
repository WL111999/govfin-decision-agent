"""GovFin 控制台：把散在四个地方的运维动作收进一个页面。

**它解决的是什么问题。** 这套系统由四块组成——govfin 决策服务、本地向量化服务、
Nexent 平台、以及三者之间的注册关系。每一块单独看都还好，合起来就是：起服务要记
容器名，查状态要进数据库，注册要跑脚本，导入知识库要调接口。第一次接触的人会卡在
"我该先做什么"，而不是"这个功能怎么用"。

控制台把这四块的状态和动作摊在一个页面上，并按**你该做的顺序**排好。每一步都
显示当前状态和该按的那个按钮，不需要先读懂架构。

两个刻意的设计：

1. **前端没有构建步骤**。单页 HTML，CSS 和 JS 都内联。有构建步骤就有"构建失败了
   所以页面打不开"这种故障，而这是运维工具——它打不开的时候，恰恰是你最需要它的
   时候。
2. **能自动做的都自动做**，不能自动做的明确写出卡在哪。用户不需要判断"网络通不通"，
   页面直接告诉他。
"""

from __future__ import annotations

import json
import os
import pathlib
import urllib.error
import urllib.request
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

ROOT = pathlib.Path(__file__).resolve().parent
STATIC = ROOT / "static"

GOVFIN_MCP = os.environ.get("GOVFIN_MCP_URL", "http://govfin-agent:8930/mcp")
GOVFIN_A2A = os.environ.get("GOVFIN_A2A_URL", "http://govfin-agent:8940")
EMBEDDING_URL = os.environ.get("EMBEDDING_URL", "http://govfin-embedding:8000")
NEXENT_API = os.environ.get("NEXENT_API", "http://nexent-config:5010")
NEXENT_SUPABASE = os.environ.get("NEXENT_SUPABASE", "http://nexent-supabase-kong:8000")
NEXENT_ANON_KEY = os.environ.get("NEXENT_ANON_KEY", "")
NEXENT_EMAIL = os.environ.get("NEXENT_EMAIL", "govfin@nexent-demo.com")
NEXENT_PASSWORD = os.environ.get("NEXENT_PASSWORD", "")

app = FastAPI(title="GovFin 控制台", version="1.0.0")


# ---------------------------------------------------------------------------
# HTTP + MCP 小工具
# ---------------------------------------------------------------------------


def _http(method: str, url: str, *, headers: dict | None = None, payload: Any = None,
          body: bytes | None = None, timeout: int = 30) -> tuple[int, str]:
    data = body if body is not None else (json.dumps(payload).encode() if payload is not None else None)
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def _json_or(raw: str, default: Any = None) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return default


class McpClient:
    """极简 MCP over HTTP 客户端。

    只实现需要的三个方法（initialize / tools-list / tools-call）。引一个 MCP SDK
    进来会让控制台多一个依赖，而控制台存在的意义之一就是**依赖少、起得来**。
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.session: str | None = None

    def _post(self, payload: dict) -> dict | None:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.session:
            headers["mcp-session-id"] = self.session
        status, raw = _http("POST", self.url, headers=headers, payload=payload, timeout=120)
        if status != 200:
            return None
        if "data: " in raw:
            raw = raw.split("data: ", 1)[1].strip()
        return _json_or(raw)

    def connect(self) -> bool:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "govfin-console", "version": "1.0"}},
        }
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.session = resp.headers.get("mcp-session-id")
                resp.read()
        except Exception:  # noqa: BLE001
            return False
        if not self.session:
            return False
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return True

    def call(self, name: str, arguments: dict, *, rid: int = 2) -> Any:
        res = self._post({
            "jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        if not res or "result" not in res:
            return None
        for block in res["result"].get("content") or []:
            if block.get("type") == "text":
                return _json_or(block["text"], block["text"])
        return res["result"]


def _call_govfin(tool: str, args: dict) -> Any:
    client = McpClient(GOVFIN_MCP)
    if not client.connect():
        raise HTTPException(status_code=503, detail="连不上 govfin 决策服务（govfin-agent:8930）")
    return client.call(tool, args)


# ---------------------------------------------------------------------------
# 状态检查
# ---------------------------------------------------------------------------


def _check(name: str, url: str, *, expect_key: str | None = None) -> dict:
    status, raw = _http("GET", url, timeout=8)
    ok = status == 200
    detail = ""
    if ok and expect_key:
        data = _json_or(raw, {})
        detail = str(data.get(expect_key, "")) if isinstance(data, dict) else ""
    return {"name": name, "ok": ok, "detail": detail or (f"HTTP {status}" if not ok else "正常")}


def _nexent_token() -> str | None:
    if not (NEXENT_ANON_KEY and NEXENT_PASSWORD):
        return None
    status, raw = _http(
        "POST", f"{NEXENT_SUPABASE}/auth/v1/token?grant_type=password",
        headers={"apikey": NEXENT_ANON_KEY, "Content-Type": "application/json"},
        payload={"email": NEXENT_EMAIL, "password": NEXENT_PASSWORD},
    )
    if status != 200:
        return None
    return (_json_or(raw, {}) or {}).get("access_token")


@app.get("/api/status")
def api_status() -> dict:
    """一次把四块的状态查清楚，前端不做多次往返。

    分开查的话，页面会东一块西一块地亮起来，用户看到的是"有的绿有的灰"，
    分不清是还在加载还是真的坏了。
    """
    services = [
        _check("决策服务", f"{GOVFIN_A2A}/health", expect_key="status"),
        _check("向量化服务", f"{EMBEDDING_URL}/health", expect_key="model"),
        _check("Nexent 后端", f"{NEXENT_API}/openapi.json"),
    ]

    decision: dict = {"graph": None, "tools": 0}
    try:
        client = McpClient(GOVFIN_MCP)
        if client.connect():
            stats = client.call("graph_stats", {})
            if isinstance(stats, dict):
                decision["graph"] = stats.get("graph")
            tools = client.call("ontology_status", {}, rid=3)
            if isinstance(tools, dict):
                decision["ontology_version"] = tools.get("ontology_version")
    except Exception:  # noqa: BLE001
        pass

    nexent: dict = {"registered": False, "tools": 0, "knowledges": [], "models": []}
    token = _nexent_token()
    if token:
        auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        status, raw = _http("GET", f"{NEXENT_API}/mcp/list", headers=auth)
        if status == 200:
            servers = (_json_or(raw, {}) or {}).get("remote_mcp_server_list") or []
            for srv in servers:
                if srv.get("remote_mcp_server_name") == "govfin-decision":
                    nexent["registered"] = bool(srv.get("enabled"))
                    nexent["tools"] = len((srv.get("registry_json") or {}).get("_toolNames") or [])
        # 知识库必须带 include_stats=true，否则只回一串索引 ID（1-b32ee3...），
        # 既没有名字也没有文档数——页面上就会显示"还没有知识库"，而实际上有。
        # 这类"读法不对看起来像没有"的错最容易误导人，因为它和真的没有长得一样。
        status, raw = _http("GET", f"{NEXENT_API}/indices?include_stats=true", headers=auth)
        if status == 200:
            data = _json_or(raw, {})
            nexent["knowledges"] = [
                {"name": k.get("display_name") or k.get("name"),
                 "docs": k.get("document_count") or k.get("chunk_count") or 0,
                 "index": k.get("name")}
                for k in (data.get("indices_info") or []) if isinstance(k, dict)
            ]

        status, raw = _http("GET", f"{NEXENT_API}/model/list", headers=auth)
        if status == 200:
            items = (_json_or(raw, {}) or {}).get("data")
            nexent["models"] = [
                {"name": m.get("display_name") or m.get("model_name"),
                 "type": m.get("model_type")}
                for m in (items or []) if isinstance(m, dict)
            ]

    return {"services": services, "decision": decision, "nexent": nexent}


# ---------------------------------------------------------------------------
# 授信决策
# ---------------------------------------------------------------------------


class DecisionRequest(BaseModel):
    subject: str


@app.post("/api/decision")
def api_decision(req: DecisionRequest) -> dict:
    subject = (req.subject or "").strip()
    if not subject:
        raise HTTPException(status_code=400, detail="请填写企业名称或统一社会信用代码")

    client = McpClient(GOVFIN_MCP)
    if not client.connect():
        raise HTTPException(status_code=503, detail="连不上 govfin 决策服务")

    result: dict = {"subject": subject, "steps": []}

    lookup = client.call("gov_business_lookup", {"subject": subject}, rid=2)
    result["lookup"] = lookup
    result["steps"].append({
        "name": "真实性核验",
        "ok": bool(isinstance(lookup, dict) and lookup.get("ok")),
        "summary": (lookup or {}).get("conclusion") if isinstance(lookup, dict) else "调用失败",
    })
    if not isinstance(lookup, dict) or not lookup.get("found"):
        result["verdict"] = None
        result["halted"] = "真实性核验未通过：登记库中查无此主体，按流程终止，不产生授信结论。"
        return result

    social = client.call("gov_social_security", {"subject": subject}, rid=3)
    judicial = client.call("gov_judicial_scan", {"subject": subject}, rid=4)
    finance = client.call("fin_financial_parser", {"subject": subject}, rid=5)
    result["facts"] = {"社保": social, "司法": judicial, "财务": finance}
    result["steps"] += [
        {"name": "社保缴纳", "ok": True,
         "summary": f"异常月份 {len((social or {}).get('abnormal_records') or [])} 个"},
        {"name": "司法与处罚", "ok": True,
         "summary": f"行政处罚 {len((judicial or {}).get('penalties') or [])} 条"},
        {"name": "财务解析", "ok": True,
         "summary": f"指标 {len((finance or {}).get('metrics') or {})} 项"},
    ]

    decision = client.call("risk_decision", {"subject": subject, "persist": True}, rid=6)
    result["decision"] = decision
    if isinstance(decision, dict):
        result["verdict"] = decision.get("verdict")
        result["confidence"] = decision.get("confidence")
        result["rationale"] = decision.get("rationale")
        result["judgements"] = decision.get("judgements") or []
        result["steps"].append({
            "name": "决策合成", "ok": bool(decision.get("ok")),
            "summary": f"{decision.get('verdict')}（置信度 {decision.get('confidence')}）",
        })
        decision_id = (decision.get("provenance") or {}).get("decision_id")
        if decision_id:
            bundle = client.call("evidence_bundle", {"decision_id": decision_id}, rid=7)
            result["evidence"] = bundle
            result["steps"].append({
                "name": "依据回执", "ok": isinstance(bundle, dict),
                "summary": f"采纳链 {len((bundle or {}).get('accepted_chains') or [])} 条"
                           f"｜漂移 {(bundle or {}).get('drift_count', 0)} 项",
            })
    return result


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------


@app.get("/api/kb")
def api_kb() -> dict:
    token = _nexent_token()
    if not token:
        return {"ok": False, "error": "未配置 Nexent 凭据，无法读取知识库"}
    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    status, raw = _http("GET", f"{NEXENT_API}/indices?include_stats=true", headers=auth)
    if status != 200:
        return {"ok": False, "error": f"读取失败 HTTP {status}"}
    data = _json_or(raw, {})
    return {
        "ok": True,
        "knowledges": [
            {"name": k.get("display_name") or k.get("name"),
             "index": k.get("name"),
             "docs": k.get("document_count") or k.get("chunk_count") or 0,
             "embedding": k.get("embedding_model_name") or k.get("embedding_model")}
            for k in (data.get("indices_info") or []) if isinstance(k, dict)
        ],
    }


# ---------------------------------------------------------------------------
# 拖拽导入
# ---------------------------------------------------------------------------

# 按扩展名决定这份材料该去哪儿。两条路的用途不同：
#   图  —— 结构化事实（谁、什么时候、什么状态），用来做推理和阈值判定
#   知识库 —— 制度性文本（条款、制度、文书），用来回答"按规定该怎么办"
# 放错地方的后果不是报错，是"明明导进去了却搜不到"。
STRUCTURED_SUFFIXES = {".json", ".csv"}
TEXT_SUFFIXES = {".txt", ".pdf", ".png", ".jpg", ".jpeg"}


@app.post("/api/upload")
async def api_upload(files: list[UploadFile] = File(...)) -> dict:
    """把拖进来的文件送到该去的地方。

    路由规则简单说：**结构化数据进图，叙述性材料进知识库**。

    这个判断不能交给用户做。同一个 PDF，是"这份财报的表格数据"还是"这份制度
    文件的条款"，用途完全不同——而用户拖文件的时候心里想的是"我要用这个"，
    不是"这该进哪一层"。前端按扩展名猜，猜错了把两条路都试一遍的成本很低，
    让用户先理解架构再拖的成本很高。
    """
    results: list[dict] = []
    graph_files: list[tuple[str, bytes]] = []
    kb_files: list[tuple[str, bytes]] = []

    for upload in files:
        name = upload.filename or "unnamed"
        raw = await upload.read()
        if not raw:
            results.append({"name": name, "ok": False, "error": "空文件"})
            continue
        suffix = pathlib.Path(name).suffix.lower()
        if suffix in STRUCTURED_SUFFIXES:
            graph_files.append((name, raw))
        elif suffix in TEXT_SUFFIXES:
            kb_files.append((name, raw))
        else:
            results.append({
                "name": name, "ok": False,
                "error": f"不支持的类型 '{suffix}'，支持："
                         f"{', '.join(sorted(STRUCTURED_SUFFIXES | TEXT_SUFFIXES))}",
            })

    for name, raw in graph_files:
        results.append(_ingest_to_graph(name, raw))
    if kb_files:
        results.extend(_ingest_to_kb(kb_files))

    return {
        "ok": all(r.get("ok") for r in results) if results else False,
        "routed": {"graph": len(graph_files), "knowledge_base": len(kb_files)},
        "results": results,
    }


def _ingest_to_graph(name: str, raw: bytes) -> dict:
    import base64

    client = McpClient(GOVFIN_MCP)
    if not client.connect():
        return {"name": name, "target": "图谱", "ok": False,
                "error": "连不上 govfin 决策服务"}
    res = client.call("ingest_document", {
        "content": base64.b64encode(raw).decode("ascii"),
        "filename": name,
    }, rid=9)
    if not isinstance(res, dict):
        return {"name": name, "target": "图谱", "ok": False, "error": "工具返回异常"}
    if not res.get("ok"):
        return {"name": name, "target": "图谱", "ok": False,
                "error": res.get("error") or "导入失败"}

    delta = res.get("graph_delta") or {}
    parts = [
        f"新增 {delta.get('nodes', 0)} 节点 / {delta.get('edges', 0)} 边",
        f"{res.get('tuples', 0)} 个元组",
    ]
    if res.get("entities_merged"):
        parts.append(f"归并 {res['entities_merged']} 个实体")
    if res.get("constrained_edges"):
        parts.append(f"绑定 {res['constrained_edges']} 条规则边")
    # 被拒绝的必须报出来：报告里写着"导入成功"而实际少了个字段，
    # 和真的全部成功在界面上长得一模一样。
    if res.get("rejected_count"):
        parts.append(f"**{res['rejected_count']} 个值被拒**")
    return {
        "name": name, "target": "图谱", "ok": True,
        "detail": "，".join(parts),
        "rejected": res.get("rejected") or [],
        "graph_total": res.get("graph_total"),
    }


def _ingest_to_kb(files: list[tuple[str, bytes]]) -> list[dict]:
    token = _nexent_token()
    if not token:
        return [{"name": n, "target": "知识库", "ok": False,
                 "error": "未配置 Nexent 凭据"} for n, _ in files]

    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    status, raw = _http("GET", f"{NEXENT_API}/indices?include_stats=true", headers=auth)
    if status != 200:
        return [{"name": n, "target": "知识库", "ok": False, "error": "读不到知识库列表"} for n, _ in files]
    items = (_json_or(raw, {}) or {}).get("indices_info") or []
    index_name = next((i.get("name") for i in items if isinstance(i, dict)), None)
    if not index_name:
        return [{"name": n, "target": "知识库", "ok": False,
                 "error": "还没有知识库，请先在 Nexent 里建一个"} for n, _ in files]

    boundary = "----govfinConsoleUpload"
    parts: list[bytes] = []
    for field, value in (("index_name", index_name), ("destination", "minio"),
                         ("folder", "knowledge_base")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n'.encode())
    for name, content in files:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode()
        )
        parts.append(content)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    status, resp = _http(
        "POST", f"{NEXENT_API}/file/upload",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}",
                 "User-Agent": "AgentFrontEnd/1.0"},
        body=body, timeout=180,
    )
    if status not in (200, 201):
        return [{"name": n, "target": "知识库", "ok": False, "error": f"上传失败 HTTP {status}"} for n, _ in files]

    payload = _json_or(resp, {}) or {}
    uploaded = payload.get("uploaded_file_paths") or []
    filenames = payload.get("uploaded_filenames") or []
    records = payload.get("file_records") or []
    if not uploaded:
        return [{"name": n, "target": "知识库", "ok": False, "error": "上传返回里没有文件路径"} for n, _ in files]

    to_process = [
        {"path_or_url": path,
         "filename": filenames[i] if i < len(filenames) else pathlib.Path(path).name,
         "file_id": next((r.get("file_id") for r in records if r.get("object_name") == path), None)}
        for i, path in enumerate(uploaded)
    ]
    status, resp = _http(
        "POST", f"{NEXENT_API}/file/process",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        payload={"files": to_process, "index_name": index_name,
                 "destination": "minio", "chunking_strategy": "basic"},
        timeout=180,
    )
    if status not in (200, 201):
        return [{"name": n, "target": "知识库", "ok": False,
                 "error": f"触发切块失败 HTTP {status}"} for n, _ in files]

    return [
        {"name": n, "target": "知识库", "ok": True,
         "detail": "已上传，正在后台切块向量化（约半分钟）"}
        for n, _ in files
    ]


# ---------------------------------------------------------------------------
# 前端
# ---------------------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})
