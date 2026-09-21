"""Provider 装配。"""

from __future__ import annotations

from govfin.config import LLMConfig, get_settings
from govfin.errors import LLMError
from govfin.llm.base import LLMClient, LLMProvider
from govfin.llm.deepseek import DeepSeekProvider
from govfin.llm.offline import OfflineProvider

# 全部走 OpenAI 兼容协议的厂商，都能复用 DeepSeekProvider
_OPENAI_COMPATIBLE = {
    "deepseek": "https://api.deepseek.com/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "modelscope": "https://api-inference.modelscope.cn/v1",
    "openai": "https://api.openai.com/v1",
    "custom": "",
}

_DEFAULT_MODEL = {
    "deepseek": "deepseek-chat",
    "dashscope": "qwen-plus",
    "qwen": "qwen-plus",
    "zhipu": "glm-4-plus",
    "modelscope": "Qwen/Qwen2.5-72B-Instruct",
    "openai": "gpt-4o-mini",
}


def build_provider(config: LLMConfig | None = None) -> LLMProvider:
    cfg = config or get_settings().llm
    name = (cfg.provider or "offline").strip().lower()

    if name == "offline":
        return OfflineProvider()
    if name not in _OPENAI_COMPATIBLE:
        raise LLMError(f"未知的 LLM provider: {name}（支持 offline / {', '.join(sorted(_OPENAI_COMPATIBLE))}）")

    if not cfg.base_url:
        cfg.base_url = _OPENAI_COMPATIBLE[name]
    if cfg.model in ("", "deepseek-chat") and name != "deepseek":
        cfg.model = _DEFAULT_MODEL.get(name, cfg.model)
    return DeepSeekProvider(cfg)


def build_client(config: LLMConfig | None = None) -> LLMClient:
    cfg = config or get_settings().llm
    return LLMClient(build_provider(cfg), cfg)


def build_client_for_tests(
    *,
    fail_on_calls: set[int] | None = None,
    always_fail: str | None = None,
    **overrides,
) -> LLMClient:
    """测试专用：确定性后端 + 可脚本化故障注入。"""
    base = get_settings().llm
    cfg = LLMConfig(
        provider="offline",
        api_key="",
        base_url=base.base_url,
        model="offline-deterministic",
        temperature=0.0,
        timeout_seconds=overrides.pop("timeout_seconds", base.timeout_seconds),
        max_retries=overrides.pop("max_retries", base.max_retries),
        backoff_base=overrides.pop("backoff_base", 0.0),
        circuit_failure_threshold=overrides.pop("circuit_failure_threshold", base.circuit_failure_threshold),
        circuit_cooldown=overrides.pop("circuit_cooldown", base.circuit_cooldown),
        cache_size=overrides.pop("cache_size", 0),
    )
    provider = OfflineProvider(fail_on_calls=fail_on_calls, always_fail=always_fail)
    return LLMClient(provider, cfg)
