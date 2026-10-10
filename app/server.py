"""桌面应用的后端：复用控制台的全部接口，再挂上桌面特有的三组。

**复用而不是重写。** `console/app.py` 里的 `/api/status`、`/api/decision`、
`/api/kb`、`/api/upload` 都是好的，重写一遍只会多出一份要同步维护的代码。
桌面与控制台的差别只有两点：服务地址从容器名换成本机端口，以及多出来的功能。

**为什么环境变量必须在 import 之前设。** `console/app.py` 在模块级读这些变量
（`GOVFIN_MCP_URL = os.environ.get(...)`），import 完再设就晚了——那时候常量
已经定好。所以下面这段 `os.environ.setdefault(...)` 必须在 `import` 之上，
这也是本文件唯一一处顺序敏感的地方。
"""

from __future__ import annotations

import os
import pathlib
import sys
import time

# ---------------------------------------------------------------------------
# 顺序敏感：先设环境变量，再 import 控制台
# ---------------------------------------------------------------------------

# 用 setdefault 而不是直接赋值：容器里跑的时候这些变量已经由 Docker 注入了，
# 桌面端不该覆盖它们。（同一个模块两处用，得照顾两边。）
for _key, _value in (
    ("GOVFIN_MCP_URL", "http://localhost:8930/mcp"),
    ("GOVFIN_A2A_URL", "http://localhost:8940"),
    ("EMBEDDING_URL", "http://localhost:8070"),
    ("NEXENT_API", "http://localhost:5010"),
    ("NEXENT_SUPABASE", "http://localhost:8000"),
    # 桌面端把地址指向本机，所以 Supabase 的匿名 key 只能从 Nexent 的 .env 现读，
    # 没法像容器那样由编排注入。
    ("NEXENT_ANON_KEY", ""),
):
    os.environ.setdefault(_key, _value)

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi import Body, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import paths  # noqa: E402
import deploy as deploy_mod  # noqa: E402

# 复用控制台的 app 对象本身——它的路由全都留在上面
from console.app import (  # noqa: E402
    app,
    _http,
    _json_or,
    _nexent_token,
    McpClient,
    GOVFIN_MCP,
    NEXENT_API,
    NEXENT_SUPABASE,
)

# ---------------------------------------------------------------------------
# 单步执行：七个独立按钮的后端
#
# 每一步映射到一个 MCP 工具。这张表必须与 `src/govfin/mcp/server.py` 里实际注册的
# 工具名和参数名一致——不一致的后果不是报错，是"这一步永远返回空"，而
# "工具调用失败"和"这家企业确实没有数据"在界面上长得一样。
# 所以 tests/test_desktop_app.py 里有一条测试专门核对这张表。
# ---------------------------------------------------------------------------

STEPS: dict[str, dict] = {
    "lookup": {
        "tool": "gov_business_lookup",
        "label": "真实性核验",
        "icon": "🔍",
        "args": lambda subject, decision_id: {"subject": subject},
        "needs": ("subject",),
        "desc": "核验主体登记在册，授信流程的第一道闸门",
    },
    "social": {
        "tool": "gov_social_security",
        "label": "社保缴纳",
        "icon": "👥",
        "args": lambda subject, decision_id: {"subject": subject},
        "needs": ("subject",),
        "desc": "逐月缴费记录与异常月份，判断用工规模真实性",
    },
    "judicial": {
        "tool": "gov_judicial_scan",
        "label": "司法与处罚",
        "icon": "⚖",
        "args": lambda subject, decision_id: {"subject": subject},
        "needs": ("subject",),
        "desc": "行政处罚与涉诉事实",
    },
    "finance": {
        "tool": "fin_financial_parser",
        "label": "财务解析",
        "icon": "📊",
        "args": lambda subject, decision_id: {"subject": subject},
        "needs": ("subject",),
        "desc": "已对齐到本体的财务指标，资产负债率自动计算",
    },
    "graph": {
        "tool": "kg_path_query",
        "label": "跨域推理",
        "icon": "🕸",
        "args": lambda subject, decision_id: {
            "start": subject, "constraint": "关联方风险传导扫描", "max_hops": 3,
        },
        "needs": ("subject",),
        "desc": "政务异常如何沿关联关系传导到金融侧",
    },
    "decide": {
        "tool": "risk_decision",
        "label": "决策合成",
        "icon": "⚡",
        "args": lambda subject, decision_id: {"subject": subject, "persist": True},
        "needs": ("subject",),
        "desc": "阈值判定与置信度衰减，产出可追溯结论",
    },
    "evidence": {
        "tool": "evidence_bundle",
        "label": "依据回执",
        "icon": "📜",
        "args": lambda subject, decision_id: {"decision_id": decision_id},
        "needs": ("decision_id",),
        "desc": "逐环证据、条款原文与依据漂移检测",
    },
}

STEP_ORDER = ("lookup", "social", "judicial", "finance", "graph", "decide", "evidence")


class StepRequest(BaseModel):
    step: str
    subject: str = ""
    decision_id: str = ""


@app.post("/api/step")
def api_step(req: StepRequest) -> dict:
    """只跑一步。

    独立执行是有意的：用户可能只想看某家企业的行政处罚，不想走完整决策链。
    强制按顺序走完七步才能看一个数，是把系统的流程强加给用户的问题。
    """
    spec = STEPS.get((req.step or "").strip())
    if spec is None:
        # 结构化错误而不是 500：前端拿到"哪一步不认识"能给出可读提示，
        # 拿到一个栈回溯只能显示"服务器错误"。
        raise HTTPException(status_code=400, detail={
            "error": f"未知的步骤 '{req.step}'",
            "available": list(STEP_ORDER),
        })

    subject = (req.subject or "").strip()
    decision_id = (req.decision_id or "").strip()
    missing = [n for n in spec["needs"] if not (subject if n == "subject" else decision_id)]
    if missing:
        readable = {"subject": "企业名称", "decision_id": "决策编号（先执行「决策合成」）"}
        raise HTTPException(status_code=400, detail={
            "error": "缺少参数：" + "、".join(readable[m] for m in missing),
            "needs": missing,
        })

    client = McpClient(GOVFIN_MCP)
    if not client.connect():
        raise HTTPException(status_code=503, detail={
            "error": "连不上决策服务（localhost:8930）",
            "hint": "服务可能还没部署或没启动，去「部署与配置」页看看",
        })

    started = time.time()
    data = client.call(spec["tool"], spec["args"](subject, decision_id), rid=11)
    elapsed = int((time.time() - started) * 1000)

    if data is None:
        return {
            "ok": False, "step": req.step, "tool": spec["tool"], "elapsed_ms": elapsed,
            "error": "工具没有返回结果（连接中断或超时）",
        }
    if isinstance(data, dict) and data.get("ok") is False:
        return {
            "ok": False, "step": req.step, "tool": spec["tool"], "elapsed_ms": elapsed,
            "data": data, "error": str(data.get("error") or "工具返回失败"),
        }
    return {"ok": True, "step": req.step, "tool": spec["tool"], "elapsed_ms": elapsed, "data": data}


@app.get("/api/steps")
def api_steps() -> dict:
    """步骤清单。前端据此渲染按钮，不在页面里写死名字。"""
    return {
        "order": list(STEP_ORDER),
        "steps": [
            {"id": key, "label": STEPS[key]["label"], "icon": STEPS[key]["icon"],
             "tool": STEPS[key]["tool"], "desc": STEPS[key]["desc"],
             "needs": list(STEPS[key]["needs"])}
            for key in STEP_ORDER
        ],
    }


# ---------------------------------------------------------------------------
# 项目目录
# ---------------------------------------------------------------------------


class ProjectRequest(BaseModel):
    path: str


@app.get("/api/project")
def api_project_get() -> dict:
    project = paths.resolve_project()
    return {"path": str(project) if project else None, "frozen": paths.is_frozen()}


@app.post("/api/project")
def api_project_set(req: ProjectRequest) -> dict:
    candidate = pathlib.Path(req.path)
    if not paths.looks_like_project(candidate):
        raise HTTPException(status_code=400, detail={
            "error": f"这个目录不像 govfin 项目：{candidate}",
            "expected": list(paths.PROJECT_MARKERS),
        })
    paths.remember_project(candidate)
    return {"ok": True, "path": str(candidate.resolve())}


def _require_project() -> pathlib.Path:
    project = paths.resolve_project()
    if project is None:
        raise HTTPException(status_code=409, detail={
            "error": "还没找到 govfin 项目目录",
            "hint": "在「部署与配置」页选择一个包含 pyproject.toml 的目录",
        })
    return project


# ---------------------------------------------------------------------------
# 部署
# ---------------------------------------------------------------------------


@app.get("/api/deploy/status")
def api_deploy_status() -> dict:
    return deploy_mod.status_snapshot(paths.resolve_project())


@app.post("/api/deploy/launch")
def api_deploy_launch() -> dict:
    ok, message = deploy_mod.launch_docker_desktop()
    return {"ok": ok, "message": message}


class DeployRequest(BaseModel):
    rebuild: bool = False
    skip_nexent: bool = False


@app.post("/api/deploy/start")
def api_deploy_start(req: DeployRequest) -> dict:
    project = _require_project()
    job = deploy_mod.start_deploy(project, rebuild=req.rebuild, skip_nexent=req.skip_nexent)
    return job.snapshot()


class StopRequest(BaseModel):
    stop_docker: bool = False


@app.post("/api/services/stop")
def api_services_stop(req: StopRequest) -> dict:
    """一键关闭。默认**只停 GovFin 的三个容器，不动 Docker 引擎。**

    停 Docker 会连带停掉它下面的所有容器——包括 Nexent 那一整套（12 个）。
    用户点"关闭 GovFin"时多半没打算把 Nexent 也关掉，而那个后果要等
    下次用 Nexent 时才发现。所以那一步得显式选。
    """
    project = _require_project()
    job = deploy_mod.stop_services(project, stop_docker=req.stop_docker)
    return job.snapshot()


@app.get("/api/services/state")
def api_services_state() -> dict:
    """当前这套服务的真实状态（不依赖有没有正在跑的任务）。

    和任务状态分开：任务状态说的是"这次操作进行到哪了"，
    这个是"现在到底什么情况"。界面初始化时要的是后者。
    """
    snapshot = deploy_mod.status_snapshot(paths.resolve_project())
    services = {
        c["name"]: ("running" if c["state"] == "running" else "stopped")
        for c in snapshot.get("containers", [])
    }
    return {
        "docker": "ready" if snapshot["docker"]["ok"] else "stopped",
        "docker_detail": snapshot["docker"]["detail"],
        "network": snapshot.get("network", False),
        "services": services,
    }


@app.post("/api/services/start")
def api_services_start() -> dict:
    """一键启动：把 Docker 和容器拉起来。**不构建镜像。**

    和 `/api/deploy/start` 是两件事：那个构建镜像（几分钟），这个只启动（几十秒）。
    日常开机用这个——镜像早就有了，每次还等构建是没必要的。

    两种情况明确分开，是为了让"要等多久"变得可预期。混成一个按钮的话，
    用户每次都得赌这次是几秒还是几分钟。
    """
    project = _require_project()
    job = deploy_mod.start_services(project)
    return job.snapshot()


@app.get("/api/deploy/log")
def api_deploy_log(job_id: str, offset: int = 0) -> dict:
    job = deploy_mod.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"error": f"找不到任务 {job_id}"})
    lines, total = job.since(offset)
    payload = job.snapshot()
    payload.update({"lines": lines, "offset": offset, "next_offset": total})
    return payload


# ---------------------------------------------------------------------------
# LLM 配置
# ---------------------------------------------------------------------------


class LlmConfigRequest(BaseModel):
    provider: str = "deepseek"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"


@app.get("/api/config/llm")
def api_config_get() -> dict:
    project = paths.resolve_project()
    if project is None:
        return {"ok": False, "error": "还没找到项目目录", "has_key": False}
    values = paths.read_env(project)
    key = values.get("GOVFIN_LLM_API_KEY", "")
    return {
        "ok": True,
        "project": str(project),
        "provider": values.get("GOVFIN_LLM_PROVIDER", "deepseek"),
        "base_url": values.get("GOVFIN_LLM_BASE_URL", "https://api.deepseek.com/v1"),
        "model": values.get("GOVFIN_LLM_MODEL", "deepseek-chat"),
        "has_key": bool(key),
        # 只回打码后的形式。前端要展示"有没有配"和"配的是哪一把"，
        # 但没有任何理由把完整密钥发到浏览器里。
        "key_masked": paths.mask_secret(key),
    }


@app.post("/api/config/llm")
def api_config_set(req: LlmConfigRequest) -> dict:
    project = _require_project()
    updates = {
        "GOVFIN_LLM_PROVIDER": req.provider.strip() or "deepseek",
        "GOVFIN_LLM_BASE_URL": req.base_url.strip(),
        "GOVFIN_LLM_MODEL": req.model.strip(),
    }
    # 空 key 表示"不改动"而不是"清空"——界面上的输入框本来就是空的
    # （我们不回填完整 key），把空当清空会让用户一保存就把 key 弄丢。
    if req.api_key.strip():
        updates["GOVFIN_LLM_API_KEY"] = req.api_key.strip()
    paths.write_env(project, updates)
    return {"ok": True, "project": str(project), "keys": sorted(updates)}


class LlmTestRequest(BaseModel):
    api_key: str = ""
    base_url: str = ""
    model: str = ""


@app.post("/api/config/test")
def api_config_test(req: LlmTestRequest) -> dict:
    """测连通性。留空的字段用 .env 里已有的值补上，这样"只改了 model"也能测。"""
    project = paths.resolve_project()
    saved = paths.read_env(project) if project else {}
    return deploy_mod.test_llm(
        base_url=(req.base_url or saved.get("GOVFIN_LLM_BASE_URL") or "https://api.deepseek.com/v1"),
        api_key=(req.api_key or saved.get("GOVFIN_LLM_API_KEY") or ""),
        model=(req.model or saved.get("GOVFIN_LLM_MODEL") or "deepseek-chat"),
    )


@app.post("/api/config/register")
def api_config_register() -> dict:
    """把模型注册进 Nexent，顺便探一下本地向量化服务。

    三件事一起做，是因为它们构成"能不能在 Nexent 里用"的完整前提：
    对话模型、向量模型、以及向量服务真的活着。只报前两项的话，
    用户会以为配好了，然后在建知识库那一步卡住。
    """
    project = paths.resolve_project()
    saved = paths.read_env(project) if project else {}
    api_key = saved.get("GOVFIN_LLM_API_KEY", "")
    results: dict = {"deepseek": None, "embedding": None, "vector_service": None}

    # 向量服务探活（不依赖 Nexent 是否可用）
    from console.app import EMBEDDING_URL

    status, raw = _http("GET", f"{EMBEDDING_URL}/health", timeout=8)
    results["vector_service"] = (
        {"ok": True, "detail": (_json_or(raw, {}) or {}).get("model", "已就绪")}
        if status == 200 else
        {"ok": False, "detail": f"连不上 {EMBEDDING_URL}（服务可能没启动）"}
    )

    token = _nexent_token()
    if not token:
        results["deepseek"] = {"ok": False, "detail": "登录 Nexent 失败，无法注册"}
        results["embedding"] = {"ok": False, "detail": "同上"}
        return {"ok": False, "results": results}

    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    status, raw = _http("GET", f"{NEXENT_API}/model/list", headers=auth)
    existing = (_json_or(raw, {}) or {}).get("data") or []
    known = {str(m.get("model_name")) for m in existing if isinstance(m, dict)}

    def _register(name: str, payload: dict, key: str) -> None:
        if name in known:
            results[key] = {"ok": True, "detail": f"{name} 已存在，跳过"}
            return
        st, body = _http("POST", f"{NEXENT_API}/model/create", headers=auth, payload=payload, timeout=60)
        results[key] = (
            {"ok": True, "detail": f"已注册 {name}"} if st in (200, 201)
            else {"ok": False, "detail": f"HTTP {st}: {body[:200]}"}
        )

    if api_key:
        _register("deepseek-chat", {
            "model_name": "deepseek-chat",
            "display_name": "DeepSeek Chat",
            "model_type": "llm",
            "model_factory": "DeepSeek",
            "base_url": saved.get("GOVFIN_LLM_BASE_URL", "https://api.deepseek.com/v1"),
            "api_key": api_key,
            "max_tokens": 8192,
            "context_window_tokens": 65536,
            "timeout_seconds": 120,
        }, "deepseek")
    else:
        results["deepseek"] = {"ok": False, "detail": "项目 .env 里还没有 API Key"}

    # 向量模型指向本机的向量化服务。地址用容器名还是 localhost 取决于谁去调它——
    # 这里是 Nexent 的容器去调，所以用容器名。
    _register("BAAI/bge-small-zh-v1.5", {
        "model_name": "BAAI/bge-small-zh-v1.5",
        "display_name": "本地中文向量模型",
        "model_type": "embedding",
        "model_factory": "custom",
        "base_url": "http://govfin-embedding:8000/v1",
        "api_key": "not-needed",
        "max_tokens": 8192,
        "context_window_tokens": 512,
    }, "embedding")

    ok = bool(results["vector_service"].get("ok")) and bool((results["deepseek"] or {}).get("ok"))
    return {"ok": ok, "results": results}


# ---------------------------------------------------------------------------
# 应用元信息 / 前端
# ---------------------------------------------------------------------------


@app.get("/api/app/meta")
def api_meta() -> dict:
    return {
        "name": "GovFin 决策工作台",
        "frozen": paths.is_frozen(),
        "project": str(paths.resolve_project() or ""),
        "python": sys.version.split()[0],
    }


# 首页要送回桌面端的界面，而不是控制台那份。
#
# **不能靠"再注册一条 `GET /`"来实现**——FastAPI（Starlette）取**第一个**匹配的
# 路由，后注册的同路径路由永远不会被调用。这么做的话服务一切正常、日志干净、
# 就是打开来是旧界面，排查起来会先怀疑缓存。
#
# 所以直接把已有的那条路由的端点换掉：找到控制台注册的 `GET /`，原地替换它的
# handler。这样路径的注册顺序不变，行为可预测。
def _override_root() -> None:
    from fastapi.routing import APIRoute

    def desktop_index() -> FileResponse:
        return FileResponse(paths.static_dir() / "index.html")

    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == "/" and "GET" in (route.methods or set()):
            route.endpoint = desktop_index
            route.dependant.call = desktop_index
            return


_override_root()


@app.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "surface": "desktop"})
