r"""桌面应用入口：起本地服务 → 开原生窗口。

开发态跑法：
    python -X utf8 app/main.py

打包成 exe 后由 PyInstaller 直接调 `main()`。

**为什么先起服务再开窗。** 窗口一打开就会去请求 `/api/...`，服务没起来的话
页面会闪一下"连接失败"再自己好——那种闪烁让人怀疑软件是不是有问题。
先把端口听上，再开窗，页面第一次加载就是完整的。

**为什么全程写日志文件。** 打包后没有控制台，`sys.stdout`/`sys.stderr` 都是
`None`。这意味着任何失败都是**静默**的：双击没反应，看不出卡在哪。

实测踩到过：uvicorn 在后台线程里起不来，而 `sys.excepthook` 不抓非主线程异常，
主线程那句 `print(..., file=sys.stderr)` 又因为 stderr 是 None 自己先崩了——
两个洞叠在一起，整个启动过程没有任何线索。所以这里：

  - 所有诊断走 `logging_setup.log()`（只写文件，不碰标准流）
  - 同时装主线程与后台线程的异常钩子
  - 每个阶段都记一行，失败时能直接看出走到哪一步
"""

from __future__ import annotations

import socket
import sys
import threading
import time

if __package__ in (None, ""):
    # 直接 `python app/main.py` 跑的时候，app/ 自己在 sys.path 里，
    # 但项目根不在——而 server.py 要用 `import paths` 这种同目录导入。
    import pathlib

    _APP_DIR = pathlib.Path(__file__).resolve().parent
    sys.path.insert(0, str(_APP_DIR))
    sys.path.insert(0, str(_APP_DIR.parent))

import logging_setup  # noqa: E402


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
    """在后台线程里跑本地服务。

    **必须接住异常**。这个函数跑在后台线程里，而 `sys.excepthook` 不管非主线程——
    原本 uvicorn 在这里起不来时，异常直接消失，主线程那边只会看到"等不到端口"
    然后自己也崩在写 stderr 上。
    """
    try:
        import uvicorn

        from server import app

        logging_setup.log(f"uvicorn 启动中… host=127.0.0.1 port={port}")
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
        logging_setup.log("uvicorn 退出（serve 返回）")
    except BaseException as exc:  # noqa: BLE001 - 后台线程的任何异常都要留下痕迹
        logging_setup.log_exception("uvicorn 启动失败：", exc)


def _wait_ready(port: int, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main() -> int:
    logging_setup.rotate_if_large()
    # 顺序要紧：先把标准流补上，再装异常钩子。
    # 没有控制台的进程里 sys.stdout/stderr 是 None，而 uvicorn 这类库会直接
    # 调 `sys.stdout.isatty()`——实测就是它在打包后把启动整个搞崩的。
    logging_setup.install_streams()
    logging_setup.install()
    logging_setup.log("=" * 50)
    logging_setup.log(f"启动；frozen={getattr(sys, 'frozen', False)} "
                      f"stdout={'None' if sys.stdout is None else 'ok'} "
                      f"stderr={'None' if sys.stderr is None else 'ok'}")

    try:
        import paths
    except BaseException as exc:  # noqa: BLE001
        logging_setup.log_exception("导入 paths 失败：", exc)
        return 1

    try:
        project = paths.resolve_project()
    except BaseException as exc:  # noqa: BLE001
        logging_setup.log_exception("解析项目目录失败：", exc)
        project = None
    logging_setup.log(f"项目目录：{project}")

    port = _free_port()
    logging_setup.log(f"选中端口：{port}")

    threading.Thread(target=_serve, args=(port,), daemon=True, name="uvicorn").start()

    if not _wait_ready(port):
        logging_setup.log("本地服务未在 25 秒内就绪，放弃启动窗口")
        # 不写 stderr：没有控制台时它是 None，写了会再抛一次，
        # 把真正的失败原因盖掉。日志文件里已经有完整记录了。
        return 1
    logging_setup.log("本地服务已就绪")

    try:
        import webview
    except BaseException as exc:  # noqa: BLE001
        logging_setup.log_exception("导入 pywebview 失败（WebView2 运行时是否可用？）：", exc)
        return 1

    url = f"http://127.0.0.1:{port}"
    try:
        webview.create_window(
            "GovFin 决策工作台",
            url,
            width=1440,
            height=920,
            min_size=(1080, 700),
            background_color="#0a0e1a",
            text_select=True,
        )
        logging_setup.log(f"窗口已创建 → {url}")
        if project is None:
            # 找不到项目不拦着开窗：决策可视化在服务已部署的情况下照常能用，
            # 只是"部署"页会给引导。拦下来会让人以为软件坏了。
            logging_setup.log("提示：未找到项目目录，部署功能需要先在界面里选择")
        webview.start()
        logging_setup.log("窗口关闭，正常退出")
        return 0
    except BaseException as exc:  # noqa: BLE001
        logging_setup.log_exception("窗口启动失败：", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
