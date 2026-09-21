"""DeepSeek provider。用 OpenAI 兼容的 /chat/completions 协议，因此同一实现
也能直接指向通义千问 DashScope、智谱 GLM、vLLM 自建端点等任何兼容服务。"""

from __future__ import annotations

import httpx

from govfin.config import LLMConfig
from govfin.errors import LLMError, LLMRateLimited, LLMResponseInvalid, LLMTimeout


class DeepSeekProvider:
    name = "deepseek"

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    def chat(self, messages: list[dict[str, str]], *, temperature: float, timeout: float) -> str:
        if not self.config.api_key:
            raise LLMError(
                "缺少 API Key。请设置环境变量 GOVFIN_LLM_API_KEY，或把 GOVFIN_LLM_PROVIDER 设为 offline 走离线确定性后端。"
            )

        url = self.config.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMTimeout(f"DeepSeek 请求超时（{timeout}s）") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"DeepSeek 网络错误: {exc}") from exc

        if resp.status_code == 429:
            raise LLMRateLimited("DeepSeek 返回 429", detail={"body": resp.text[:400]})
        if resp.status_code >= 500:
            raise LLMError(f"DeepSeek 服务端错误 {resp.status_code}", detail={"body": resp.text[:400]})
        if resp.status_code >= 400:
            raise LLMError(f"DeepSeek 请求被拒 {resp.status_code}", detail={"body": resp.text[:400]})

        try:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMResponseInvalid("DeepSeek 响应结构异常", detail={"body": resp.text[:400]}) from exc
