r"""把桌面应用打包成单个 GovFin.exe。

跑法：
    python -X utf8 scripts/build_exe.py

产物：`dist/GovFin.exe`（约 60–80MB），并复制一份到项目根。

**为什么产物要复制到项目根。** exe 启动时要找项目目录（Docker 构建需要它当上下文、
API Key 写在它的 .env 里）。`app/paths.py` 的第一条候选就是"exe 所在目录"——
把 exe 放在项目根，它自己就找到了，用户不用做任何配置。

**关于 hidden import。** PyInstaller 靠静态分析找依赖，而下面这些是运行时才决定的
字符串式导入，静态分析看不见，不显式声明就会打出一个"能启动但一用就崩"的包：

  - uvicorn 的 loop / protocol 实现，它按名字动态加载
  - pywebview 的平台后端（Windows 上走 EdgeChromium/WebView2 那条）
  - pythonnet 的 clr_loader，它是 .NET 互操作的入口

这些是**试出来的**而不是查出来的：每一条都对应一次"打包成功、运行失败"。
所以这个列表值得原样保留，别看着冗余就删。
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SEP = ";" if sys.platform == "win32" else ":"

HIDDEN_IMPORTS = [
    # uvicorn：按字符串动态加载实现，静态分析看不见
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    # pywebview：平台后端同样是运行时选择的
    "webview",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    # pythonnet / .NET 互操作
    "clr_loader",
    "pythonnet",
    # 控制台复用进来的东西
    "console",
    "console.app",
    # 应用自己的模块（都在 app/ 下，按路径加载的）
    "paths",
    "deploy",
    "server",
]


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("✗ 没装 pyinstaller。先跑：")
        print("    pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pyinstaller")
        return 1

    for required in ("app/main.py", "app/static/index.html", "console/app.py"):
        if not (ROOT / required).exists():
            print(f"✗ 缺少 {required}")
            return 1

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",
        # --windowed：不要控制台窗口。副作用是异常不再打印到任何地方，
        # 所以 app/main.py 里装了 excepthook 把未捕获异常写进日志文件。
        "--windowed",
        "--name", "GovFin",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build" / "exe"),
        "--specpath", str(ROOT / "build"),
        # 界面必须打进包里
        "--add-data", f"{ROOT / 'app' / 'static'}{SEP}static",
        # 控制台那份前端也带上：桌面端复用 console.app，它 import 时会引用到
        "--add-data", f"{ROOT / 'console'}{SEP}console",
        # 项目根加进搜索路径，让 `import paths` / `import console.app` 能找到
        "--paths", str(ROOT / "app"),
        "--paths", str(ROOT),
    ]
    for name in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", name]
    # 明确排除：这些是开发/测试期的东西，打进去只会让包变大
    for name in ("pytest", "hypothesis", "tkinter", "matplotlib", "pandas", "numpy"):
        cmd += ["--exclude-module", name]

    cmd.append(str(ROOT / "app" / "main.py"))

    print("打包中…（第一次要几分钟）")
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print("✗ 打包失败，见上面的输出")
        return result.returncode

    exe = ROOT / "dist" / ("GovFin.exe" if sys.platform == "win32" else "GovFin")
    if not exe.exists():
        print(f"✗ 打包结束但没找到产物：{exe}")
        return 1

    # 复制到项目根——app/paths.py 的第一条候选就是"exe 所在目录"，
    # 放这儿它自己就找到项目了，用户不用配任何东西。
    target = ROOT / exe.name
    shutil.copy2(exe, target)

    size_mb = exe.stat().st_size / 1024 / 1024
    print()
    print(f"✓ 完成：{exe}  （{size_mb:.1f} MB）")
    print(f"✓ 已复制到：{target}")
    print()
    print("双击运行即可。窗口里第一次打开会稍慢（onefile 要先解包）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
