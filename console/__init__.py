"""控制台包。

原本 `console/` 只是个放 Dockerfile 和 app.py 的目录，容器里跑
`uvicorn app:app` 不需要它是个包。桌面端要在同一个进程里 import 它
（`from console.app import app`），所以补上这个文件——**逻辑一行没动**。

别删掉它：删了之后容器照常能跑，桌面端会 ImportError，而那种"一边好一边坏"
的失败最容易被当成玄学。
"""
