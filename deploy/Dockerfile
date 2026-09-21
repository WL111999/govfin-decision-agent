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

# 这里**必须用 bash 而不是 sh**。Debian 的 /bin/sh 是 dash，而 `wait -n`
# （任一子进程退出就返回）是 bash 的扩展，dash 不认，会直接报
# `wait: Illegal option -n` 并让容器以退出码 2 结束——镜像构建得好好的，
# 容器却起不来。基础镜像里本来就带 bash（Debian 的 bash 是 Essential），所以
# 不需要额外装，只要显式点名。
#
# 用 `wait -n` 而不是 `wait`：后者要等全部子进程结束，MCP 挂了而 A2A 还活着时
# 容器会一直显示 running，健康检查却查不到 MCP——这正是本文件反复在消除的
# 那种"看起来正常"的失效。
CMD ["bash", "-c", "python -m govfin.mcp.server --transport streamable-http --port ${GOVFIN_MCP_PORT} & python -m govfin.a2a.server --port ${GOVFIN_A2A_PORT} & wait -n"]

EXPOSE 8930 8940

# 必须用 CMD 的 **shell 形式**（而不是 `CMD ["curl", ...]` 的 exec 形式）：
# exec 形式不经 shell，${GOVFIN_A2A_PORT} 会被原样当字面量传给 curl，健康检查
# 恒定失败——而且失败得不明显（容器一直是 unhealthy，但服务其实是好的）。
#
# 注意这里**不能**写 `CMD-SHELL`：那是 docker-compose.yml 里的关键字
# （`healthcheck.test: CMD-SHELL ...`），Dockerfile 的 HEALTHCHECK 只认 `CMD`，
# 写成 CMD-SHELL 会在解析阶段直接报错，连镜像都构建不出来。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://localhost:${GOVFIN_A2A_PORT}/health" || exit 1
