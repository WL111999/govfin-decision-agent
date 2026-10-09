"""仅供开发时在浏览器里预览界面用（不起原生窗口）。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from server import app
import uvicorn
uvicorn.run(app, host="127.0.0.1", port=8124, log_level="warning")
