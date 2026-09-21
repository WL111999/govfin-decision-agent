"""PDF 解析：财报（含嵌套表格）与司法判决书。

财报 PDF 的难点是嵌套表——主表里嵌子表（如"其中：短期借款"）。pdfplumber 给出的
是几何表格，层级信息已经丢失，需要用缩进/字体/表头关键字重建。这里的做法是：
先按几何抽表，再用"表头行 + 缩进标记"把子表识别出来并保留父子关系。
"""

from __future__ import annotations

import io
from pathlib import Path

from govfin.errors import ParseError
from govfin.graph.schema import MODALITY_PDF
from govfin.ingest.multimodal import ParsedDocument

# 中文财报里标识子表的行首标记
_NESTED_MARKERS = ("其中：", "其中:", "减：", "加：", "（其中）")

# 判决书的结构化段落标题
_JUDGMENT_SECTIONS = (
    "案由", "当事人", "原告", "被告", "第三人", "诉讼请求",
    "本院查明", "本院认为", "裁判结果", "判决如下", "执行",
)


class PdfParser:
    name = "pdf"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == ".pdf"

    def parse(self, path: str | Path, *, source: str | None = None, **_) -> ParsedDocument:
        p = Path(path)
        try:
            data = p.read_bytes()
        except OSError as exc:
            raise ParseError(f"无法读取 PDF '{p}': {exc}") from exc
        return self.parse_bytes(data, source=source or p.name)

    def parse_bytes(self, data: bytes, *, source: str, **_) -> ParsedDocument:
        if not data:
            raise ParseError(f"PDF '{source}' 为空文件")
        if not data[:5].startswith(b"%PDF-"):
            raise ParseError(f"'{source}' 不是合法 PDF（缺少 %PDF- 魔数）")

        doc = ParsedDocument(source=source, modality=MODALITY_PDF, metadata={"bytes": len(data)})

        try:
            import pdfplumber
        except ImportError as exc:  # pragma: no cover - 依赖缺失属于部署问题
            raise ParseError("缺少 pdfplumber，无法解析 PDF") from exc

        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                doc.metadata["pages"] = len(pdf.pages)
                doc.metadata["pdf_metadata"] = {k: str(v) for k, v in (pdf.metadata or {}).items()}
                for page_no, page in enumerate(pdf.pages, start=1):
                    self._parse_page(page, page_no, doc)
        except ParseError:
            raise
        except Exception as exc:  # noqa: BLE001 - pdfplumber 对损坏文件抛的异常类型很杂
            raise ParseError(f"PDF '{source}' 解析失败: {exc}") from exc

        if not doc.blocks:
            doc.warnings.append("未从 PDF 中提取到任何文本或表格（可能是纯扫描件，需走 OCR）")
        return doc

    def _parse_page(self, page, page_no: int, doc: ParsedDocument) -> None:
        text = page.extract_text() or ""
        if text.strip():
            doc.add("text", text, locator=f"page:{page_no}", page=page_no)

        try:
            tables = page.extract_tables() or []
        except Exception as exc:  # noqa: BLE001
            doc.warnings.append(f"第 {page_no} 页表格抽取失败: {exc}")
            return

        for t_index, table in enumerate(tables):
            rendered, nested = _render_table(table)
            if not rendered.strip():
                continue
            doc.add(
                "table",
                rendered,
                locator=f"page:{page_no}/table:{t_index}",
                page=page_no,
                nested_table_count=nested,
                rows=len(table),
            )


def _render_table(table: list[list[str | None]]) -> tuple[str, int]:
    """把几何表格渲染成管道分隔文本，并统计识别出的嵌套子表数。"""
    lines: list[str] = []
    nested = 0
    for row in table:
        cells = ["" if c is None else str(c).replace("\n", " ").strip() for c in row]
        if not any(cells):
            continue
        first = cells[0]
        if any(first.startswith(marker) for marker in _NESTED_MARKERS):
            nested += 1
            # 用缩进标记保留父子关系，抽取器据此建 parent_of 关系
            cells[0] = "    " + first
        lines.append("|".join(cells))
    return "\n".join(lines), nested


def detect_document_kind(text: str) -> str:
    """粗判 PDF 属于哪类业务文档。抽取器据此选择抽提策略。"""
    hits = sum(1 for s in _JUDGMENT_SECTIONS if s in text)
    if hits >= 3:
        return "judgment"
    financial_markers = ("资产负债表", "利润表", "现金流量表", "营业收入", "净利润", "资产负债率")
    if sum(1 for m in financial_markers if m in text) >= 3:
        return "financial_report"
    if "营业执照" in text or "统一社会信用代码" in text:
        return "business_license"
    return "generic"
