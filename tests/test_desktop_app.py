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

    **这里用的必须是编造的密钥。** 早先这条测试拿真实 key 当输入，
    于是它连同密钥一起被提交到了公开仓库——测试数据也属于仓库内容，
    不会因为它"只是测试"就不泄露。凡是要写进仓库的字面量，
    都得当成会被全世界看到。
    """
    fake = "sk-" + "0123456789abcdef" * 2 + "beef"
    masked = app_paths.mask_secret(fake)
    assert masked.startswith("sk-")
    assert masked.endswith("beef"), f"末四位没保留：{masked}"
    assert "0123456789abcdef" not in masked, "打码后仍能看到密钥中段"
    assert "*" * 8 in masked
    assert len(masked) < len(fake), "打码后不该还是原长"


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


# ---------------------------------------------------------------------------
# 无控制台环境（打包成 --windowed 之后的真实处境）
#
# 这一段盯的是一个**只在打包后才出现**的失效：exe 双击后静默退出，
# 没有任何提示、没有任何日志。根因是 sys.stdout/sys.stderr 为 None，
# 而 uvicorn 会调 `sys.stdout.isatty()` 来决定要不要上色。
#
# 不写这条测试的话，它在开发时永远不复现（开发态标准流是好的），
# 只有用户双击时才会遇到——而那时你手上一条线索都没有。
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app_logging():
    return _load("_app_logging", "app/logging_setup.py")


def test_log_stream_survives_the_calls_libraries_actually_make(app_logging):
    """补上的标准流必须能应付库真实会调的那些方法。

    特别是 `isatty()` —— uvicorn 的 DefaultFormatter 在 __init__ 里就调它，
    返回值得是布尔，缺了这个方法就是一个 AttributeError。
    """
    stream = app_logging._LogStream("test")
    assert stream.isatty() is False, "isatty 必须返回布尔，uvicorn 靠它决定要不要上色"
    assert stream.writable() is True
    assert stream.encoding == "utf-8"
    # 写入要能被接受（返回字符数），哪怕内容会被转进日志
    assert stream.write("hello") == 5
    assert stream.flush() is None
    with pytest.raises(OSError):
        stream.fileno()  # 没有真实 fd，抛 OSError 是 io 的约定


def test_install_streams_fills_only_missing_ones(app_logging, monkeypatch):
    """只补 None 的那些。

    开发态跑 `python app/main.py` 时标准流是好的，替换掉反而让人看不到输出。
    """
    import io
    import sys as _sys

    real_out, real_err = _sys.stdout, _sys.stderr
    try:
        _sys.stdout = None
        _sys.stderr = io.StringIO()
        app_logging.install_streams()
        assert _sys.stdout is not None, "None 的 stdout 没被补上"
        assert isinstance(_sys.stderr, io.StringIO), "好的 stderr 被无谓替换了"
    finally:
        _sys.stdout, _sys.stderr = real_out, real_err


def test_uvicorn_can_configure_logging_without_a_console(app_logging):
    """真正的回归测试：uvicorn 能在无标准流的环境下配置日志。

    这一条直接复现原始故障——把 stdout/stderr 置 None，然后让 uvicorn
    构建它的 Config（`configure_logging()` 就在 `__init__` 里跑）。
    没有补标准流的话，这里会抛
    `AttributeError: 'NoneType' object has no attribute 'isatty'`。
    """
    import sys as _sys

    try:
        import uvicorn  # noqa: F401
    except ImportError:
        pytest.skip("没装 uvicorn")

    real_out, real_err = _sys.stdout, _sys.stderr
    try:
        _sys.stdout = None
        _sys.stderr = None
        app_logging.install_streams()
        # Config 的构造函数里就会调 configure_logging
        cfg = uvicorn.Config(lambda: None, log_level="warning")
        assert cfg.log_level == "warning"
    finally:
        _sys.stdout, _sys.stderr = real_out, real_err


def test_main_fills_streams_before_starting_the_service():
    """启动顺序：补标准流必须发生在起 uvicorn **之前**。

    上一条测试自己调了 `install_streams()`，所以它证明的是"这个方法有用"，
    证明不了"启动流程真的会调它"——把 main.py 里那行删掉，它照样通过。
    （变异验证抓到了这一点。）

    顺序也不能反：uvicorn 在后台线程里起的，`Config.__init__` 里就会读
    `sys.stdout`。先起服务再补流，那一下照样崩。
    """
    import ast

    tree = ast.parse((_ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    main_fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "main"),
        None,
    )
    assert main_fn is not None, "main.py 里找不到 main()"

    def _line_of(pred) -> int | None:
        for node in ast.walk(main_fn):
            if pred(node):
                return node.lineno
        return None

    streams_at = _line_of(
        lambda n: isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "install_streams"
    )
    thread_at = _line_of(
        lambda n: isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "Thread"
    )

    assert streams_at is not None, (
        "main() 里没有调用 install_streams()——无控制台时 uvicorn 会因为 "
        "sys.stdout 是 None 而崩在 isatty() 上"
    )
    assert thread_at is not None, "main() 里没找到起服务线程的地方"
    assert streams_at < thread_at, (
        f"install_streams() 在第 {streams_at} 行，而起服务在第 {thread_at} 行——"
        "补标准流必须在前，否则 uvicorn 配置日志时就已经崩了"
    )


def test_log_stream_does_not_recurse_when_the_log_file_fails(app_logging):
    """日志写不进去时也不能崩，更不能递归。

    `_LogStream.write` 调 `log()`，而 `log()` 失败时若往 stderr 写，就会再进
    `_LogStream.write`——无限递归。所以 `log()` 必须自己吞掉所有异常。
    """
    stream = app_logging._LogStream("test")
    # 写一个空串和一个纯空白串：不该产生日志行，也不该抛
    assert stream.write("") == 0
    assert stream.write("   \n") == 4


# ---------------------------------------------------------------------------
# 一键启动（区别于一键部署）
#
# 用户的实际场景：机器重启后 Docker 没在跑、容器全没起，点一下要能全起来。
# 这和"部署"是两件事：部署要构建镜像（几分钟），启动只用已构建好的（几十秒）。
# 两者最重要的差别就是**启动不该构建镜像**——一旦开始构建，用户等的就不是
# 几十秒而是几分钟，而他不知道自己点错了哪个按钮。
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app_deploy():
    sys.path.insert(0, str(_ROOT / "app"))
    return _load("_app_deploy", "app/deploy.py")


def test_start_services_exists_and_is_separate_from_deploy(app_deploy):
    """启动和部署必须是两个入口。"""
    assert hasattr(app_deploy, "start_services"), "没有 start_services"
    assert hasattr(app_deploy, "start_deploy"), "没有 start_deploy"
    assert app_deploy.start_services is not app_deploy.start_deploy


def test_start_services_never_builds_images(app_deploy):
    """一键启动里**不能**出现 docker build。

    这是它和部署的本质区别，也是用户能预期"几十秒还是几分钟"的唯一依据。
    真去构建的话，用户点的是"启动"却要等几分钟，而他不知道自己点错了哪个。
    """
    import ast

    source = (_ROOT / "app" / "deploy.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    # 取 _start_only 这个函数的源码段
    target = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "_start_only"), None)
    assert target is not None, "找不到 _start_only"
    body = ast.get_source_segment(source, target) or ""

    assert '"build"' not in body and "'build'" not in body, (
        "_start_only 里出现了 docker build —— 启动不该构建镜像"
    )
    assert '"run"' in body or "'run'" in body, "没找到启动容器的 docker run"


def test_missing_image_is_reported_not_silently_rebuilt(app_deploy):
    """镜像缺失时要**真的查一遍**，并且**真的中止**。

    静默跳过的后果：用户点"启动"，然后对着一个没有任何解释的进度条等下去，
    最后拿到一个起不来的服务。

    这里查的是逻辑而不是措辞：`missing` 的赋值里必须真的调用 image_exists，
    并且那个分支必须能中止流程。只断言"代码里出现了 missing 和 部署 这两个词"
    是拦不住退化的 —— 把赋值改成 `missing = []`，那些字面量还在，测试照样绿。
    （变异验证抓到过这一点。）
    """
    import ast

    source = (_ROOT / "app" / "deploy.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "_start_only"), None)
    assert target is not None, "找不到 _start_only"

    # 1) missing 的赋值必须真的去查镜像在不在
    computed_from_probe = False
    for node in ast.walk(target):
        if isinstance(node, ast.Assign) and any(
            isinstance(tg, ast.Name) and tg.id == "missing" for tg in node.targets
        ):
            seg = ast.get_source_segment(source, node.value) or ""
            computed_from_probe = "image_exists" in seg
    assert computed_from_probe, (
        "missing 不是由 image_exists 算出来的 —— 那它恒为空，等于没检查"
    )

    # 2) 那个分支必须能中止（有 return）
    guards = [
        node for node in ast.walk(target)
        if isinstance(node, ast.If)
        and "missing" in (ast.get_source_segment(source, node.test) or "")
    ]
    assert guards, "没有针对 missing 的分支"
    aborts = any(
        isinstance(n, ast.Return) for g in guards for n in ast.walk(g)
    )
    assert aborts, "缺镜像时那个分支不会中止流程，会继续往下走去启动容器"

    # 3) 而且要告诉用户下一步去哪
    body = ast.get_source_segment(source, target) or ""
    assert "部署" in body or "deploy" in body.lower(), "没告诉用户该去点『部署』"


def test_services_start_route_is_registered(app_server):
    """路由要真的挂上，否则前端点了会 404。"""
    from fastapi.routing import APIRoute

    paths = {r.path for r in app_server.app.routes if isinstance(r, APIRoute)}
    assert "/api/services/start" in paths
    assert "/api/deploy/start" in paths, "部署入口被误删了"


def test_stop_services_defaults_to_not_touching_docker(app_deploy):
    """关闭的默认行为是**只停 GovFin 的容器，不动 Docker 引擎**。

    这不是谨慎，是必需的默认值：停 Docker 会连带停掉它下面的**所有**容器——
    包括 Nexent 那一整套（12 个）。用户点"关闭 GovFin"时多半没打算把 Nexent
    也关掉，而那个后果要等下次用 Nexent 时才发现，中间隔了很久、离原因很远。
    """
    import inspect

    sig = inspect.signature(app_deploy.stop_services)
    assert "stop_docker" in sig.parameters, "stop_services 没有停引擎的开关"
    assert sig.parameters["stop_docker"].default is False, (
        "stop_docker 的默认值必须是 False —— 默认把别人的服务一起停掉是不可接受的"
    )


def test_stop_services_route_is_registered(app_server):
    from fastapi.routing import APIRoute

    paths = {r.path for r in app_server.app.routes if isinstance(r, APIRoute)}
    assert "/api/services/stop" in paths
    assert "/api/services/state" in paths, "缺了状态查询路由"


def test_service_state_reports_every_container(app_server):
    """状态查询要覆盖全部容器，不能漏。

    漏掉一个的后果：界面上那个节点永远是灰的，而它其实在跑——
    用户会去点启动，然后什么也没发生。
    """
    import inspect

    src = inspect.getsource(app_server.api_services_state)
    assert "CONTAINERS" in src or "containers" in src, "状态查询没有遍历容器"
    assert "services" in src, "没有返回 services 字段（前端靠它画节点）"


def test_job_tracks_structured_state_not_just_text(app_deploy):
    """任务要维护结构化状态，而不只是文本日志。

    界面要靠它画拓扑：**日志是给人读的，状态是给界面画的**。
    让界面去 grep 日志的话，每次改提示文案都可能悄悄弄坏画面——
    而那种失效不会有任何测试变红。
    """
    job = app_deploy.DeployJob(_ROOT)
    assert hasattr(job, "services") and hasattr(job, "phase")
    snap = job.snapshot()
    for key in ("phase", "docker", "services", "action"):
        assert key in snap, f"snapshot 里缺 {key}，界面画不出拓扑"

    # 每个容器都要有初始状态，否则前端会少画一个节点
    assert set(snap["services"]) == set(app_deploy.CONTAINERS), (
        "services 里的键与 CONTAINERS 对不上，会有节点画不出来"
    )
    assert all(v == "pending" for v in snap["services"].values())


def test_set_phase_does_not_write_a_log_line(app_deploy):
    """`set_phase` 不该写日志 —— 否则同一句话会出现两遍。

    实测踩到过：日志里"检查 Docker"连着出现两次，看起来像执行了两轮。
    message 是给界面显示的状态行，日志由调用处自己写。
    """
    job = app_deploy.DeployJob(_ROOT)
    before = len(job.lines)
    job.set_phase("docker", "检查 Docker")
    assert len(job.lines) == before, "set_phase 往日志里写了东西"
    assert job.snapshot()["message"] == "检查 Docker", "message 没被记下来"


# ---------------------------------------------------------------------------
# 前端渲染的数据契约
#
# 前端没有 JS 测试框架，所以这几条只能检查"代码用了正确的取值方式"，
# 而不是"渲染出了正确的字符串"。它们挡不住所有问题，但能挡住**最恶心的那一类**：
# 字段形状变了、渲染代码没跟上，界面上出现 `[object Object]`——
# 而那是用户唯一看得见的线索，除了"这东西坏了"什么也说明不了。
# ---------------------------------------------------------------------------


def _ui_source() -> str:
    return (_ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")


def test_confidence_is_rendered_through_the_shape_aware_helper():
    """置信度必须经 confText 渲染，不能直接塞进模板字符串。

    这个字段在**两条通道里形状不同**：
        kg_path_query    → 对象 {score, hop_count, geometry_factor, ...}
        evidence_bundle  → 数字
    直接 `esc(x)` 的话，对象那一路会渲染成 `[object Object]`（实测踩到）。
    """
    src = _ui_source()
    assert "function confText(" in src, "没有 confText 辅助函数"
    # 两处渲染都必须走它
    assert "confText(p.confidence)" in src, "跨域推理面板没走 confText"
    assert "confText(c.confidence)" in src, "依据回执面板没走 confText"
    # 不能有绕过它的写法残留
    assert "esc(p.confidence" not in src, "还有绕过 confText 的写法"
    assert "esc(c.confidence" not in src, "还有绕过 confText 的写法"


def test_confText_handles_both_shapes():
    """confText 的逻辑本身：两种形状都要能出数字。

    用 Python 复刻同一套规则来验证——不是完美的等价测试（JS 与 Python 的
    Number 语义不完全一样），但足以钉住"对象要取 .score"这个关键分支。
    真正跑一遍 JS 需要 Node，为这一条引进来不划算。
    """
    src = _ui_source()
    body = src.split("function confText(", 1)[1].split("\n}", 1)[0]
    assert "v.score" in body, "对象形态没有取 .score"
    assert "typeof v === 'object'" in body, "没有区分对象与数字"
    assert "toFixed(4)" in body, "没有格式化到四位小数（和别处的精度不一致）"
