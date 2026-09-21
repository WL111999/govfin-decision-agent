"""集中配置。所有可调参数走环境变量，便于在 Nexent 容器里换配置不改代码。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path | None = None, *, override: bool = False) -> dict[str, str]:
    """极简 .env 加载器，避免为了 12 行逻辑引入 python-dotenv 依赖。

    只支持 ``KEY=VALUE`` 与 ``# 注释``，值两侧的引号会被剥掉。
    已存在的环境变量优先（``override=False``），这样容器里注入的配置
    不会被仓库里的开发用 .env 覆盖。
    """
    env_path = path or PROJECT_ROOT / ".env"
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded
    try:
        content = env_path.read_text(encoding="utf-8")
    except OSError:
        return loaded

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if not key:
            continue
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


load_dotenv()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class LLMConfig:
    provider: str = field(default_factory=lambda: _env("GOVFIN_LLM_PROVIDER", "deepseek"))
    api_key: str = field(default_factory=lambda: _env("GOVFIN_LLM_API_KEY", ""))
    base_url: str = field(default_factory=lambda: _env("GOVFIN_LLM_BASE_URL", "https://api.deepseek.com/v1"))
    model: str = field(default_factory=lambda: _env("GOVFIN_LLM_MODEL", "deepseek-chat"))
    temperature: float = field(default_factory=lambda: _env_float("GOVFIN_LLM_TEMPERATURE", 0.0))
    timeout_seconds: float = field(default_factory=lambda: _env_float("GOVFIN_LLM_TIMEOUT", 60.0))
    max_retries: int = field(default_factory=lambda: _env_int("GOVFIN_LLM_MAX_RETRIES", 3))
    backoff_base: float = field(default_factory=lambda: _env_float("GOVFIN_LLM_BACKOFF_BASE", 0.5))
    # 熔断器：连续失败 N 次后打开，冷却 M 秒后半开探测
    circuit_failure_threshold: int = field(default_factory=lambda: _env_int("GOVFIN_LLM_CIRCUIT_THRESHOLD", 6))
    circuit_cooldown: float = field(default_factory=lambda: _env_float("GOVFIN_LLM_CIRCUIT_COOLDOWN", 30.0))
    cache_size: int = field(default_factory=lambda: _env_int("GOVFIN_LLM_CACHE_SIZE", 2048))

    @property
    def usable(self) -> bool:
        return self.provider != "offline" and bool(self.api_key)


@dataclass
class GraphConfig:
    db_path: str = field(
        default_factory=lambda: _env("GOVFIN_GRAPH_DB", str(PROJECT_ROOT / "data" / "govfin_graph.db"))
    )
    # 路径搜索安全边界——防止在稠密图上被查询拖死
    max_path_depth: int = field(default_factory=lambda: _env_int("GOVFIN_MAX_PATH_DEPTH", 6))
    max_paths: int = field(default_factory=lambda: _env_int("GOVFIN_MAX_PATHS", 2000))
    max_expansions: int = field(default_factory=lambda: _env_int("GOVFIN_MAX_EXPANSIONS", 200_000))
    query_timeout_seconds: float = field(default_factory=lambda: _env_float("GOVFIN_QUERY_TIMEOUT", 10.0))
    busy_timeout_ms: int = field(default_factory=lambda: _env_int("GOVFIN_SQLITE_BUSY_TIMEOUT", 10_000))


@dataclass
class ConfidenceConfig:
    """置信度修正衰减模型参数。"""

    # 每条边按其证据类别承担的惩罚系数：直接证据不罚，LLM 生成边罚得最重
    lambda_direct: float = field(default_factory=lambda: _env_float("GOVFIN_LAMBDA_DIRECT", 1.0))
    lambda_derived: float = field(default_factory=lambda: _env_float("GOVFIN_LAMBDA_DERIVED", 1.6))
    lambda_llm: float = field(default_factory=lambda: _env_float("GOVFIN_LAMBDA_LLM", 2.6))
    # 每跳衰减底数：非证据性损耗，模拟语义漂移
    hop_decay: float = field(default_factory=lambda: _env_float("GOVFIN_HOP_DECAY", 0.95))
    # 瓶颈保护：路径最终置信度不得低于 (gamma + (1-gamma)*min_edge_weight)
    bottleneck_floor: float = field(default_factory=lambda: _env_float("GOVFIN_BOTTLENECK", 0.25))
    # 决策阈值：低于该值的路径不进入结论，但保留在 Layer3 作为"被拒绝的推理"
    decision_threshold: float = field(default_factory=lambda: _env_float("GOVFIN_DECISION_THRESHOLD", 0.55))
    # LLM 生成边的先验 Beta(alpha, beta)，随历史验证反馈更新
    llm_prior_alpha: float = field(default_factory=lambda: _env_float("GOVFIN_LLM_PRIOR_ALPHA", 2.0))
    llm_prior_beta: float = field(default_factory=lambda: _env_float("GOVFIN_LLM_PRIOR_BETA", 2.0))


@dataclass
class OntologyConfig:
    # 聚类：把同一 UNK 提及的嵌入归并为一个候选概念的最小相似度
    cluster_similarity: float = field(default_factory=lambda: _env_float("GOVFIN_CLUSTER_SIM", 0.82))
    # 候选被提升为提案所需的最小出现频次
    min_support: int = field(default_factory=lambda: _env_int("GOVFIN_MIN_SUPPORT", 3))
    # 提案自动通过的最小置信度；低于则可自动拒绝
    auto_accept_confidence: float = field(default_factory=lambda: _env_float("GOVFIN_AUTO_ACCEPT", 0.90))
    auto_reject_confidence: float = field(default_factory=lambda: _env_float("GOVFIN_AUTO_REJECT", 0.35))
    embedding_dim: int = field(default_factory=lambda: _env_int("GOVFIN_EMBED_DIM", 256))


@dataclass
class ServerConfig:
    host: str = field(default_factory=lambda: _env("GOVFIN_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("GOVFIN_PORT", 8077))
    a2a_port: int = field(default_factory=lambda: _env_int("GOVFIN_A2A_PORT", 8078))
    mcp_sse_port: int = field(default_factory=lambda: _env_int("GOVFIN_MCP_SSE_PORT", 8079))
    public_base_url: str = field(default_factory=lambda: _env("GOVFIN_PUBLIC_BASE_URL", "http://localhost:8077"))


@dataclass
class Settings:
    llm: LLMConfig = field(default_factory=LLMConfig)
    graph: GraphConfig = field(default_factory=GraphConfig)
    confidence: ConfidenceConfig = field(default_factory=ConfidenceConfig)
    ontology: OntologyConfig = field(default_factory=OntologyConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    data_dir: Path = field(default_factory=lambda: Path(_env("GOVFIN_DATA_DIR", str(PROJECT_ROOT / "data"))))

    def describe(self) -> dict:
        """给 /health 和审计日志用的安全快照——绝不泄露 api_key。"""
        out: dict = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if hasattr(value, "__dataclass_fields__"):
                sub = {}
                for sf in fields(value):
                    sv = getattr(value, sf.name)
                    if "key" in sf.name or "token" in sf.name or "secret" in sf.name:
                        sv = "***set***" if sv else ""
                    sub[sf.name] = sv
                out[f.name] = sub
            else:
                out[f.name] = str(value)
        return out


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings
