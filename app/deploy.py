"""桌面应用里的 Docker 编排。

和 `scripts/one_click_deploy.py` 的关系：那个是**命令行**入口，边跑边往终端打印；
这个是**界面**入口，同样的动作，但输出进一个带偏移量的日志缓冲，好让前端轮询取增量。

为什么不干脆让界面调那个脚本：脚本是为终端写的，它的进度信息混在 stdout 里，
而界面需要的是"从第 N 行开始有哪些新行"。把两者塞进一个文件会让脚本里长满
`if 在界面里` 的分支——不如让它们共享同一套底层动作（下面这些函数），
各自管各自的输出。
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import threading
import time
import uuid

# Windows 上 Docker Desktop 的默认安装位置。PATH 里通常没有 docker——
# 安装程序只把它加进 Docker Desktop 自己的 shell，不加进系统 PATH。
DOCKER_CANDIDATES = (
    r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
    "/usr/bin/docker",
    "/usr/local/bin/docker",
)

DOCKER_DESKTOP_CANDIDATES = (
    r"C:\Program Files\Docker\Docker\Docker Desktop.exe",
    "/Applications/Docker.app",
)

NEXENT_NETWORK = "nexent_network"

# 与 one_click_deploy.py 保持一致。两处都要维护是有点别扭，但这里是界面入口、
# 那里是命令行入口，共享一份配置需要先抽出公共模块——而目前只有三个容器，
# 抽出来的收益还抵不上多一层间接。
CONTAINERS = {
    "govfin-agent": {
        "image": "govfin-decision-agent:1.0.0",
        "dockerfile": "deploy/Dockerfile",
        "context": ".",
        "ports": ["8930:8930", "8940:8940"],
        "alias": "govfin-agent",
        "volumes": ["govfin-graph-data:/app/data"],
        "label": "决策服务",
    },
    "govfin-embedding": {
        "image": "govfin-embedding:1.0.0",
        "dockerfile": "deploy/embedding/Dockerfile",
        "context": "deploy/embedding",
        "ports": ["8070:8000"],
        "alias": "govfin-embedding",
        "label": "向量化服务",
    },
    "govfin-console": {
        "image": "govfin-console:1.0.0",
        "dockerfile": "console/Dockerfile",
        "context": "console",
        "ports": ["8090:8080"],
        "alias": "govfin-console",
        "label": "网页控制台",
    },
}


def find_docker() -> str | None:
    for path in DOCKER_CANDIDATES:
        if pathlib.Path(path).exists():
            return path
    return shutil.which("docker")


def find_docker_desktop() -> str | None:
    for path in DOCKER_DESKTOP_CANDIDATES:
        if pathlib.Path(path).exists():
            return path
    return None


def run(cmd: list[str], *, timeout: int = 900) -> subprocess.CompletedProcess:
    """跑一条命令，编码固定 utf-8 容错。

    Windows 控制台默认 GBK，docker 的输出里带 UTF-8 字符时会直接抛
    UnicodeDecodeError——那会让"读日志"变成一件会崩的事。
    """
    kwargs: dict = {}
    if os.name == "nt":
        # 不弹黑窗
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, **kwargs,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"命令超时（{timeout}s）")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


# ---------------------------------------------------------------------------
# 状态探测
# ---------------------------------------------------------------------------


def docker_running() -> tuple[bool, str]:
    docker = find_docker()
    if not docker:
        return False, "找不到 docker 命令，请先安装 Docker Desktop"
    r = run([docker, "info", "--format", "{{.ServerVersion}}"], timeout=60)
    if r.returncode != 0:
        return False, "Docker 守护进程没有运行"
    return True, f"引擎 {r.stdout.strip()}"


def container_state(name: str) -> str:
    docker = find_docker()
    if not docker:
        return ""
    r = run([docker, "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.State}}"], timeout=60)
    return (r.stdout or "").strip()


def container_health(name: str) -> str:
    docker = find_docker()
    if not docker:
        return ""
    r = run([docker, "ps", "--filter", f"name=^{name}$", "--format", "{{.Status}}"], timeout=60)
    return (r.stdout or "").strip()


def image_exists(image: str) -> bool:
    docker = find_docker()
    if not docker:
        return False
    r = run([docker, "images", "-q", image], timeout=60)
    return bool((r.stdout or "").strip())


def network_exists(name: str = NEXENT_NETWORK) -> bool:
    docker = find_docker()
    if not docker:
        return False
    r = run([docker, "network", "ls", "--filter", f"name=^{name}$", "--format", "{{.Name}}"], timeout=60)
    return name in (r.stdout or "")


def status_snapshot(project: pathlib.Path | None) -> dict:
    """一眼看清部署到哪一步了。前端拿它渲染状态灯。"""
    ok, detail = docker_running()
    snapshot: dict = {
        "docker": {"ok": ok, "detail": detail, "path": find_docker() or "",
                   "desktop": find_docker_desktop() or ""},
        "project": str(project) if project else None,
        "containers": [],
        "images": [],
        "network": False,
    }
    if not ok:
        return snapshot

    snapshot["network"] = network_exists()
    for name, spec in CONTAINERS.items():
        state = container_state(name)
        snapshot["containers"].append({
            "name": name,
            "label": spec["label"],
            "state": state or "missing",
            "status": container_health(name) if state == "running" else "",
            "image": spec["image"],
            "image_ready": image_exists(spec["image"]),
        })
        snapshot["images"].append({"image": spec["image"], "ready": image_exists(spec["image"])})
    return snapshot


def launch_docker_desktop() -> tuple[bool, str]:
    """把 Docker Desktop 拉起来。**不等待**——它要几十秒，等待会让界面卡住。

    这里直接返回，由前端轮询 `docker_running()` 看什么时候好了。
    这比在后端阻塞住然后超时，体验和实现都简单。
    """
    exe = find_docker_desktop()
    if not exe:
        return False, "找不到 Docker Desktop，请手动启动或先安装"
    try:
        if os.name == "nt":
            # 用 start 而不是直接 Popen：Docker Desktop 是 GUI 程序，
            # 直接拉起会让它成为本进程的子进程，我们退出时可能把它一起带走。
            subprocess.Popen(f'start "" "{exe}"', shell=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            subprocess.Popen(["open", "-a", exe])
    except OSError as exc:
        return False, f"启动失败：{exc}"
    return True, "已发出启动指令，通常需要 30–60 秒"


# ---------------------------------------------------------------------------
# 部署任务
# ---------------------------------------------------------------------------


class DeployJob:
    """一次部署的执行记录。

    日志用 list + 只增不减的偏移量。前端带着 offset 来问"有没有新的"，
    这样重连、刷新页面、开两个窗口都不会丢行或者重复行。
    """

    def __init__(self, project: pathlib.Path, *, rebuild: bool = False, skip_nexent: bool = False) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.project = pathlib.Path(project)
        self.rebuild = rebuild
        self.skip_nexent = skip_nexent
        self.lines: list[str] = []
        self.done = False
        self.ok = False
        self.started_at = time.time()
        self.finished_at: float | None = None
        self._lock = threading.Lock()

    def log(self, message: str = "") -> None:
        with self._lock:
            self.lines.append(message)

    def log_block(self, text: str) -> None:
        """把一段多行输出逐行写进去。

        逐行而不是整段：前端是按行渲染的，整段塞进去会变成一行超长文本，
        横向滚动着看日志是最难读的形式。
        """
        for line in (text or "").splitlines():
            self.log(f"    {line}")

    def since(self, offset: int) -> tuple[list[str], int]:
        with self._lock:
            total = len(self.lines)
            if offset < 0 or offset > total:
                offset = 0
            return self.lines[offset:], total

    def snapshot(self) -> dict:
        elapsed = (self.finished_at or time.time()) - self.started_at
        return {
            "job_id": self.id,
            "running": not self.done,
            "done": self.done,
            "ok": self.ok,
            "elapsed_seconds": round(elapsed, 1),
            "line_count": len(self.lines),
        }


_JOBS: dict[str, DeployJob] = {}
_JOBS_LOCK = threading.Lock()


def get_job(job_id: str) -> DeployJob | None:
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def start_deploy(project: pathlib.Path, *, rebuild: bool = False, skip_nexent: bool = False) -> DeployJob:
    job = DeployJob(project, rebuild=rebuild, skip_nexent=skip_nexent)
    with _JOBS_LOCK:
        _JOBS[job.id] = job
        # 只留最近 5 次，避免长时间运行后内存里堆一堆没人看的日志
        for stale in list(_JOBS)[:-5]:
            _JOBS.pop(stale, None)
    threading.Thread(target=_run_deploy, args=(job,), daemon=True).start()
    return job


def _run_deploy(job: DeployJob) -> None:
    try:
        ok = _deploy_steps(job)
        job.ok = ok
    except Exception as exc:  # noqa: BLE001 - 任何异常都要变成日志，不能让线程静默死掉
        job.log(f"✗ 部署中断：{type(exc).__name__}: {exc}")
        job.ok = False
    finally:
        job.done = True
        job.finished_at = time.time()
        job.log("")
        job.log("完成。" if job.ok else "未完成，请看上面的报错。")


def _deploy_steps(job: DeployJob) -> bool:
    project = job.project
    docker = find_docker()
    if not docker:
        job.log("✗ 找不到 docker 命令")
        job.log("  请先安装 Docker Desktop：https://www.docker.com/products/docker-desktop/")
        return False

    running, detail = docker_running()
    if not running:
        job.log(f"✗ {detail}")
        job.log("  请先在「部署与配置」页点『启动 Docker Desktop』，等托盘图标变绿后重试。")
        return False
    job.log(f"✓ Docker 就绪（{detail}）")

    # 镜像
    job.log("")
    job.log("构建镜像")
    for name, spec in CONTAINERS.items():
        image = spec["image"]
        if image_exists(image) and not job.rebuild:
            job.log(f"  ↺ {image} 已存在")
            continue
        job.log(f"  + 构建 {image} …（第一次或重建时较慢，可能几分钟）")
        r = run([docker, "build", "-f", spec["dockerfile"], "-t", image, spec["context"]],
                timeout=2400)
        if r.returncode != 0:
            job.log(f"  ✗ 构建 {image} 失败")
            job.log_block((r.stdout or r.stderr or "")[-2000:])
            return False
        job.log(f"  ✓ {image} 完成")

    # 容器
    job.log("")
    job.log("启动容器")
    console_env = _console_env_args(project)
    for name, spec in CONTAINERS.items():
        state = container_state(name)
        if state == "running":
            job.log(f"  ↺ {name} 已在运行")
        else:
            if state:
                run([docker, "rm", "-f", name], timeout=120)
            cmd = [docker, "run", "-d", "--name", name, "--restart", "unless-stopped"]
            for mapping in spec["ports"]:
                cmd += ["-p", mapping]
            for mount in spec.get("volumes", []):
                cmd += ["-v", mount]
            if name == "govfin-console":
                cmd += console_env
            r = run(cmd + [spec["image"]], timeout=300)
            if r.returncode != 0:
                job.log(f"  ✗ 启动 {name} 失败：{(r.stderr or r.stdout or '')[:300]}")
                return False
            job.log(f"  + {name} 已启动")

        if network_exists() and not _has_network(docker, name):
            r = run([docker, "network", "connect", "--alias", spec["alias"], NEXENT_NETWORK, name], timeout=60)
            if r.returncode == 0:
                job.log(f"    已接入 {NEXENT_NETWORK}")

    # 等就绪
    job.log("")
    job.log("等待服务就绪 …")
    for _ in range(30):
        time.sleep(4)
        status = container_health("govfin-agent")
        if "healthy" in status:
            job.log(f"  ✓ 决策服务 {status}")
            break
    else:
        job.log("  ! 决策服务还没到 healthy，继续后续步骤（可能只是慢）")

    # Nexent 侧注册
    if job.skip_nexent:
        job.log("")
        job.log("↺ 按设置跳过 Nexent 注册")
    else:
        nexent_env = _nexent_src() / "deploy" / "env" / ".env"
        if not nexent_env.exists():
            job.log("")
            job.log(f"! 找不到 {nexent_env}")
            job.log("  Nexent 似乎还没部署。govfin 本身已经可用，但要在 Nexent 界面里用，")
            job.log("  需要先按 deploy/README.md 部署 Nexent。")
        else:
            job.log("")
            job.log("在 Nexent 里注册")
            for script, label in (
                ("scripts/provision_tenant.py", "开租户 / 注册 MCP / 导入 Skill / 配模型"),
                ("scripts/sync_knowledge_base.py", "建知识库 / 导入领域文档"),
            ):
                path = project / script
                if not path.exists():
                    job.log(f"  ! 找不到 {script}，跳过")
                    continue
                job.log(f"  + {label} …")
                r = run([_python(), "-X", "utf8", str(path)], timeout=1200)
                job.log_block((r.stdout or "").strip()[-3000:])
                if r.returncode != 0:
                    job.log(f"  ! {script} 返回 {r.returncode}（不阻断，见上面的输出）")

    job.log("")
    job.log("控制台    http://localhost:8090")
    job.log("Nexent    http://localhost:3000")
    return True


def _nexent_src() -> pathlib.Path:
    """本机 Nexent 源码目录。

    不写死路径：那样既是泄露开发机目录结构，也让别人 clone 下来跑不了。
    这里复用 scripts/ 下那套解析（与项目同级的 nexent_src 是默认位置）。
    """
    import sys as _sys

    scripts_dir = pathlib.Path(__file__).resolve().parent.parent / "scripts"
    if str(scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(scripts_dir))
    try:
        import nexent_local
        return nexent_local.require_nexent_src()
    except Exception:  # noqa: BLE001
        return pathlib.Path("nexent_src")


def _has_network(docker: str, name: str) -> bool:
    r = run([docker, "inspect", name, "--format",
             "{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}"], timeout=60)
    return NEXENT_NETWORK in (r.stdout or "")


def _python() -> str:
    """跑辅助脚本用的解释器。

    打包成 exe 后 `sys.executable` 是 GovFin.exe 本身，拿它去跑
    `provision_tenant.py` 会变成"用记事本打开 PDF"——所以冻结态要另找一个
    python。找不到就退回 `sys.executable`，让报错来得明确一点。
    """
    import sys

    if not getattr(sys, "frozen", False):
        return sys.executable
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            return found
    return sys.executable


def _console_env_args(project: pathlib.Path) -> list[str]:
    """控制台容器要的凭据。从 Nexent 的 .env 取，不写死。"""
    args: list[str] = []
    nexent_env = _nexent_src() / "deploy" / "env" / ".env"
    anon = ""
    if nexent_env.exists():
        for line in nexent_env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("SUPABASE_KEY="):
                anon = line.partition("=")[2].strip().strip('"').strip("'")
                break
    if anon:
        args += ["-e", f"NEXENT_ANON_KEY={anon}"]
    # 租户凭据从本地 .env 读。读不到就**不传密码**——控制台会显示"未配置"，
    # 而不是拿到一个写死在公开仓库里的默认密码。
    govfin_env = project / ".env"
    email = "govfin@nexent-demo.com"
    password = ""
    if govfin_env.exists():
        values = _read_env(govfin_env)
        email = values.get("NEXENT_TENANT_EMAIL", email) or email
        password = values.get("NEXENT_TENANT_PASSWORD", "")
    args += ["-e", f"NEXENT_EMAIL={email}"]
    if password:
        args += ["-e", f"NEXENT_PASSWORD={password}"]
    return args


def _read_env(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


# ---------------------------------------------------------------------------
# LLM 连通性
# ---------------------------------------------------------------------------


def test_llm(base_url: str, api_key: str, model: str, timeout: int = 25) -> dict:
    """真调一次模型，不是只看 key 格式对不对。

    只校验格式的话，一把过期的 key、一个写错的 base_url、一个没有余额的账号
    都会显示"配置正常"，然后在真正用的时候才炸——而那时报错的地方离原因很远。
    """
    import urllib.error
    import urllib.request

    if not api_key:
        return {"ok": False, "error": "没有填 API Key"}
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "回复一个字：好"}],
        "max_tokens": 8,
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        elapsed = int((time.time() - started) * 1000)
        reply = ""
        choices = body.get("choices") or []
        if choices:
            reply = ((choices[0].get("message") or {}).get("content") or "").strip()
        return {"ok": True, "latency_ms": elapsed, "reply": reply[:40], "model": body.get("model", model)}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        hint = {
            401: "API Key 无效或已过期",
            402: "账户余额不足",
            429: "被限流，稍后再试",
        }.get(exc.code, "")
        return {"ok": False, "error": f"HTTP {exc.code} {hint}".strip(), "detail": detail}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
