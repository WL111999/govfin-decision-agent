"""解析器注册表。按扩展名 + 内容嗅探分派，调用方不需要知道模态。"""

from __future__ import annotations

from pathlib import Path

from govfin.errors import ParseError
from govfin.ingest.multimodal import ParsedDocument
from govfin.ingest.parsers.image_parser import ImageParser
from govfin.ingest.parsers.json_parser import JsonParser
from govfin.ingest.parsers.pdf_parser import PdfParser
from govfin.ingest.parsers.table_parser import TableParser
from govfin.ingest.parsers.text_parser import TextParser

_PARSERS = (PdfParser(), TableParser(), JsonParser(), ImageParser(), TextParser())

# 顺序敏感：ImageParser 要排在 TextParser 前面，否则图片扩展名不会被拦截
_SUFFIX_ORDER = {
    ".pdf": 0,
    ".csv": 1,
    ".tsv": 1,
    ".xlsx": 1,
    ".xls": 1,
    ".json": 2,
    ".jsonl": 2,
    ".png": 3,
    ".jpg": 3,
    ".jpeg": 3,
    ".bmp": 3,
    ".tif": 3,
    ".tiff": 3,
    ".webp": 3,
    ".txt": 4,
    ".md": 4,
    ".text": 4,
}


def get_parsers() -> tuple:
    return _PARSERS


def parser_for(path: str | Path) -> object:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix not in _SUFFIX_ORDER:
        raise ParseError(
            f"不支持的文件类型 '{suffix}'",
            detail={"supported": sorted(_SUFFIX_ORDER)},
        )
    target = _PARSERS[_SUFFIX_ORDER[suffix]]
    if not target.supports(p):
        raise ParseError(
            f"'{p.name}' 由 {type(target).__name__} 处理时被拒绝（内容与扩展名不符）"
        )
    return target


def parse(path: str | Path, **kwargs) -> ParsedDocument:
    return parser_for(path).parse(path, **kwargs)


def parse_bytes(data: bytes, *, source: str, suffix: str, **kwargs) -> ParsedDocument:
    """从内存字节解析。MCP 工具与 HTTP 上传走这条路径，避免落盘。"""
    suffix = suffix.lower()
    if suffix not in _SUFFIX_ORDER:
        raise ParseError(f"不支持的文件类型 '{suffix}'")
    return _PARSERS[_SUFFIX_ORDER[suffix]].parse_bytes(data, source=source, **kwargs)


__all__ = [
    "ImageParser",
    "JsonParser",
    "PdfParser",
    "TableParser",
    "TextParser",
    "get_parsers",
    "parse",
    "parse_bytes",
    "parser_for",
]
