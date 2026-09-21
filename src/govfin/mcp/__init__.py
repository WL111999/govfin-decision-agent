"""MCP 工具层：把领域能力暴露为 ModelEngine Nexent 可编排的 MCP tool。"""

from govfin.mcp.server import SERVER_NAME, SERVER_VERSION, build_server, main

__all__ = ["SERVER_NAME", "SERVER_VERSION", "build_server", "main"]
