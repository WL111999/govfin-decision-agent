"""统一异常体系。所有对外失败都归一到 GovFinError 子类，便于 A2A/MCP 层做错误映射。"""

from __future__ import annotations


class GovFinError(Exception):
    """所有领域异常的基类。"""

    code = "GOVFIN_ERROR"
    retryable = False

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "detail": self.detail,
        }


class ValidationError(GovFinError):
    code = "VALIDATION_ERROR"


class ParseError(GovFinError):
    """多模态解析失败（损坏 PDF、非法 JSON、无法解码图像等）。"""

    code = "PARSE_ERROR"


class GraphError(GovFinError):
    code = "GRAPH_ERROR"


class NodeNotFound(GraphError):
    code = "NODE_NOT_FOUND"


class PathConstraintError(GraphError):
    """路径查询约束自相矛盾或超出安全边界。"""

    code = "PATH_CONSTRAINT_ERROR"


class PathExplosionError(PathConstraintError):
    """路径数超过预算上限，查询被主动熔断。"""

    code = "PATH_EXPLOSION"


class OntologyError(GovFinError):
    code = "ONTOLOGY_ERROR"


class ConstraintViolation(OntologyError):
    """候选概念违反上层本体的 SHACL/OWL 公理约束。"""

    code = "CONSTRAINT_VIOLATION"

    def __init__(self, message: str, *, violations: list | None = None) -> None:
        super().__init__(message, detail={"violations": violations or []})
        self.violations = violations or []


class VersionConflict(OntologyError):
    """本体版本乐观锁冲突，说明有并发编辑。"""

    code = "VERSION_CONFLICT"
    retryable = True


class LLMError(GovFinError):
    code = "LLM_ERROR"
    retryable = True


class LLMTimeout(LLMError):
    code = "LLM_TIMEOUT"


class LLMRateLimited(LLMError):
    code = "LLM_RATE_LIMITED"


class LLMResponseInvalid(LLMError):
    """LLM 返回内容无法解析为要求的结构，重试通常无用。"""

    code = "LLM_RESPONSE_INVALID"
    retryable = False


class CircuitOpen(LLMError):
    """熔断器打开，上游持续故障时快速失败而不是排队拖垮调用方。"""

    code = "CIRCUIT_OPEN"


class ToolError(GovFinError):
    code = "TOOL_ERROR"


class DataSourceUnavailable(ToolError):
    """外部数据源不可用，Skill 层据此走降级策略。"""

    code = "DATA_SOURCE_UNAVAILABLE"
    retryable = True
