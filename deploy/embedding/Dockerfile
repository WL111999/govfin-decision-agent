# 本地向量化服务。零 API key，政务数据不出本机。
#
# ---- 关于基底镜像 ----
# 默认用 govfin-decision-agent:1.0.0 而不是 python:3.11-slim，理由是**可用性**：
# 前者本来就是后者的基础上加了依赖构建出来的，本地必然存在；而 python:3.11-slim
# 需要从 Docker Hub 拉，国内直连经常不通（实测连续三次超时）。一个"理论上更干净
# 但拉不下来"的基底，不如一个"稍微多带点东西但一定能构建"的基底。
#
# 多带的那点东西可以接受：基底里已有的 fastapi / uvicorn 正好这里也要用，
# 省掉重复安装；多出来的 govfin 业务代码在容器里不会被导入，不影响运行。
#
# 网络通畅时想换回官方基底，加 --build-arg BASE_IMAGE=python:3.11-slim 即可。
ARG BASE_IMAGE=govfin-decision-agent:1.0.0
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EMBEDDING_CACHE=/models \
    HF_HUB_DISABLE_TELEMETRY=1

# 模型从国内镜像下：huggingface.co 在国内直连不通，走 hf-mirror.com。
# 做成可覆盖的变量而不是写死——海外部署时直连更快，"改一行环境变量"比
# "改代码再重建镜像"便宜得多。
ARG HF_ENDPOINT=https://hf-mirror.com
ENV HF_ENDPOINT=${HF_ENDPOINT}

WORKDIR /app

# 基底镜像以非 root 的 govfin 用户运行（UID 10001），而 /models 要建在根目录下，
# 所以这一段得临时提权。不写成"整个镜像都跑 root"是因为没必要——
# 提权只发生在需要写 / 的两行上，运行期仍然是普通用户。
USER root
RUN mkdir -p /models && chown -R govfin:govfin /models
USER govfin

# 只装推理需要的：fastembed 走 ONNX Runtime，**不拉 torch**。
# torch 光 wheel 就 2GB+，而这里只需要一个 512 维的中文向量模型，用不上它。
RUN pip install --no-cache-dir \
      --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
      fastembed

COPY server.py /app/server.py

# 构建期就把模型拉下来。运行期再拉的话，第一次检索会卡住好几分钟，
# 而用户看到的现象是"知识库建好了但搜不出东西"——他不会等那么久，
# 只会得出"这功能是坏的"这个结论。
RUN python -c "\
from fastembed import TextEmbedding; \
import os; \
m = TextEmbedding(model_name=os.environ.get('EMBEDDING_MODEL','BAAI/bge-small-zh-v1.5'), cache_dir='/models'); \
v = list(m.embed(['测试']))[0]; \
print('模型就绪，维度', len(v))"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
