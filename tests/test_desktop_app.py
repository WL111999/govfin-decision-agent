"""桌面应用层：步骤映射、配置读写、路径解析。

这一层最容易出的错不是崩溃，而是**悄悄对不上**：

- 步骤表里写着 `gov_judicial_scan`，而服务端注册的工具改名叫别的了 ——
  调用返回空，界面上显示"没有行政处罚"，而真实的含义是"这个工具根本不存在"。
- 写 `.env` 时把文件整体覆盖 —— 用户手写的注释和别的键没了，下次想不起来自己写过什么。
- 项目目录解析把任意一个含 `pyproject.toml` 的目录当成项目 —— 指错目录之后
  Docker 会拿错误的上下文去构建，失败信息离原因很远。

这些都不会让测试变红，除非专门为它们写断言。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    """按路径加载模块。

    `app/` 不在包路径里（它是应用外壳，不是库），所以没法直接 import。
    """
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def app_paths():
    return _load("_app_paths", "app/paths.py")


@pytest.fixture(scope="module")
def app_server():
    """加载 server 模块——顺带验证它确实能装配起来。

    导入本身就有意义：`server.py` 要在 import 之前设好环境变量、
    再覆盖掉控制台的首页路由，任何一步写错都会在这里炸出来。
    """
    sys.path.insert(0, str(_ROOT / "app"))
    sys.path.insert(0, str(_ROOT))
    return _load("_app_server", "app/server.py")


# ---------------------------------------------------------------------------
# 步骤映射
# ---------------------------------------------------------------------------


def test_every_step_tool_is_actually_registered(app_server):
    """七个步骤引用的工具，必须真的在 MCP 服务端注册着。

    这条是这组测试里最要紧的一条。工具名写错的后果**不是报错**——
    MCP 调用一个不存在的工具会返回错误，而前端把它渲染成"这一步没有数据"，
    和"这家企业确实没有记录"在界面上长得一模一样。
    """
    import inspect
    import re

    source = (_ROOT / "src" / "govfin" / "mcp" / "server.py").read_text(encoding="utf-8")
    registered = set(re.findall(r'@server\.tool\(\s*\n\s*name="([^"]+)"', source))
    assert registered, "没能从 server.py 里解析出工具名，正则需要更新"

    used = {spec["tool"] for spec in app_server.STEPS.values()}
    missing = used - registered
    assert not missing, f"桌面端引用了未注册的工具：{sorted(missing)}（已注册：{sorted(registered)}）"


def _tool_signatures() -> dict[str, set[str]]:
    """从 server.py 的 AST 里取出每个工具的形参名。

    用 AST 而不是正则：`kg_path_query` 的签名跨了好几行，正则要么匹配不上
    （于是测试被 skip 掉、等于没测），要么写得很脆——改一次格式就失效。
    AST 不受换行与缩进影响。
    """
    import ast

    tree = ast.parse((_ROOT / "src" / "govfin" / "mcp" / "server.py").read_text(encoding="utf-8"))
    signatures: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # 找它上面的 @server.tool(name="...") 装饰器
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            for kw in dec.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    args = [a.arg for a in node.args.args]
                    signatures[kw.value.value] = set(args)
    return signatures


def test_step_argument_names_match_the_tool_signatures(app_server):
    """步骤表给出的参数名，必须是工具真正接受的参数名。

    多传一个参数，MCP 侧会因校验失败而拒绝；少传一个必填参数同理。
    两种都会表现为"这一步跑不出结果"——而"参数写错了"和"这家企业没有数据"
    在界面上长得一模一样。
    """
    signatures = _tool_signatures()
    assert signatures, "没能从 server.py 解析出任何工具签名，解析逻辑需要更新"

    for step_id, spec in app_server.STEPS.items():
        tool = spec["tool"]
        assert tool in signatures, f"步骤 {step_id} 引用的 {tool} 在 server.py 里没有定义"
        provided = set(spec["args"]("甲科技有限公司", "DEC-X"))
        unknown = provided - signatures[tool]
        assert not unknown, (
            f"步骤 {step_id} 给 {tool} 传了它不接受的参数 {sorted(unknown)}；"
            f"它接受的是 {sorted(signatures[tool])}"
        )


def test_step_needs_declare_what_they_require(app_server):
    """`evidence` 需要决策编号，其余需要主体。声明错了按钮的禁用逻辑就错了。"""
    for step_id, spec in app_server.STEPS.items():
        needs = set(spec["needs"])
        assert needs, f"{step_id} 没声明需要什么"
        assert needs <= {"subject", "decision_id"}, f"{step_id} 声明了未知的依赖 {needs}"
    assert set(app_server.STEPS["evidence"]["needs"]) == {"decision_id"}


def test_step_order_covers_every_step(app_server):
    """顺序表不能漏掉任何一个步骤，否则界面上会少一个按钮。"""
    assert set(app_server.STEP_ORDER) == set(app_server.STEPS)
    assert len(app_server.STEP_ORDER) == len(set(app_server.STEP_ORDER)), "顺序表有重复"


def test_unknown_step_returns_structured_error(app_server):
    """未知步骤要给出可读错误并列出可选项，而不是 500。

    前端拿到 `{error, available}` 能直接告诉用户"哪一步不认识"；
    拿到一个栈回溯只能显示"服务器错误"。
    """
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        app_server.api_step(app_server.StepRequest(step="bogus", subject="甲科技有限公司"))
    assert exc.value.status_code == 400
    assert "available" in exc.value.detail
    assert set(exc.value.detail["available"]) == set(app_server.STEP_ORDER)


def test_missing_decision_id_is_explained_not_just_rejected(app_server):
    """缺决策编号时要说清"先跑决策合成"，而不是只说缺参数。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        app_server.api_step(app_server.StepRequest(step="evidence", subject="甲科技有限公司"))
    assert exc.value.status_code == 400
    message = exc.value.detail["error"]
    assert "决策编号" in message
    assert "决策合成" in message, "只说缺参数，没说怎么才能不缺"


def test_root_route_serves_the_desktop_page(app_server):
    """首页必须指向桌面端的界面，而不是控制台那份。

    这条盯的是一个隐蔽的坑：FastAPI 取**第一个**匹配的路由，而控制台先注册了
    `GET /`。所以"再注册一条"是没用的——必须原地替换掉已有那条的 handler。
    不做这条断言的话，症状是"打开来是旧界面"，很容易被当成浏览器缓存。
    """
    from fastapi.routing import APIRoute

    roots = [
        r for r in app_server.app.routes
        if isinstance(r, APIRoute) and r.path == "/" and "GET" in (r.methods or set())
    ]
    assert roots, "首页路由不见了"
    assert roots[0].endpoint.__name__ == "desktop_index", (
        f"首页由 {roots[0].endpoint.__module__}.{roots[0].endpoint.__name__} 处理，"
        "不是桌面端界面"
    )


# ---------------------------------------------------------------------------
# .env 读写
# ---------------------------------------------------------------------------


SAMPLE_ENV = """\
# 本地开发配置 —— 已被 .gitignore 排除
GOVFIN_LLM_PROVIDER=deepseek
GOVFIN_LLM_API_KEY=sk-old-key-1234
GOVFIN_LLM_MODEL=deepseek-chat

# 可选：多模态 OCR
# GOVFIN_VLM_API_KEY=
GOVFIN_GRAPH_DB=data/govfin_graph.db
"""


def test_env_write_preserves_untouched_lines(app_paths, tmp_path):
    """只改一个键，其余行（含注释与空行）必须逐字保留。

    整体覆盖是最省事的写法，也是最伤人的：用户手写的注释、调过的参数、
    我们不知道的键，全都没了——而且没有任何提示。
    """
    project = tmp_path / "proj"
    project.mkdir()
    env = project / ".env"
    env.write_text(SAMPLE_ENV, encoding="utf-8")

    app_paths.write_env(project, {"GOVFIN_LLM_MODEL": "deepseek-reasoner"})
    after = env.read_text(encoding="utf-8")

    assert "GOVFIN_LLM_MODEL=deepseek-reasoner" in after
    for line in ("# 本地开发配置 —— 已被 .gitignore 排除",
                 "GOVFIN_LLM_PROVIDER=deepseek",
                 "GOVFIN_LLM_API_KEY=sk-old-key-1234",
                 "# 可选：多模态 OCR",
                 "# GOVFIN_VLM_API_KEY=",
                 "GOVFIN_GRAPH_DB=data/govfin_graph.db"):
        assert line in after, f"写 .env 时丢了这一行：{line!r}"


def test_env_write_preserves_line_endings(app_paths, tmp_path):
    """不改变换行符。

    Windows 上 `write_text` 默认把 `\\n` 翻成 `\\r\\n`。一次"什么都没改"的保存
    会让整个文件在 git diff 里整篇变红，而内容一个字没动——那种 diff 会把
    真正的改动淹掉。
    """
    project = tmp_path / "proj"
    project.mkdir()
    env = project / ".env"
    env.write_text(SAMPLE_ENV, encoding="utf-8", newline="\n")

    app_paths.write_env(project, {"GOVFIN_LLM_MODEL": "deepseek-chat"})
    raw = env.read_bytes()
    assert b"\r\n" not in raw, "写回后混进了 CRLF"


def test_env_write_appends_new_keys_with_a_comment(app_paths, tmp_path):
    """新键追加到末尾，并加一行来源注释——读者要知道这行是谁写的。"""
    project = tmp_path / "proj"
    project.mkdir()
    env = project / ".env"
    env.write_text(SAMPLE_ENV, encoding="utf-8")

    app_paths.write_env(project, {"GOVFIN_LLM_TEMPERATURE": "0.0"})
    after = env.read_text(encoding="utf-8")
    assert "GOVFIN_LLM_TEMPERATURE=0.0" in after
    assert "由 GovFin 桌面应用写入" in after
    # 原有的键一个不少
    assert "GOVFIN_LLM_API_KEY=sk-old-key-1234" in after


def test_env_read_ignores_comments_and_blanks(app_paths, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".env").write_text(SAMPLE_ENV, encoding="utf-8")
    values = app_paths.read_env(project)
    assert values["GOVFIN_LLM_PROVIDER"] == "deepseek"
    assert values["GOVFIN_LLM_API_KEY"] == "sk-old-key-1234"
    assert "# GOVFIN_VLM_API_KEY" not in values
    assert len(values) == 4


def test_mask_secret_keeps_only_the_ends(app_paths):
    """打码保留前缀和末四位——够对上号，但不泄露中间。

    前端要展示"配的是哪一把"，但不该拿到完整密钥。
    """
    masked = app_paths.mask_secret("***REDACTED***")
    assert masked.startswith("sk-")
    assert masked.endswith("f085")
    assert "4fdd712a" not in masked, "打码后仍能看到密钥中段"
    assert "*" * 8 in masked


def test_mask_secret_handles_short_and_empty(app_paths):
    assert app_paths.mask_secret("") == ""
    assert app_paths.mask_secret("abc") == "***"
    assert app_paths.mask_secret("12345678") == "********"


# ---------------------------------------------------------------------------
# 项目目录解析
# ---------------------------------------------------------------------------


def test_looks_like_project_requires_both_markers(app_paths, tmp_path):
    """只有 `pyproject.toml` 不算项目。

    随便一个 Python 包目录都有 `pyproject.toml`。要两个标记才能确认是**这个**项目，
    否则用户可能指到别的目录上，然后 Docker 拿错误的上下文去构建。
    """
    only_pyproject = tmp_path / "a"
    only_pyproject.mkdir()
    (only_pyproject / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert not app_paths.looks_like_project(only_pyproject), "只凭 pyproject.toml 就认了"

    real = tmp_path / "b"
    (real / "deploy").mkdir(parents=True)
    (real / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (real / "deploy" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    assert app_paths.looks_like_project(real)


def test_resolve_project_finds_the_repo(app_paths):
    """开发态下应当能自动找到本仓库——app/ 的上一级就是项目根。"""
    found = app_paths.resolve_project()
    assert found is not None, "开发态都没能找到项目根"
    assert app_paths.looks_like_project(found)


def test_config_roundtrip(app_paths, tmp_path, monkeypatch):
    """项目路径要能存能读——这是"下次打开还记得"的全部实现。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    app_paths.save_config(project_dir=r"D:\some\path")
    assert app_paths.load_config().get("project_dir") == r"D:\some\path"


def test_broken_config_does_not_crash(app_paths, tmp_path, monkeypatch):
    """配置文件坏了就当没有。

    它只是个"记住上次选的目录"的便利，不是运行必需的数据。为了它让整个应用
    起不来，是把小便利放在了可用性前面。
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path = app_paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    assert app_paths.load_config() == {}
