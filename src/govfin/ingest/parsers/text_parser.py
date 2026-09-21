"""半结构化文本解析：征信报告、监管文件、行政处罚决定书。

征信报告不是纯自由文本——它有稳定的分节结构和"标签: 值"行。把它当纯文本喂给
LLM 会浪费 token 且不稳定；这里先做结构切分，让抽取器拿到带定位信息的段落。
"""

from __future__ import annotations

import re
from pathlib import Path

from govfin.errors import ParseError
from govfin.graph.schema import MODALITY_TEXT
from govfin.ingest.multimodal import ParsedDocument

_SECTION_PATTERNS = (
    "个人基本信息", "信息概要", "信贷交易信息提示", "信贷交易明细",
    "非信贷交易信息明细", "公共信息明细", "查询记录", "报告说明",
    "一、", "二、", "三、", "四、", "五、", "六、", "七、", "八、", "九、",
)

_KV = re.compile(r"^\s*([^:：\s][^:：]{0,30})\s*[:：]\s*(.+?)\s*$")
_BULLET = re.compile(r"^\s*(?:[-•·*]|\d+[.、)）]|[（(]\d+[）)])\s*")


class TextParser:
    name = "text"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in (".txt", ".md", ".text", "")

    def parse(self, path: str | Path, *, source: str | None = None, encoding: str | None = None, **_) -> ParsedDocument:
        p = Path(path)
        try:
            data = p.read_bytes()
        except OSError as exc:
            raise ParseError(f"无法读取 '{p}': {exc}") from exc
        return self.parse_bytes(data, source=source or p.name, encoding=encoding)

    def parse_bytes(self, data: bytes, *, source: str, encoding: str | None = None, **_) -> ParsedDocument:
        if not data.strip():
            raise ParseError(f"'{source}' 为空文件")
        text = _decode(data, encoding)

        doc = ParsedDocument(source=source, modality=MODALITY_TEXT)
        sections = _split_sections(text)
        doc.metadata["section_count"] = len(sections)

        for index, (title, body) in enumerate(sections):
            locator = f"section:{index}" + (f"/{title}" if title else "")
            if title:
                doc.add("text", f"【{title}】", locator=f"{locator}/title")
            for line_no, line in enumerate(body.splitlines()):
                stripped = line.strip()
                if not stripped:
                    continue
                kv = _KV.match(stripped)
                if kv and len(kv.group(1)) <= 20:
                    doc.add(
                        "kv",
                        f"{kv.group(1).strip()}: {kv.group(2).strip()}",
                        locator=f"{locator}/line:{line_no}",
                        label=kv.group(1).strip(),
                        value=kv.group(2).strip(),
                        section=title,
                    )
                else:
                    doc.add(
                        "text",
                        _BULLET.sub("", stripped),
                        locator=f"{locator}/line:{line_no}",
                        section=title,
                    )
        return doc


def _decode(data: bytes, encoding: str | None) -> str:
    if encoding:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError as exc:
            raise ParseError(f"按 {encoding} 解码失败: {exc}") from exc
    for candidate in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(candidate)
        except UnicodeDecodeError:
            continue
    # 政务老系统导出的文件常带混合编码，用替换字符兜底而不是直接失败
    return data.decode("utf-8", errors="replace")


def _split_sections(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_title = ""
    current: list[str] = []

    for line in lines:
        stripped = line.strip()
        is_section = any(
            stripped.startswith(p) and (len(stripped) <= len(p) + 12)
            for p in _SECTION_PATTERNS
        )
        if is_section:
            if current or current_title:
                sections.append((current_title, current))
            current_title = stripped.rstrip("：:")
            current = []
        else:
            current.append(line)
    if current or current_title:
        sections.append((current_title, current))

    return [(title, "\n".join(body)) for title, body in sections]
