"""启动期的日志。

**为什么需要单独一个模块。** 打包成 `GovFin.exe` 之后没有控制台，
`sys.stdout` 和 `sys.stderr` 都是 `None`。这让"出错了"变成一件完全静默的事：

  - `print(..., file=sys.stderr)` 自己就抛 `AttributeError`（None 没有 write）
  - `sys.excepthook` 只抓主线程，而后台线程里的 uvicorn 崩了没人报
  - 双重失败的结果是：双击没反应，任何地方都查不到原因

这不是调试期的临时手段，是**这个应用必须有的一部分**：一个没有控制台的程序，
如果不主动把错误写下来，就等于把"为什么不工作"这个问题永久地藏起来了。

所以：所有启动期诊断都走 `log()`，它只依赖文件系统，不碰标准流。
"""

from __future__ import annotations

import os
import pathlib
import sys
import threading
import time
import traceback

APP_NAME = "GovFin"
_lock = threading.Lock()


def log_dir() -> pathlib.Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return pathlib.Path(base) / APP_NAME


def log_file() -> pathlib.Path:
    return log_dir() / "startup.log"


def log(message: str) -> None:
    """写一行启动日志。绝不抛异常，也绝不碰 stdout/stderr。

    这个函数会在"什么都还没准备好"的时刻被调用（连 paths.py 可能都还没导入成），
    所以它自己不依赖任何项目内的东西。
    """
    try:
        with _lock:
            path = log_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"[{stamp}] {message}\n")
    except Exception:  # noqa: BLE001 - 日志失败绝不能影响主流程
        pass


def log_exception(prefix: str, exc: BaseException | None = None) -> None:
    log(prefix)
    if exc is None:
        exc = sys.exc_info()[1]
    if exc is not None:
        log("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


class _LogStream:
    """冒充 stdout/stderr，把写进来的东西转进日志文件。

    **为什么必须有这个。** 打包成 `--windowed` 之后没有控制台，
    `sys.stdout` / `sys.stderr` 都是 `None`。而很多库会直接对它们操作：

        uvicorn/logging.py:42   self.use_colors = sys.stdout.isatty()
        → AttributeError: 'NoneType' object has no attribute 'isatty'

    这不是 uvicorn 的 bug——它假定进程有一个标准流，这个假定在常规程序里成立。
    问题在我们这边：一个没有控制台的 GUI 程序，本来就该把标准流接到一个真实的
    去处（日志文件），而不是留成 None 让每个库自己去踩。

    实测就是它导致 exe 双击后静默退出：uvicorn 起不来 → 端口等不到 →
    主线程那处写 stderr 的地方又崩一次。两层都无声。

    `isatty()` 返回 False 是对的——日志文件确实不是终端，uvicorn 据此不上色。
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, tag: str) -> None:
        self._tag = tag

    def write(self, text) -> int:
        if text:
            stripped = str(text).rstrip()
            # 空行不记：库经常写一个裸的换行，记下来只是噪声
            if stripped:
                log(f"[{self._tag}] {stripped}")
        return len(text or "")

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def fileno(self) -> int:
        # 没有真实文件描述符。抛 OSError 是标准做法（io 的约定），
        # 而不是返回一个假的 fd——那会让调用方拿着一个无效 fd 去做系统调用。
        raise OSError("日志流没有文件描述符")


def install_streams() -> None:
    """把缺失的标准流补上。

    只在它们**确实是 None** 的时候动：开发态跑 `python app/main.py` 时标准流是好的，
    替换掉反而会让人看不到输出。
    """
    if sys.stdout is None:
        sys.stdout = _LogStream("stdout")
    if sys.stderr is None:
        sys.stderr = _LogStream("stderr")


def install() -> None:
    """把主线程与**后台线程**的未捕获异常都接住。

    `threading.excepthook` 是关键的一条：uvicorn 跑在后台线程里，
    它的崩溃原本不会经过 `sys.excepthook`——这正是本次排查卡了半天的原因。
    只装主线程那个等于漏掉了一半。
    """

    def _main_hook(exc_type, exc_value, exc_tb) -> None:
        try:
            log("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
        except Exception:  # noqa: BLE001
            pass
        # 不要调 sys.__excepthook__：在没有控制台的进程里它只会再抛一次。
        # 日志已经写下了，这就够了。

    def _thread_hook(args) -> None:
        if args.exc_type is SystemExit:
            return
        try:
            log(f"后台线程 {getattr(args.thread, 'name', '?')} 崩溃：")
            log("".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = _main_hook
    threading.excepthook = _thread_hook


def rotate_if_large(max_bytes: int = 512 * 1024) -> None:
    """日志太大就清掉重来。

    这是个诊断日志，不是审计日志——保留最近的比保留全部有用。
    """
    try:
        path = log_file()
        if path.exists() and path.stat().st_size > max_bytes:
            path.write_text("", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
