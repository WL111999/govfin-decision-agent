"""LLM 抽象层：可插拔 provider + 重试 + 熔断 + 结果缓存。

设计要点：
- 上层的本体演化/推理管道只依赖 ``LLMClient.complete_json``，不感知具体厂商。
- 熔断器让上游持续故障时快速失败，而不是把调用方线程池拖死——这在批量本体演化
  （一次几百个候选概念）场景下是必需的。
- 缓存以 prompt+schema 为键，让重复的伪标注/提案生成在一次运行内只花一份钱。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Protocol, runtime_checkable

from govfin.config import LLMConfig
from govfin.errors import (
    CircuitOpen,
    LLMError,
    LLMRateLimited,
    LLMResponseInvalid,
    LLMTimeout,
)


@runtime_checkable
class LLMProvider(Protocol):
    """最小 provider 契约。实现方只需把 messages 转成一次纯文本补全。"""

    name: str

    def chat(self, messages: list[dict[str, str]], *, temperature: float, timeout: float) -> str: ...


class CircuitBreaker:
    """三态熔断器：closed → open → half_open → closed。

    只有本类自己加锁，粒度足够细（一次 complete 调用），不会成为吞吐瓶颈。
    """

    def __init__(self, threshold: int, cooldown: float) -> None:
        self._threshold = max(1, threshold)
        self._cooldown = max(0.0, cooldown)
        self._failures = 0
        self._opened_at = 0.0
        self._state = "closed"
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self) -> None:
        if self._state == "open" and time.monotonic() - self._opened_at >= self._cooldown:
            self._state = "half_open"

    def allow(self) -> bool:
        with self._lock:
            self._maybe_half_open()
            return self._state != "open"

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = "closed"

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state == "half_open" or self._failures >= self._threshold:
                self._state = "open"
                self._opened_at = time.monotonic()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "failures": self._failures,
                "threshold": self._threshold,
            }


class _TTLCache:
    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, capacity)
        self._store: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                self.hits += 1
                return self._store[key]
            self.misses += 1
            return None

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._store[key] = value
            self._store.move_to_end(key)
            while len(self._store) > self._capacity:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "size": len(self._store),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 4) if total else 0.0,
            }


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def extract_json(text: str) -> Any:
    """从 LLM 自由文本里抠出 JSON。

    真实模型经常在 JSON 外面套解释文字或 markdown 围栏，这个小工具是
    让管道在真实 LLM 下稳定工作的关键——否则一次多余的解释就会让整批演化失败。
    """
    if text is None:
        raise LLMResponseInvalid("LLM 返回为空")
    stripped = text.strip()
    for candidate in _candidates(stripped):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    raise LLMResponseInvalid("无法从 LLM 输出中解析出 JSON", detail={"raw": text[:800]})


def _candidates(text: str) -> list[str]:
    out = [text]
    out.extend(m.group(1).strip() for m in _JSON_FENCE.finditer(text))
    m = _JSON_OBJECT.search(text)
    if m:
        out.append(m.group(0))
    # 围栏内容里再抠一次对象，处理"围栏里还有解释文字"的情况
    for fenced in _JSON_FENCE.finditer(text):
        m2 = _JSON_OBJECT.search(fenced.group(1))
        if m2:
            out.append(m2.group(0))
    return [c for c in out if c]


class LLMClient:
    """带弹性能力的 LLM 门面。"""

    def __init__(self, provider: LLMProvider, config: LLMConfig) -> None:
        self.provider = provider
        self.config = config
        self.breaker = CircuitBreaker(config.circuit_failure_threshold, config.circuit_cooldown)
        self._cache = _TTLCache(config.cache_size)
        self.call_count = 0
        self.failure_count = 0
        self._lock = threading.Lock()

    # ---------- 底层补全 ----------

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        use_cache: bool = True,
    ) -> str:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        cache_key = _fingerprint(messages, temperature if temperature is not None else self.config.temperature)
        if use_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        text = self._call_with_resilience(messages, temperature)
        if use_cache:
            self._cache.put(cache_key, text)
        return text

    def complete_json(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        use_cache: bool = True,
    ) -> Any:
        text = self.complete(prompt, system=system, temperature=temperature, use_cache=use_cache)
        return extract_json(text)

    def _call_with_resilience(self, messages: list[dict[str, str]], temperature: float | None) -> str:
        if not self.breaker.allow():
            raise CircuitOpen(
                "LLM 熔断器已打开，跳过调用以避免拖垮上游",
                detail=self.breaker.snapshot(),
            )

        temp = self.config.temperature if temperature is None else temperature
        last_error: Exception | None = None

        for attempt in range(max(1, self.config.max_retries)):
            try:
                with self._lock:
                    self.call_count += 1
                text = self.provider.chat(messages, temperature=temp, timeout=self.config.timeout_seconds)
                self.breaker.record_success()
                return text
            except LLMResponseInvalid:
                # 结构性错误重试无意义，直接上抛
                self.breaker.record_success()
                raise
            except Exception as exc:  # noqa: BLE001 - provider 异常类型不可控，统一归类
                last_error = _classify(exc)
                with self._lock:
                    self.failure_count += 1
                self.breaker.record_failure()
                if attempt == self.config.max_retries - 1:
                    break
                time.sleep(self.config.backoff_base * (2**attempt))

        assert last_error is not None
        raise last_error

    def stats(self) -> dict:
        return {
            "provider": self.provider.name,
            "model": self.config.model,
            "calls": self.call_count,
            "failures": self.failure_count,
            "cache": self._cache.stats(),
            "circuit": self.breaker.snapshot(),
        }

    def clear_cache(self) -> None:
        self._cache.clear()


def _fingerprint(messages: list[dict[str, str]], temperature: float) -> str:
    payload = json.dumps({"m": messages, "t": temperature}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _classify(exc: Exception) -> LLMError:
    if isinstance(exc, LLMError):
        return exc
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "timeout" in name or "timeout" in msg:
        return LLMTimeout(f"LLM 调用超时: {exc}")
    if "429" in msg or "rate" in msg and "limit" in msg:
        return LLMRateLimited(f"LLM 触发限流: {exc}")
    return LLMError(f"LLM 调用失败: {exc}")
