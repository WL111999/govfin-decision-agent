r"""本机 Nexent 部署的定位与凭据。

管两件事：**它在哪**（find_nexent_src）和**怎么进去**（凭据读写）。
放一起是因为调用方总是同时要这两个 —— "连上本机那一套 Nexent"。

**为什么要有这个模块。** 早先租户密码是**硬编码在脚本里**的，于是它跟着仓库
一起公开了——任何 clone 下来的人都知道演示环境用的是哪个密码。同时公开的还有
平台超管的默认密码。

问题不在于"那个密码有多重要"，而在于**公开仓库里不该出现任何能直接用的凭据**。
哪怕它只是本地演示环境的，把它写进源码就等于默认所有人都该知道它。

改成本地生成、存在项目 `.env` 里（`.env` 已被 gitignore）、脚本之间靠它传递：

  - 第一次跑 `provision_tenant.py` 时生成一个随机密码并写入
  - 其余脚本从 `.env` 读，读不到就明确报错（而不是回退到一个公开的默认值）

**邮箱不算秘密**，所以它仍然有一个默认值——只有密码需要随机化。
"""

from __future__ import annotations

import pathlib
import secrets
import string

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]

ENV_EMAIL_KEY = "NEXENT_TENANT_EMAIL"
ENV_PASSWORD_KEY = "NEXENT_TENANT_PASSWORD"

# 邮箱留在源码里没问题：它是个标识，不是凭据。
DEFAULT_EMAIL = "govfin@nexent-demo.com"


def _strong_password(length: int = 16) -> str:
    """生成一个能过常见强度校验的密码（大写+小写+数字）。"""
    alphabet = string.ascii_letters + string.digits
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.isupper() for c in pwd)
                and any(c.islower() for c in pwd)
                and any(c.isdigit() for c in pwd)):
            return pwd


# ---------------------------------------------------------------------------
# 定位本机的 Nexent 源码
#
# **不写死绝对路径。** 早先这里是某个具体路径，于是两件坏事同时发生：
#   1. 它跟着仓库公开了，暴露了开发机的目录结构；
#   2. 别人 clone 下来根本跑不了 —— 他们没有那个路径。
# 两条都指向同一个修法：默认值必须是**可推导的**，而不是某个人的实际位置。
# ---------------------------------------------------------------------------

NEXENT_SRC_ENV = "NEXENT_SRC"


def _is_nexent_src(path: pathlib.Path) -> bool:
    """靠 Nexent 自己的标志文件确认，而不是"目录存在就算"。"""
    try:
        return ((path / "deploy" / "docker" / "deploy.sh").exists()
                or (path / "backend" / "apps").exists())
    except OSError:
        return False


def find_nexent_src(project: pathlib.Path | None = None) -> pathlib.Path | None:
    """找到本机的 Nexent 源码目录，找不到返回 None。

    顺序：
      1. 环境变量 NEXENT_SRC
      2. 项目 .env 里的 NEXENT_SRC
      3. **与项目同级的 nexent_src** —— 官方部署文档推荐的位置，
         也是绝大多数人的实际布局
      4. 项目内的 nexent_src
    """
    import os

    root = pathlib.Path(project) if project else PROJECT_ROOT

    explicit = ((os.environ.get(NEXENT_SRC_ENV) or "").strip()
                or read_env(root).get(NEXENT_SRC_ENV, "").strip())
    if explicit:
        candidate = pathlib.Path(explicit)
        if _is_nexent_src(candidate):
            return candidate

    for candidate in (root.parent / "nexent_src", root / "nexent_src"):
        if _is_nexent_src(candidate):
            return candidate
    return None


def require_nexent_src(project: pathlib.Path | None = None) -> pathlib.Path:
    found = find_nexent_src(project)
    if found:
        return found
    raise LookupError(
        "找不到本机的 Nexent 源码目录。三选一：\n"
        f"  1. 在项目 .env 里填 {NEXENT_SRC_ENV}=<Nexent 源码路径>\n"
        "  2. 设同名环境变量\n"
        "  3. 把 Nexent 源码放在与本项目**同级**的 nexent_src/ 目录下\n"
        "（Nexent 源码：https://github.com/ModelEngine-Group/nexent）"
    )


def env_path(project: pathlib.Path | None = None) -> pathlib.Path:
    return (pathlib.Path(project) if project else PROJECT_ROOT) / ".env"


def read_env(project: pathlib.Path | None = None) -> dict[str, str]:
    path = env_path(project)
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_env(updates: dict[str, str], project: pathlib.Path | None = None) -> None:
    """逐行改写 `.env`，只动给定的键，注释与其它键原样保留。

    与 `app/paths.py` 里的同名函数同一套规则：整体覆盖会把用户手写的注释和
    别的配置一起抹掉。`newline="\\n"` 是为了不把换行符翻成 CRLF——
    那会让整个文件在 git diff 里变红，把真正的改动淹掉。
    """
    path = env_path(project)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append("# Nexent 租户凭据（本机生成，不进版本库）")
        for key, value in remaining.items():
            out.append(f"{key}={value}")

    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out) + "\n")


def load(project: pathlib.Path | None = None) -> tuple[str, str] | None:
    """读已保存的凭据；没有就返回 None。"""
    values = read_env(project)
    email = values.get(ENV_EMAIL_KEY, "").strip()
    password = values.get(ENV_PASSWORD_KEY, "").strip()
    if email and password:
        return email, password
    return None


def load_or_create(
    project: pathlib.Path | None = None, *, email_hint: str | None = None
) -> tuple[str, str]:
    """读凭据；没有就生成一个并写进 `.env`。

    只在开通租户时调用——其余脚本用 `require()`，读不到就报错。
    让它们回退到一个默认密码，等于把刚修掉的问题又请回来。
    """
    existing = load(project)
    if existing:
        return existing
    email = (email_hint or DEFAULT_EMAIL).strip()
    password = _strong_password()
    write_env({ENV_EMAIL_KEY: email, ENV_PASSWORD_KEY: password}, project)
    return email, password


def require(project: pathlib.Path | None = None) -> tuple[str, str]:
    """读凭据；没有就抛一个说得清的错。"""
    existing = load(project)
    if existing:
        return existing
    raise LookupError(
        "项目 .env 里没有租户凭据。先跑一次：\n"
        "    python -X utf8 scripts/provision_tenant.py\n"
        "它会在本地生成密码并写入 .env（不进版本库）。"
    )


def mask(value: str) -> str:
    """打码，用于打印。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}{'*' * 8}{value[-4:]}"


# ---------------------------------------------------------------------------
# 平台超管
#
# 超管密码是 **Nexent 部署时自动创建**的，默认值写在 Nexent 自己的源码里
# （`deploy/docker/create-su.sh`），所以它本来就不是我们的秘密。
#
# 但它仍然不该出现在**我们的**源码里：一个公开仓库里写着"Nexent 默认超管密码是
# 什么"，等于手把手教人怎么登录一个没改过默认密码的实例。所以这里不写死，
# 改成按顺序找：环境变量 → 项目 .env → Nexent 的部署日志。
# ---------------------------------------------------------------------------

ENV_SU_EMAIL_KEY = "NEXENT_SUPERADMIN_EMAIL"
ENV_SU_PASSWORD_KEY = "NEXENT_SUPERADMIN_PASSWORD"
SU_EMAIL_DEFAULT = "suadmin@nexent.com"


def _from_deploy_log(nexent_src: pathlib.Path) -> str | None:
    """从 Nexent 的部署输出里把超管密码捞出来。

    部署时它会把账号密码打印出来（"📧 Email / 🔏 Password"）。这是最可靠的来源——
    比让用户自己去翻日志再手抄一遍强。
    """
    import re

    candidates = [
        nexent_src / "deploy" / "docker" / "deploy.log",
        nexent_src / "deploy.log",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # 只找超管那一段附近的密码，避免误抓别的
        for match in re.finditer(r"(?:Password|密码)\s*[:：]\s*(\S+)", text):
            tail = text[match.end():match.end() + 200]
            head = text[max(0, match.start() - 400):match.start()]
            if "suadmin" in head or "super admin" in head.lower() or "superadmin" in head.lower():
                return match.group(1).strip()
            _ = tail
    return None


def superadmin(
    project: pathlib.Path | None = None,
    *,
    nexent_src: pathlib.Path | None = None,
    password_hint: str | None = None,
) -> tuple[str, str]:
    """拿到平台超管凭据。

    顺序：显式传入 → 环境变量 → 项目 .env → Nexent 部署日志。
    全都找不到就抛错，并说清去哪找——**不回退到任何写死的默认值**。
    """
    import os

    values = read_env(project)
    email = (
        (os.environ.get(ENV_SU_EMAIL_KEY) or "").strip()
        or values.get(ENV_SU_EMAIL_KEY, "").strip()
        or SU_EMAIL_DEFAULT
    )
    password = (
        (password_hint or "").strip()
        or (os.environ.get(ENV_SU_PASSWORD_KEY) or "").strip()
        or values.get(ENV_SU_PASSWORD_KEY, "").strip()
    )
    if password:
        return email, password

    if nexent_src:
        found = _from_deploy_log(pathlib.Path(nexent_src))
        if found:
            # 找到了就存下来，下次不必再翻日志
            write_env({ENV_SU_EMAIL_KEY: email, ENV_SU_PASSWORD_KEY: found}, project)
            return email, found

    raise LookupError(
        "找不到 Nexent 平台超管密码。三选一：\n"
        f"  1. 在项目 .env 里填 {ENV_SU_PASSWORD_KEY}=<密码>\n"
        "  2. 设同名环境变量\n"
        "  3. 确保 Nexent 的 deploy/docker/deploy.log 还在（部署时会打印该密码）\n"
        "注意：这里**不会**回退到公开的默认密码——那个密码是给全新部署用的，\n"
        "      如果没改过，请先去 Nexent 界面把它改掉。"
    )
