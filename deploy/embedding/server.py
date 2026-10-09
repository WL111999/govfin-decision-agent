"""本地向量化服务：OpenAI 兼容的 /v1/embeddings。

**为什么要有这个东西。** Nexent 的知识库必须有一个 embedding 模型——文档要先变成
向量才能被检索。而 DeepSeek 的 API **不提供 embedding 端点**（`/v1/embeddings`
返回 404），它只有对话模型。所以"用 DeepSeek 搭知识库"这条路是走不通的，不是配置
问题，是能力缺口。

三条替代路线里选了本地模型：

  1. 再注册一个云端 embedding 服务（SiliconFlow / DashScope / OpenAI）
     —— 要新账号、新 key，且数据出网。
  2. 用对话模型冒充 embedding
     —— 做不到。对话模型返回的是文本，不是定长向量，而向量检索要求同一个空间里
        可比的距离。硬凑出来的"向量"检索结果会像是随机的。
  3. **本地运行一个小模型**（本文件）
     —— 不用 key、不联网、政务数据不出本机。代价是要下一个约 100MB 的模型。

政务场景选第 3 条还有一层考虑：**数据不出网**本身就是合规要求，而不是省钱。

接口按 OpenAI 规范实现，这样 Nexent 那边只要把 base_url 指过来就能用，不需要
为它改任何代码。
"""

from __future__ import annotations

import os
import threading
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
# 缓存目录固定下来，配合容器里的 volume，避免每次重建镜像都重新下模型
CACHE_DIR = os.environ.get("EMBEDDING_CACHE", "/models")

app = FastAPI(title="govfin embedding", version="1.0.0")

_model = None
_model_lock = threading.Lock()


def _get_model():
    """懒加载 + 加锁。

    模型加载要几秒，而 uvicorn 默认单进程多线程。不加锁的话，并发来的头几个请求
    会各自加载一遍，几百 MB 的模型在内存里存好几份——显存/内存不够的机器会直接
    OOM，而报错信息只会说 "Killed"，指不回这里。
    """
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from fastembed import TextEmbedding

                _model = TextEmbedding(model_name=MODEL_NAME, cache_dir=CACHE_DIR)
    return _model


class EmbeddingRequest(BaseModel):
    input: str | list[str]
    model: str | None = None
    encoding_format: str | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "model": MODEL_NAME, "loaded": _model is not None}


@app.get("/v1/models")
def list_models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": MODEL_NAME, "object": "model", "owned_by": "govfin-local"}],
    }


@app.post("/v1/embeddings")
def embeddings(req: EmbeddingRequest) -> dict[str, Any]:
    texts = [req.input] if isinstance(req.input, str) else list(req.input)
    if not texts:
        raise HTTPException(status_code=400, detail="input 不能为空")

    model = _get_model()
    vectors = [v.tolist() for v in model.embed(texts)]

    return {
        "object": "list",
        "model": MODEL_NAME,
        "data": [
            {"object": "embedding", "index": i, "embedding": vec}
            for i, vec in enumerate(vectors)
        ],
        "usage": {
            "prompt_tokens": sum(len(t) for t in texts),
            "total_tokens": sum(len(t) for t in texts),
        },
    }
