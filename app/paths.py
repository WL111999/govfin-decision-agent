r"""桌面应用的路径解析。

**这一层存在的唯一理由：打包之后 `__file__` 就不再指向源码了。**

PyInstaller 把整个应用解包到一个临时目录（`sys._MEIPASS`）再运行，所以
`__file__` 指向的是那个临时目录，而不是用户的项目目录。而我们需要知道用户的项目
在哪——因为：

  - Docker 构建要拿项目当构建上下文（`deploy/Dockerfile`、`src/`、`data/`）
  - `.env` 在项目里，改 API Key 要写它

于是分两条路：

  - ``resource_path()``  → 读**打包进去**的东西（界面 HTML），走 `sys._MEIPASS`
  - ``resolve_project()`` → 找**用户的**项目目录，走下面三条候选

开发态（`python app/main.py`）两者都退化成"相对文件位置"，不需要特殊处理。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_NAME = "GovFin"

# 判断"这个目录是不是项目根"的标记文件。用两个而不是一个：
# 只有 pyproject.toml 的话，随便哪个 Python 包目录都会命中；
# 加上 deploy/Dockerfile 才能确认是**这个**项目。
PROJECT_MARKERS = ("pyproject.toml", "deploy/Dockerfile")


def is_frozen() -> bool:
    """是否跑在 PyInstaller 打出来的包里。"""
    return bool(getattr(sys, "frozen", False))


def resource_path(*parts: str) -> Path:
    """定位打包进应用的资源（界面 HTML 等）。

    打包后 `__file__` 在临时解包目录里，所以要走 `sys._MEIPASS`；
    开发态就直接相对本文件往上找 `app/`。
    """
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS"))
    else:
        base = Path(__file__).resolve().parent
    return base.joinpath(*parts)


def static_dir() -> Path:
    return resource_path("static")


def config_file() -> Path:
    """用户级配置（记着项目在哪）。

    放 `%APPDATA%\\GovFin\\` 而不是项目目录里：这份配置描述的是"这台机器上项目在
    哪"，它属于机器而不属于项目——写进项目会让它跟着 git 走，换台机器就错了。
    """
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(base) / APP_NAME / "config.json"


def load_config() -> dict:
    path = config_file()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        # 配置坏了就当没有，不要让应用起不来——它只是个"记住上次选的目录"的便利，
        # 不是运行必需的数据。让用户重新选一次目录，比给他一个打不开的软件好。
        return {}


def save_config(**updates) -> None:
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**load_config(), **updates}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def looks_like_project(path: Path) -> bool:
    """这个目录是不是 govfin 项目根。"""
    try:
        return all((path / marker).exists() for marker in PROJECT_MARKERS)
    except OSError:
        return False


def _candidates() -> list[Path]:
    """按优先级列出候选的项目目录，去重保序。"""
    found: list[Path] = []

    def add(p: Path | None) -> None:
        if p is None:
            return
        try:
            resolved = p.resolve()
        except OSError:
            return
        if resolved not in found:
            found.append(resolved)

    if is_frozen():
        # 主路径：exe 就放在项目根（build_exe.py 会复制一份过去）
        add(Path(sys.executable).parent)
        # 兼容：exe 被放在 dist/ 里的情况
        add(Path(sys.executable).parent.parent)
    else:
        # 开发态：app/ 的上一级就是项目根
        add(Path(__file__).resolve().parent.parent)

    saved = load_config().get("project_dir")
    if saved:
        add(Path(saved))

    return found


def resolve_project() -> Path | None:
    """找到用户的 govfin 项目目录，找不到返回 None。"""
    for candidate in _candidates():
        if looks_like_project(candidate):
            return candidate
    return None


def remember_project(path: Path) -> None:
    save_config(project_dir=str(Path(path).resolve()))


# ---------------------------------------------------------------------------
# .env 读写
#
# 写 .env 这件事本身不难，难的是**不破坏用户已有的内容**：他可能手写过注释、
# 调过参数、加过我们不知道的键。整体覆盖会把那些悄悄抹掉，而用户下次发现时
# 已经想不起来自己写过什么了。所以这里逐行改写：只动目标键，其余原样。
# ---------------------------------------------------------------------------

def env_path(project: Path) -> Path:
    return Path(project) / ".env"


def read_env(project: Path) -> dict[str, str]:
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


def write_env(project: Path, updates: dict[str, str]) -> None:
    """逐行改写 .env，只动 updates 里的键，注释与其它键原样保留。

    已存在但值不同的键**就地改值**（不动它的位置和相邻注释）；
    不存在的新键**追加到末尾**并加一段说明性注释。

    `newline="\\n"` 不是可有可无的：Windows 上 `write_text` 默认把 `\\n` 翻成
    `\\r\\n`，于是一次"什么都没改"的保存也会把整个文件的换行符换掉——文件在
    git diff 里整篇变红，而内容一个字没动。那种 diff 会把真正的改动淹掉。
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
        out.append("# 由 GovFin 桌面应用写入")
        for key, value in remaining.items():
            out.append(f"{key}={value}")

    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out) + "\n")


def mask_secret(value: str) -> str:
    """把密钥变成能展示的形式。

    保留前缀和末四位：前缀（sk-）能看出这是哪家的 key，末四位能让人对上是哪一把。
    中间一律星号——够长的星号，不泄露长度以外的任何信息。
    """
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}{'*' * 8}{value[-4:]}"
