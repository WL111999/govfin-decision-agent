r"""一键部署：从零到能用的完整流程。

跑法：
    python -X utf8 scripts/one_click_deploy.py

它按顺序做完这些事，每一步都先检查当前状态，**已经做过的就跳过**——所以随时可以
再跑一次，不会重复建东西：

    1. 检查 Docker 守护进程
    2. 构建三个镜像（决策服务 / 向量化服务 / 控制台）
    3. 启动容器并接入 Nexent 网络
    4. 在 Nexent 里开租户、注册 MCP、导入 Skill、配模型
    5. 建知识库并导入领域文档
    6. 打印访问地址

**为什么写成 Python 而不是 bash。** 目标机器是 Windows，而 Windows 上跑 bash 脚本
要先有 Git Bash 或 WSL——那就等于要求用户再装一样东西才能用"一键部署"。Python
在需要跑这套系统的机器上必然存在（Nexent 的部署脚本也用它）。

**为什么每步都要检查状态。** 一键部署最常见的失败不是"跑不起来"，而是"跑了两次，
第二次建了一堆重复的东西"。幂等比快更重要。
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
NEXENT_SRC = pathlib.Path(r"nexent_src")

# Windows 上 Docker Desktop 的默认安装位置。PATH 里通常没有 docker，
# 因为安装程序只把它加进 Docker Desktop 自己的 shell，不加进系统 PATH。
DOCKER_CANDIDATES = (
    r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
    "/usr/bin/docker",
    "/usr/local/bin/docker",
)

CONTAINERS = {
    "govfin-agent": {
        "image": "govfin-decision-agent:1.0.0",
        "dockerfile": "deploy/Dockerfile",
        "context": ".",
        "ports": ["8930:8930", "8940:8940"],
        "alias": "govfin-agent",
    },
    "govfin-embedding": {
        "image": "govfin-embedding:1.0.0",
        "dockerfile": "deploy/embedding/Dockerfile",
        "context": "deploy/embedding",
        "ports": ["8070:8000"],
        "alias": "govfin-embedding",
    },
    "govfin-console": {
        "image": "govfin-console:1.0.0",
        "dockerfile": "console/Dockerfile",
        "context": "console",
        "ports": ["8090:8080"],
        "alias": "govfin-console",
    },
}

NEXENT_NETWORK = "nexent_network"


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def step(n: int, total: int, title: str) -> None:
    print(f"\n{'=' * 60}\n[{n}/{total}] {title}\n{'=' * 60}")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def skip(msg: str) -> None:
    print(f"  ↺ {msg}")


def warn(msg: str) -> None:
    print(f"  ! {msg}")


def fail(msg: str) -> None:
    print(f"  ✗ {msg}")


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------


def find_docker() -> str | None:
    for path in DOCKER_CANDIDATES:
        if pathlib.Path(path).exists():
            return path
    found = shutil.which("docker")
    if found:
        return found
    return None


def run(cmd: list[str], *, timeout: int = 900, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=check,
    )


def docker_ok(docker: str) -> bool:
    try:
        r = run([docker, "info", "--format", "{{.ServerVersion}}"], timeout=60)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def container_state(docker: str, name: str) -> str:
    r = run([docker, "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.State}}"], timeout=60)
    return (r.stdout or "").strip()


def network_exists(docker: str, name: str) -> bool:
    r = run([docker, "network", "ls", "--filter", f"name=^{name}$", "--format", "{{.Name}}"], timeout=60)
    return name in (r.stdout or "")


def image_exists(docker: str, image: str) -> bool:
    r = run([docker, "images", "-q", image], timeout=60)
    return bool((r.stdout or "").strip())


def container_has_network(docker: str, name: str, network: str) -> bool:
    r = run([docker, "inspect", name, "--format",
             "{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}"], timeout=60)
    return network in (r.stdout or "")


# ---------------------------------------------------------------------------
# 步骤
# ---------------------------------------------------------------------------


def do_docker(docker: str, total: int) -> bool:
    step(1, total, "检查 Docker")
    if docker_ok(docker):
        r = run([docker, "info", "--format", "{{.ServerVersion}}"], timeout=60)
        ok(f"守护进程就绪（引擎 {r.stdout.strip()}）")
        return True

    fail("Docker 守护进程没有运行")
    print()
    print("  请先启动 Docker Desktop：")
    print("    - 开始菜单搜索 Docker Desktop 并打开")
    print("    - 或运行： \"C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe\"")
    print("  等托盘图标变成绿色后，重新运行本脚本。")
    return False


def do_images(docker: str, total: int, rebuild: bool) -> bool:
    step(2, total, "构建镜像")
    for name, spec in CONTAINERS.items():
        image = spec["image"]
        if image_exists(docker, image) and not rebuild:
            skip(f"{image} 已存在（要重建加 --rebuild）")
            continue
        print(f"  + 构建 {image} …")
        r = run([docker, "build", "-f", spec["dockerfile"], "-t", image, spec["context"]], timeout=1800)
        if r.returncode != 0:
            fail(f"构建 {image} 失败")
            print((r.stdout or "")[-1500:])
            print((r.stderr or "")[-800:])
            return False
        ok(f"{image} 构建完成")
    return True


def do_containers(docker: str, total: int, argv: list[str]) -> bool:
    step(3, total, "启动容器")

    env_args = {
        "govfin-console": _console_env_args(),
    }

    for name, spec in CONTAINERS.items():
        state = container_state(docker, name)
        if state == "running":
            skip(f"{name} 已在运行")
            # 容器可能是在网络接入之前启动的，所以每次都要确认网络
            if network_exists(docker, NEXENT_NETWORK) and not container_has_network(docker, name, NEXENT_NETWORK):
                r = run([docker, "network", "connect", "--alias", spec["alias"], NEXENT_NETWORK, name], timeout=60)
                if r.returncode == 0:
                    ok(f"{name} 已接入 {NEXENT_NETWORK}")
        else:
            if state:
                print(f"  + {name} 存在但未运行，重新启动")
                run([docker, "rm", "-f", name], timeout=120)
            port_args: list[str] = []
            for mapping in spec["ports"]:
                port_args += ["-p", mapping]
            cmd = [docker, "run", "-d", "--name", name, "--restart", "unless-stopped",
                   *port_args, *env_args.get(name, []), spec["image"]]
            r = run(cmd, timeout=300)
            if r.returncode != 0:
                fail(f"启动 {name} 失败：{(r.stderr or r.stdout or '')[:400]}")
                return False
            ok(f"{name} 已启动")

            if network_exists(docker, NEXENT_NETWORK):
                r = run([docker, "network", "connect", "--alias", spec["alias"], NEXENT_NETWORK, name], timeout=60)
                if r.returncode == 0:
                    ok(f"{name} 已接入 {NEXENT_NETWORK}")

    print("\n  等待服务就绪 …")
    for attempt in range(30):
        time.sleep(4)
        r = run([docker, "ps", "--filter", "name=^govfin-agent$", "--format", "{{.Status}}"], timeout=60)
        status = (r.stdout or "").strip()
        if "healthy" in status:
            ok(f"决策服务 {status}")
            break
        if attempt == 29:
            warn(f"决策服务状态：{status or '未知'}（继续，但可能还没就绪）")
    return True


def _console_env_args() -> list[str]:
    """控制台要读 Nexent 的凭据才能显示状态。从 Nexent 的 .env 里取，不写死。"""
    env_file = NEXENT_SRC / "deploy" / "env" / ".env"
    anon = ""
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("SUPABASE_KEY="):
                anon = line.partition("=")[2].strip().strip('"').strip("'")
                break
    args: list[str] = []
    if anon:
        args += ["-e", f"NEXENT_ANON_KEY={anon}"]
    args += [
        "-e", "NEXENT_EMAIL=govfin@nexent-demo.com",
        "-e", "NEXENT_PASSWORD=***REDACTED***",
    ]
    return args


def do_nexent(total: int, skip_tenant: bool) -> bool:
    step(4, total, "在 Nexent 里注册")
    if skip_tenant:
        skip("按 --skip-nexent 要求跳过")
        return True

    env_file = NEXENT_SRC / "deploy" / "env" / ".env"
    if not env_file.exists():
        warn(f"找不到 {env_file}")
        print("     Nexent 似乎还没部署。govfin 本身已经可用（见第 6 步），")
        print("     但要在 Nexent 界面里用，需要先按 deploy/README.md 部署 Nexent。")
        return True

    for script, label in (
        ("provision_tenant.py", "开租户 / 注册 MCP / 导入 Skill / 配模型"),
        ("sync_knowledge_base.py", "建知识库 / 导入领域文档"),
    ):
        path = ROOT / "scripts" / script
        if not path.exists():
            warn(f"找不到 {path}，跳过")
            continue
        print(f"  + {label} …")
        r = run([sys.executable, "-X", "utf8", str(path)], timeout=900)
        out = (r.stdout or "").strip()
        for line in out.splitlines():
            if line.strip() and not line.startswith("["):
                print(f"    {line}")
        if r.returncode != 0:
            warn(f"{script} 返回 {r.returncode}；上面的输出说明了卡在哪")
    return True


def do_done() -> None:
    print(f"\n{'=' * 60}")
    print("  部署完成")
    print(f"{'=' * 60}\n")
    print("  控制台    http://localhost:8090      ← 从这里开始")
    print("  Nexent    http://localhost:3000")
    print("  决策服务   http://localhost:8940/health")
    print("  向量化    http://localhost:8070/health")
    print()
    print("  控制台按「检查服务 → 授信决策 → 知识库 → Nexent 集成」四步排好，")
    print("  每一步都显示当前状态和该做什么。")
    print()
    print("  登录 Nexent 用租户账号（不是 suadmin）：")
    print("    govfin@nexent-demo.com / ***REDACTED***")
    print()
    print("  导入智能体：桌面「Nexent导入件\\跨域授信决策智能体.zip」")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="GovFin 一键部署")
    parser.add_argument("--rebuild", action="store_true", help="强制重建镜像")
    parser.add_argument("--skip-nexent", action="store_true", help="跳过 Nexent 注册")
    args = parser.parse_args()

    total = 4
    print("\nGovFin 一键部署")
    print(f"项目目录：{ROOT}")

    docker = find_docker()
    if not docker:
        print("\n✗ 找不到 docker 命令")
        print("  请先安装 Docker Desktop：https://www.docker.com/products/docker-desktop/")
        return 1

    if not do_docker(docker, total):
        return 1
    if not do_images(docker, total, args.rebuild):
        return 1
    if not do_containers(docker, total, sys.argv):
        return 1
    do_nexent(total, args.skip_nexent)
    do_done()
    return 0


if __name__ == "__main__":
    sys.exit(main())
