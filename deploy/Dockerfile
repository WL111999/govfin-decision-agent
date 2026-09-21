# govfin-decision-agent：MCP 工具服务 + A2A 协作服务
#
# 单镜像双入口。MCP 和 A2A 共用同一个 AgentRuntime 与同一份图数据库，
# 分成两个镜像会让两边的图状态出现分叉——A2A 编排出的决策写进运行时图，
# 而 MCP 工具查不到，这在溯源场景里是致命的。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    GOVFIN_DATA_DIR=/app/data \
    GOVFIN_GRAPH_DB=/app/data/govfin_graph.db

WORKDIR /app

# 中文字体：多模态解析要处理中文营业执照图片，缺字体时 OCR 输出会是空白方框
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl tini libgl1 libglib2.0-0 fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/pyproject.toml
COPY src/ /app/src/
RUN pip install --no-cache-dir -e /app

COPY data/ /app/data/
COPY scripts/ /app/scripts/
COPY tests/ /app/tests/
COPY nexent/ /app/nexent/

# 构建期先把样例数据导进去，避免容器首次启动时冷启动等待。
# 随后立刻自检：图空了、层缺了、MCP 工具少注册了，都在这里就让构建失败。
# 这三类问题在运行期都是**静默**的——图空只会让结论变空，不会抛错。
RUN govfin ingest --data /app/data && govfin doctor

# 非 root 运行。图数据库需要可写，提前把属主改掉。
RUN useradd --create-home --uid 10001 govfin \
 && mkdir -p /app/data \
 && chown -R govfin:govfin /app
USER govfin

ENV GOVFIN_MCP_PORT=8930 \
    GOVFIN_A2A_PORT=8940

# tini 做 PID 1：uvicorn 和 stdio MCP 都涉及子进程，缺 init 会留下僵尸进程
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "python -m govfin.mcp.server --transport streamable-http --port ${GOVFIN_MCP_PORT} & python -m govfin.a2a.server --port ${GOVFIN_A2A_PORT} & wait -n"]

EXPOSE 8930 8940

# 必须用 CMD-SHELL：exec 形式的 CMD 不做变量展开，${GOVFIN_A2A_PORT} 会被原样
# 当字面量传给 curl，健康检查恒定失败——而且失败得不明显（容器一直是 unhealthy，
# 但服务其实是好的）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD-SHELL curl -fsS "http://localhost:${GOVFIN_A2A_PORT}/health" || exit 1
