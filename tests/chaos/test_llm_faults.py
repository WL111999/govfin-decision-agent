"""LLM 故障注入：真实模型会怎么坏，坏了之后系统会变成什么样。

外部依赖的失效方式不是"抛一个漂亮的异常"，而是五花八门：连接被重置、
返回 200 但正文是空的、返回一段解释文字再跟半个 JSON、限流、超时、
返回一屏 HTML 错误页。每一条都要有明确的、**受控的**归宿。

这里不测"重试逻辑写对了吗"，测的是三件更要紧的事：

1. **非受控异常不得逃逸**。调用方拿到 traceback 就无法判断该重试还是该终止，
   只能整个流程失败——这是"部分可用"和"完全不可用"的分界。
2. **缓存不得被故障污染**。失败的调用若被当成结果写进缓存，后续所有
   看起来正常的查询都会拿到那一份失败产物。这是最隐蔽的一类，因为
   它让故障在时间上"传染"到之后的所有请求。
3. **熔断之后必须真的停下来**。熔断器存在的前提是它能阻止调用打到已经
   确定不可用的上游。若它只改状态码却照旧发请求，那它就只是个装饰品。
"""

from __future__ import annotations

import threading

import pytest

from govfin.config import LLMConfig
from govfin.errors import (
    CircuitOpen,
    GovFinError,
    LLMError,
    LLMRateLimited,
    LLMResponseInvalid,
    LLMTimeout,
)
from govfin.llm.base import CircuitBreaker, LLMClient, extract_json

pytestmark = pytest.mark.chaos


# ----------------------------------------------------------------------
# 故障注入用的 provider
# ----------------------------------------------------------------------


class ScriptedProvider:
    """按剧本回答，并记录每一次调用。

    记录调用次数是关键：重试、熔断、缓存这三件事的**唯一**可观测证据就是
    "上游到底被调了几次"。只看返回值永远分不清"重试了三次"和"命中缓存了"。
    """

    name = "scripted"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def chat(self, messages, *, temperature, timeout):
        with self._lock:
            self.calls.append({"messages": messages, "temperature": temperature, "timeout": timeout})
            index = len(self.calls) - 1
        scripted = self.responses[index] if index < len(self.responses) else self.responses[-1]
        if isinstance(scripted, Exception):
            raise scripted
        return scripted


def _config(**overrides) -> LLMConfig:
    """小参数配置：重试 3 次、退避 0 秒、熔断阈值 3。

    退避必须置 0，否则这套测试会真的睡满 0.5+1+2 秒——
    故障注入测试跑得慢，就会被排除出日常回归，然后永远不再被执行。
    """
    base = {
        "max_retries": 3,
        "backoff_base": 0.0,
        "circuit_failure_threshold": 3,
        "circuit_cooldown": 60.0,
        "cache_size": 8,
        "timeout_seconds": 1.0,
        "temperature": 0.0,
    }
    base.update(overrides)
    return LLMConfig(**base)


# ----------------------------------------------------------------------
# 故障分类
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raised,expected",
    [
        (TimeoutError("connection timed out"), LLMTimeout),
        (Exception("HTTP 429 Too Many Requests"), LLMRateLimited),
        (Exception("rate limit exceeded"), LLMRateLimited),
        (ConnectionResetError("connection reset by peer"), LLMError),
        (OSError("network unreachable"), LLMError),
        (ValueError("unexpected payload"), LLMError),
    ],
    ids=["timeout", "429", "限流文案", "连接重置", "网络不可达", "未知负载"],
)
def test_provider_failures_classify_into_domain_errors(raised, expected):
    """上游各种原生异常必须被归到领域异常，且分类要能被运维直接用。

    "超时"和"限流"的处理方式完全不同——前者该放长 timeout 或减少并发，
    后者该退避或换 key。全都归成一个 LLMError 等于把判断责任推给调用方，
    而调用方只能看到一句字符串。
    """
    client = LLMClient(ScriptedProvider(raised), _config())
    with pytest.raises(expected) as exc:
        client.complete("任意提问")

    assert isinstance(exc.value, GovFinError), "必须落在领域异常体系内，否则 MCP 层无法映射"
    assert exc.value.code
    assert exc.value.retryable is True
    assert exc.value.to_dict()["message"]


def test_retries_exactly_max_retries_times_then_gives_up():
    """重试次数必须精确等于配置值。

    多一次是浪费，少一次是可靠性打折。用 provider 的调用次数而不是
    "最终是否抛异常"来断言，因为后者在重试 1 次和重试 10 次时表现一样。
    """
    provider = ScriptedProvider(TimeoutError("timeout"))
    client = LLMClient(provider, _config(max_retries=3))

    with pytest.raises(LLMTimeout):
        client.complete("任意提问")
    assert len(provider.calls) == 3
    assert client.stats()["failures"] == 3


def test_success_after_transient_failures_is_returned():
    """瞬时故障后的成功必须被正常返回，而不是被之前的失败带偏。"""
    provider = ScriptedProvider(TimeoutError("t1"), TimeoutError("t2"), "最终答案")
    client = LLMClient(provider, _config(max_retries=3))

    assert client.complete("任意提问") == "最终答案"
    assert len(provider.calls) == 3
    assert client.stats()["failures"] == 2, "失败计数应保留，它是上游健康状况的信号"
    assert client.breaker.state == "closed", "成功后熔断器必须复位"


# ----------------------------------------------------------------------
# 响应体本身是坏的
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   \n  ",
        "抱歉，我不能协助这个请求。",
        "这是一段解释文字，后面没有 JSON。",
        '{"status": "ok", ',          # 截断的 JSON
        "<html><body>502 Bad Gateway</body></html>",
        "```json\n{不是合法 JSON}\n```",
    ],
    ids=["空", "纯空白", "拒答", "纯文字", "截断", "错误页", "围栏内非法"],
)
def test_unparseable_body_raises_controlled_invalid(body):
    """返回 200 但正文不是 JSON —— 必须报"响应非法"，而不是解析崩溃。

    这条路径现实中比网络故障更常见：模型拒答、被内容策略拦下、
    上游反向代理返回错误页，全都是 HTTP 200。
    """
    provider = ScriptedProvider(body)
    with pytest.raises(GovFinError) as exc:
        LLMClient(provider, _config()).complete_json("任意提问")
    assert isinstance(exc.value, LLMResponseInvalid)
    assert exc.value.retryable is False, "内容是结构性问题，重试同样的提问只会得到同样的东西"


def test_invalid_response_is_not_retried():
    """响应非法不得重试——它对同一个提问是确定性的。

    把它当瞬时故障重试，等于每次调用都烧三倍的钱去拿到同一个错误。
    """
    provider = ScriptedProvider("这不是 JSON")
    with pytest.raises(LLMResponseInvalid):
        LLMClient(provider, _config(max_retries=3)).complete_json("任意提问")
    assert len(provider.calls) == 1, "响应非法却重试了"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('好的，结果如下：\n{"a": 1}\n以上。', {"a": 1}),
        ('```json\n说明文字\n{"a": 1}\n```', {"a": 1}),
        ('[{"a": 1}]', [{"a": 1}]),
    ],
    ids=["纯对象", "json 围栏", "裸围栏", "前后有解释", "围栏内还有解释", "数组"],
)
def test_extract_json_tolerates_real_model_output(raw, expected):
    """真实的模型输出几乎不会是一条干净的 JSON。

    这个容错是让整条演化管道在真实 LLM 下能跑通的关键：一次多余的解释
    就会让整批几百个候选概念的演化全部失败。
    """
    assert extract_json(raw) == expected


# ----------------------------------------------------------------------
# 熔断
# ----------------------------------------------------------------------


def test_circuit_opens_and_then_stops_calling_upstream():
    """熔断打开后，请求不得再打到上游。

    这是熔断器唯一的存在理由。只改内部状态却继续发请求，等于给一个
    已经确定不可用的上游继续加压，同时让调用方多等一个完整的超时。
    """
    provider = ScriptedProvider(TimeoutError("timeout"))
    client = LLMClient(provider, _config(max_retries=1, circuit_failure_threshold=3))

    for _ in range(3):
        with pytest.raises(LLMError):
            client.complete("提问", use_cache=False)
    assert len(provider.calls) == 3
    assert client.breaker.state == "open"

    with pytest.raises(CircuitOpen) as exc:
        client.complete("换一个提问", use_cache=False)
    assert len(provider.calls) == 3, "熔断打开后仍然调用了上游"
    assert exc.value.detail["state"] == "open"
    assert exc.value.detail["failures"] >= 3


def test_circuit_half_opens_after_cooldown_and_recovers_on_success():
    """冷却期一过就放行探测，成功即恢复。

    冷却期设 0 秒让这条路径可测且不用 sleep：三态熔断器的价值全在
    half_open 这一步，没有它就只能等进程重启。
    """
    provider = ScriptedProvider(TimeoutError("t"))
    client = LLMClient(
        provider, _config(max_retries=1, circuit_failure_threshold=2, circuit_cooldown=0.0)
    )

    for _ in range(2):
        with pytest.raises(LLMError):
            client.complete("提问", use_cache=False)
    # cooldown=0 意为"不冷却"，因此状态一经读取就已经是半开，观察不到 open。
    assert client.breaker.state == "half_open"

    provider.responses = ["恢复了"]
    assert client.complete("提问", use_cache=False) == "恢复了"
    assert client.breaker.state == "closed"
    assert client.breaker.snapshot()["failures"] == 0


def test_circuit_breaker_state_transitions_are_consistent():
    """三态迁移的完整走位：closed → open → half_open → open → half_open → closed。

    单独测熔断器是因为它的状态机里带时间因素，混在客户端里测要么依赖
    sleep、要么观察不到某些状态。这里用两个冷却期把两段行为分开验证，
    并且刻意区分两个观测口径：

    - ``snapshot()["state"]`` 是**原始状态**，不做时间推算，用于核查状态机走位。
    - ``.state`` / ``allow()`` 是**时间视角**，会把到期的 open 折算成 half_open。

    两者都必须测：只有原始状态，无法说明冷却期有没有真的挡住请求；
    只有时间视角，则零冷却下永远观察不到 open，状态机等于没法验证。
    """
    # 长冷却：open 是一个稳定可观察的状态，请求必须被挡住
    holding = CircuitBreaker(threshold=2, cooldown=60.0)
    assert holding.state == "closed"
    assert holding.allow() is True

    holding.record_failure()
    holding.record_failure()
    assert holding.snapshot()["state"] == "open"
    assert holding.state == "open", "冷却期内时间视角也应是 open"
    assert holding.allow() is False, "冷却期内不得放行"

    # 零冷却：不冷却 ⇒ 打开即半开，因此只能靠原始状态验证走位
    probing = CircuitBreaker(threshold=2, cooldown=0.0)
    probing.record_failure()
    probing.record_failure()
    assert probing.snapshot()["state"] == "open"
    assert probing.state == "half_open", "零冷却下读取状态即应折算为半开"
    assert probing.allow() is True, "半开必须放行探测请求"

    probing.record_failure()
    assert probing.snapshot()["state"] == "open", "半开探测失败必须立刻回到打开"

    probing.record_success()
    assert probing.snapshot()["state"] == "closed"
    assert probing.snapshot()["failures"] == 0


def test_circuit_threshold_is_at_least_one():
    """阈值为 0 或负数时必须被夹到 1，而不是一上来就永久熔断。

    阈值来自环境变量。配成 0 的人想表达的通常是"关掉熔断"，
    若照字面实现就变成"第一次失败即熔断"，方向正好相反。
    """
    breaker = CircuitBreaker(threshold=0, cooldown=60.0)
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.snapshot()["threshold"] == 1


# ----------------------------------------------------------------------
# 缓存
# ----------------------------------------------------------------------


def test_identical_prompt_hits_cache():
    """同一提问在同一轮运行内只应花一次钱。"""
    provider = ScriptedProvider("答案")
    client = LLMClient(provider, _config())

    assert client.complete("同样的问题") == "答案"
    assert client.complete("同样的问题") == "答案"
    assert len(provider.calls) == 1
    assert client.stats()["cache"]["hits"] == 1


def test_cache_key_includes_temperature_and_system_prompt():
    """温度与 system 提示词必须参与缓存键。

    漏掉温度会让"保守取样"和"发散取样"共用同一份结果——在提案生成
    这种靠多样性吃饭的场景里，表现为所有候选都一模一样，且没有任何报错。
    """
    provider = ScriptedProvider("答案")
    client = LLMClient(provider, _config())

    client.complete("问题", temperature=0.0)
    client.complete("问题", temperature=0.9)
    client.complete("问题", temperature=0.0, system="你是审查员")
    assert len(provider.calls) == 3, "温度或 system 不同的调用被错误地当成了同一次"


def test_failures_are_never_cached():
    """失败的调用绝不能进缓存。

    这是本文件最重要的一条：一旦失败产物被缓存，之后的每一次查询都会
    命中那份垃圾，故障于是在时间上无限传染——而且修复上游之后依然如此，
    因为缓存里躺着的是"上一次的失败"。除了重启没有任何办法。
    """
    provider = ScriptedProvider(TimeoutError("上游挂了"))
    client = LLMClient(provider, _config(max_retries=1))

    with pytest.raises(LLMError):
        client.complete("问题")
    assert client.stats()["cache"]["size"] == 0, "失败被写进了缓存"

    provider.responses = ["上游恢复了"]
    assert client.complete("问题") == "上游恢复了", "缓存里残留的失败产物挡住了正常的重试"
    assert len(provider.calls) == 2


def test_use_cache_false_always_calls_provider():
    """显式禁用缓存时必须真的打上游——探测上游健康状况依赖这一点。"""
    provider = ScriptedProvider("答案")
    client = LLMClient(provider, _config())

    for _ in range(3):
        client.complete("问题", use_cache=False)
    assert len(provider.calls) == 3


def test_cache_evicts_oldest_beyond_capacity():
    """缓存容量受限，超出后按最久未用淘汰，不得无限增长。

    无界缓存在批量演化（一次几百个候选）里会稳定吃掉几百 MB——
    容器内存上限一到就是 OOM，而症状看起来和 LLM 毫无关系。
    """
    provider = ScriptedProvider("答案")
    client = LLMClient(provider, _config(cache_size=4))

    for i in range(20):
        client.complete(f"问题 {i}")
    assert client.stats()["cache"]["size"] == 4
    assert client.stats()["cache"]["size"] <= 4


def test_clear_cache_forces_refetch():
    provider = ScriptedProvider("答案")
    client = LLMClient(provider, _config())
    client.complete("问题")
    client.clear_cache()
    client.complete("问题")
    assert len(provider.calls) == 2


# ----------------------------------------------------------------------
# 演化管道在 LLM 全挂时的降级
# ----------------------------------------------------------------------


def test_evolution_survives_permanently_broken_llm():
    """LLM 永久不可用时，本体演化必须仍然走完并产出可审计的结果。

    这是项目对外的核心承诺之一："没有 key 也能跑，LLM 只用于语义补全"。
    承诺若只在文档里成立，部署到无外网环境时才会发现整条演化链路是断的。
    这里注入一个永远抛异常的客户端，验证管道**不中断**。
    """
    from govfin.evolution.pipeline import OntologyEvolutionPipeline
    from govfin.evolution.unk_pool import KIND_ENTITY
    from govfin.graph.store import GraphStore
    from govfin.ontology.seed import build_seed_ontology

    store = GraphStore(in_memory=True, ontology=build_seed_ontology())
    broken = LLMClient(ScriptedProvider(TimeoutError("LLM 永久不可用")), _config(max_retries=1))
    pipeline = OntologyEvolutionPipeline(store, ontology=store.ontology, client=broken, use_llm=True)

    for i, text in enumerate(("经营异常名录", "经营异常名录", "异常经营名录", "经营异常名录")):
        pipeline.pool.observe(
            text,
            KIND_ENTITY,
            source_document=f"doc:chaos-{i}",
            context="企业被列入经营异常名录",
            hint_type="工商登记",
        )

    report = pipeline.run_cycle(commit=False)
    payload = report.to_dict()

    assert payload["cycle"] >= 1
    assert payload["pool_size"] > 0
    # 关键：结论不能因为 LLM 挂了就消失。符号对齐自己就能识别出这个概念，
    # 只是语义细节（父类、属性）会缺省。
    assert payload["version_before"] == payload["version_after"], "dry-run 不应推进版本"
    assert store.integrity_report()["healthy"] is True
    store.close()


def test_evolution_degrades_when_llm_returns_garbage():
    """LLM 返回无法解析的内容时，同样必须降级而不是中断。

    与"上游挂了"是两种不同的坏法：上游活着但胡说八道，比彻底挂掉更难发现。
    """
    from govfin.evolution.pipeline import OntologyEvolutionPipeline
    from govfin.evolution.unk_pool import KIND_ENTITY
    from govfin.graph.store import GraphStore
    from govfin.ontology.seed import build_seed_ontology

    store = GraphStore(in_memory=True, ontology=build_seed_ontology())
    garbage = LLMClient(ScriptedProvider("我认为这个概念应该叫……嗯，让我想想。"), _config(max_retries=1))
    pipeline = OntologyEvolutionPipeline(store, ontology=store.ontology, client=garbage, use_llm=True)

    for i, text in enumerate(("环保核查异常", "环保核查异常", "环保核查异常")):
        pipeline.pool.observe(text, KIND_ENTITY, source_document=f"doc:g-{i}", context="环保核查异常")

    report = pipeline.run_cycle(commit=False)
    assert report.to_dict()["cycle"] >= 1
    assert store.integrity_report()["healthy"] is True
    store.close()


def test_domain_tools_never_leak_llm_exceptions():
    """工具层的本体演化入口在 LLM 全挂时也要返回结构化结果。

    工具层对模型的承诺是"返回 ok 字段而不是抛异常"。LLM 异常若穿透到
    这一层，MCP 会把 traceback 交给模型，模型唯一能做的就是放弃任务。
    """
    from govfin.evolution.pipeline import OntologyEvolutionPipeline
    from govfin.runtime import AgentRuntime
    from govfin.tools import DomainTools

    runtime = AgentRuntime(in_memory=True)
    broken = LLMClient(ScriptedProvider(TimeoutError("down")), _config(max_retries=1))
    runtime._pipeline = OntologyEvolutionPipeline(
        runtime.store, ontology=runtime.ontology, client=broken, use_llm=True
    )

    payload = DomainTools(runtime).ontology_evolve(commit=False)
    assert payload["ok"] is True
    assert payload["cycle"] >= 1
    runtime.close()
