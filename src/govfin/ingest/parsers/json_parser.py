"""社保 JSON 解析。

政务数据交换常见的两种形态：
- 单条记录对象 ``{...}``
- 批量记录 ``{"records": [...]}`` 或直接数组 ``[...]``

难点在键名不统一——不同统筹区的接口用"缴费基数"/"缴纳基数"/"缴存基数"描述同一
概念。这里做键名归一而不是直接透传，否则同一实体在图上会裂成多个节点。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from govfin.errors import ParseError
from govfin.graph.schema import MODALITY_JSON
from govfin.ingest.multimodal import ParsedDocument

# 政务接口键名归一表：同义键 → 规范键
_KEY_ALIASES: dict[str, str] = {
    "缴费基数": "缴纳基数",
    "缴存基数": "缴纳基数",
    "社保基数": "缴纳基数",
    "费款所属期": "缴纳月份",
    "缴费月份": "缴纳月份",
    "所属月份": "缴纳月份",
    "参保人数": "实缴人数",
    "缴费人数": "实缴人数",
    "缴费状态": "缴纳状态",
    "征缴状态": "缴纳状态",
    "单位名称": "名称",
    "企业名称": "名称",
    "单位全称": "名称",
    "信用代码": "统一社会信用代码",
    "社会信用代码": "统一社会信用代码",
    "uscc": "统一社会信用代码",
    "id": "记录编号",
    "record_id": "记录编号",
    "sbid": "记录编号",
}

_RECORD_CONTAINER_KEYS = ("records", "data", "list", "items", "rows", "result")


class JsonParser:
    name = "json"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in (".json", ".jsonl")

    def parse(self, path: str | Path, *, source: str | None = None, **_) -> ParsedDocument:
        p = Path(path)
        try:
            data = p.read_bytes()
        except OSError as exc:
            raise ParseError(f"无法读取 '{p}': {exc}") from exc
        return self.parse_bytes(data, source=source or p.name, jsonl=p.suffix.lower() == ".jsonl")

    def parse_bytes(self, data: bytes, *, source: str, jsonl: bool = False, **_) -> ParsedDocument:
        if not data.strip():
            raise ParseError(f"'{source}' 为空文件")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = data.decode("gbk")
            except UnicodeDecodeError as exc:
                raise ParseError(f"'{source}' 编码无法识别（非 UTF-8 亦非 GBK）") from exc

        doc = ParsedDocument(source=source, modality=MODALITY_JSON, metadata={"jsonl": jsonl})

        try:
            records = _read_jsonl(text) if jsonl else _extract_records(json.loads(text))
        except json.JSONDecodeError as exc:
            raise ParseError(f"'{source}' JSON 语法错误: {exc}") from exc
        except RecursionError as exc:
            # 深层嵌套的 JSON（例如几万个连续的 `[`）会让 json.loads 递归爆栈。
            # RecursionError 是 RuntimeError 的子类而非 ValueError，
            # 因此上面那条 except 拦不住它——它会一路穿透到 MCP 层变成 500。
            # 这是**畸形输入**，不是解析器故障，要按受控的语法错误上报。
            raise ParseError(
                f"'{source}' JSON 嵌套层级过深，超出安全解析深度（可能是畸形或恶意构造）"
            ) from exc

        if not records:
            doc.warnings.append("JSON 中没有解析出任何记录对象")

        for index, record in enumerate(records):
            if not isinstance(record, dict):
                doc.warnings.append(f"第 {index} 条记录不是对象，已跳过")
                continue
            normalized = normalize_keys(record)
            # 每个字段单独成块：抽取器靠"标签: 值"定位字段，整条记录挤在一个块里
            # 会让 partition(':') 把后续所有字段吞进第一个字段的值。
            # 同时 locator 精确到字段名，审计时能直指是哪一行支撑了判定。
            doc.add(
                "record",
                _render_kv(normalized),
                locator=f"record:{index}",
                record_index=index,
                record=normalized,
                record_header=True,
            )
            for field_name, value in normalized.items():
                if field_name == "__raw__":
                    continue
                doc.add(
                    "kv",
                    f"{field_name}: {_flatten(value)}",
                    locator=f"record:{index}/{field_name}",
                    record_index=index,
                    field=field_name,
                )

        doc.metadata["record_count"] = len(records)
        return doc


def _read_jsonl(text: str) -> list[Any]:
    out: list[Any] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            out.append(json.loads(stripped))
        except json.JSONDecodeError as exc:
            raise ParseError(f"JSONL 第 {line_no} 行语法错误: {exc}") from exc
        except RecursionError as exc:
            # 与 parse_bytes 同源：单行也可以是几万个 `[`。这里若不拦，
            # 整条 JSONL 会以 RecursionError 崩掉，而不是指出是第几行的问题。
            raise ParseError(f"JSONL 第 {line_no} 行嵌套层级过深，超出安全解析深度") from exc
    return out


def _extract_records(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in _RECORD_CONTAINER_KEYS:
            if key in payload and isinstance(payload[key], list):
                return payload[key]
        return [payload]
    return []


def normalize_keys(record: dict, *, _depth: int = 0) -> dict:
    """递归归一键名。嵌套太深的部分保留原样——政务 JSON 的深层结构往往是自由字段。"""
    if _depth > 4:
        return dict(record)
    out: dict[str, Any] = {}
    for key, value in record.items():
        canonical = _KEY_ALIASES.get(key, key)
        if canonical in out and out[canonical] not in (None, ""):
            # 同义键同时出现且都非空——保留先到的，另一份放进 __raw__ 以免静默丢数据
            out.setdefault("__raw__", {})[key] = value
            continue
        if isinstance(value, dict):
            out[canonical] = normalize_keys(value, _depth=_depth + 1)
        elif isinstance(value, list):
            out[canonical] = [
                normalize_keys(v, _depth=_depth + 1) if isinstance(v, dict) else v for v in value
            ]
        else:
            out[canonical] = value
    return out


def _render_kv(record: dict) -> str:
    return "\n".join(f"{k}: {_flatten(v)}" for k, v in record.items() if k != "__raw__")


def _flatten(value: Any, *, _depth: int = 0) -> str:
    """把任意嵌套值压成一行文本。

    必须有深度上限。``normalize_keys`` 在第 4 层就停止递归了，但那只保护了
    键名归一——它保留的深层原值仍会原样交到这里。一个勉强能被 ``json.loads``
    解析的深层结构（解析深度受解释器递归上限约束，而渲染每层要花好几个栈帧）
    足以在渲染阶段爆栈，且此时抛出的 RecursionError 没有任何受控异常包着。
    超深部分用省略号替代：完整展开这种结构对阅读和推理都没有价值。
    """
    if _depth > 8:
        return "..."
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}={_flatten(v, _depth=_depth + 1)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_flatten(v, _depth=_depth + 1) for v in value) + "]"
    return str(value)


def records_of(doc: ParsedDocument) -> list[dict]:
    """从 ParsedDocument 取回结构化记录。抽取器需要原始 dict 而非渲染文本。"""
    return [b.metadata["record"] for b in doc.blocks if b.kind == "kv" and "record" in b.metadata]
