r"""桌面应用入口：起本地服务 → 开原生窗口。

开发态跑法：
    python -X utf8 app/main.py

打包成 exe 后由 PyInstaller 直接调 `main()`。

**为什么先起服务再开窗。** 窗口一打开就会去请求 `/api/...`，服务没起来的话
页面会闪一下"连接失败"再自己好——那种闪烁让人怀疑软件是不是有问题。
先把端口听上，再开窗，页面第一次加载就是完整的。
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import traceback

if __package__ in (None, ""):
    # 直接 `python app/main.py` 跑的时候，app/ 自己在 sys.path 里，
    # 但项目根不在——而 server.py 要用 `import paths` 这种同目录导入。
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def _free_port() -> int:
    """要一个空闲端口。

    不用固定端口：8090 可能被网页控制台占着，而桌面应用和它并存是完全正常的
    （一个在浏览器里看，一个在原生窗口里用）。固定端口会让第二个启动失败，
    而失败原因是"端口被占用"——用户很难想到是自己刚才开的那个控制台。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve(port: int) -> None:
    import uvicorn

    from server import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)


def _wait_ready(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _install_excepthook() -> None:
    """未捕获异常写进一个日志文件。

    打包成 `--windowed` 之后**没有控制台**，异常会静默消失——软件双击没反应，
    而用户拿不到任何线索。所以兜底写到文件里，让它至少是可查的。
    """

    def hook(exc_type, exc_value, exc_tb) -> None:
        try:
            import paths

            log = paths.config_file().parent / "error.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as fh:
                fh.write(f"\n{'=' * 60}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                traceback.print_exception(exc_type, exc_value, exc_tb, file=fh)
        except Exception:  # noqa: BLE001
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = hook


def main() -> int:
    _install_excepthook()

    import paths

    project = paths.resolve_project()
    port = _free_port()

    threading.Thread(target=_serve, args=(port,), daemon=True).start()
    if not _wait_ready(port):
        print("本地服务启动失败", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{port}"

    import webview

    webview.create_window(
        "GovFin 决策工作台",
        url,
        width=1440,
        height=920,
        min_size=(1080, 700),
        background_color="#0a0e1a",
        text_select=True,
    )

    if project is None:
        # 找不到项目不拦着开窗：决策可视化在服务已部署的情况下照常能用，
        # 只是"部署"页会给引导。拦下来会让人以为软件坏了。
        print(f"提示：没找到 govfin 项目目录，部署功能需要先在界面里选择目录", file=sys.stderr)

    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
