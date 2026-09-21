"""图像解析：营业执照扫描件等。

OCR 后端可插拔，按能力降级：
  1. PaddleOCR      —— 中文证件类场景精度最好，离线
  2. Tesseract      —— 需要 chi_sim 语言包
  3. VLM            —— 走多模态大模型 API（Qwen-VL 等），无需本地依赖
  4. Sidecar        —— 读取同名 ``.ocr.json`` 边车文件

Sidecar 后端不是"作弊"：政务影像库在入库时经常由前置 OCR 服务生成伴生文本，
读边车是真实存在的生产形态，同时让整条管道在没有任何 OCR 依赖的环境下可被
端到端测试。后端不可用时**明确降级并报警告**，绝不返回空结果假装成功。
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
from pathlib import Path
from typing import Protocol

from govfin.errors import DataSourceUnavailable, ParseError
from govfin.graph.schema import MODALITY_IMAGE
from govfin.ingest.multimodal import ParsedDocument

# 营业执照字段：标签 → 规范字段名。OCR 常见形近字错识别在此做容错
_LICENSE_FIELDS: dict[str, str] = {
    "名称": "名称",
    "名 称": "名称",
    "统一社会信用代码": "统一社会信用代码",
    "统一社会信用代码/注册号": "统一社会信用代码",
    "注册号": "注册号",
    "类型": "企业类型",
    "类 型": "企业类型",
    "法定代表人": "法定代表人",
    "负责人": "法定代表人",
    "注册资本": "注册资本",
    "成立日期": "成立日期",
    "营业期限": "营业期限",
    "住所": "住所",
    "经营范围": "经营范围",
    "登记机关": "登记机关",
    "许可经营项目": "许可经营项目",
}

# OCR 易混字纠正表，仅用于字段值（如信用代码里的 O/0、I/1）
_OCR_CONFUSION = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "S": "5", "B": "8", "Z": "2"})

# 统一社会信用代码专用纠正表。GB 32100 的字符集是 0-9A-HJ-NPQRTUWXY，
# 天然不含 I、O、S、V、Z —— 这五个字母一旦出现，铁定是 OCR 看错了，可以放心纠正。
# **B 不在其列**：B 是信用代码里的合法字符，把 B 纠成 8 不是纠错，是制造错误。
# 91310115MA1K3XYB02 与 91310115MA1K3XY802 是两个不同的代码，而后者看起来
# 同样"合法"，一旦写入就会与工商登记的原值冲突、把企业身份归并到别的节点上，
# 且没有任何异常可捕获。因此这里只纠正**不可能合法出现**的字符。
_USCC_CONFUSION = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "S": "5", "Z": "2"})

_USCC = re.compile(r"[0-9A-HJ-NPQRTUWXY]{18}")
_DATE = re.compile(r"(\d{4})\s*[-年./]\s*(\d{1,2})\s*[-月./]\s*(\d{1,2})")
_AMOUNT = re.compile(r"([\d,]+(?:\.\d+)?)\s*(万元|万人民币|元|亿元)?")


class OcrBackend(Protocol):
    name: str

    def available(self) -> bool: ...

    def recognize(self, image_bytes: bytes) -> str: ...


class SidecarBackend:
    """读取同名边车 JSON。测试与离线演示的默认后端。"""

    name = "sidecar"

    def __init__(self, image_path: str | Path | None = None) -> None:
        self.image_path = Path(image_path) if image_path else None

    def available(self) -> bool:
        return bool(self.image_path and self._sidecar_path(self.image_path).exists())

    def recognize(self, image_bytes: bytes) -> str:
        if not self.image_path:
            raise DataSourceUnavailable("Sidecar 后端需要在 parse(path) 调用时提供文件路径")
        sidecar = self._sidecar_path(self.image_path)
        if not sidecar.exists():
            raise DataSourceUnavailable(f"未找到边车文件 {sidecar.name}")
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ParseError(f"边车文件 {sidecar.name} 读取失败: {exc}") from exc
        if isinstance(payload, dict) and "text" in payload:
            return str(payload["text"])
        # 也接受 {"fields": {...}} 形式：直接渲染成"标签: 值"文本
        if isinstance(payload, dict) and "fields" in payload:
            return "\n".join(f"{k}: {v}" for k, v in payload["fields"].items())
        raise ParseError(f"边车文件 {sidecar.name} 结构不符合预期（需含 text 或 fields）")

    @staticmethod
    def _sidecar_path(image_path: Path) -> Path:
        return image_path.with_suffix(image_path.suffix + ".ocr.json")


class TesseractBackend:
    name = "tesseract"

    def available(self) -> bool:
        try:
            import pytesseract  # noqa: F401

            pytesseract.get_tesseract_version()
            return True
        except Exception:  # noqa: BLE001 - 依赖或二进制缺失都算不可用
            return False

    def recognize(self, image_bytes: bytes) -> str:
        import pytesseract
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes))
        try:
            return pytesseract.image_to_string(img, lang="chi_sim+eng")
        except pytesseract.TesseractError:
            # 未装中文语言包时退化为英文，至少能读出数字与代码
            return pytesseract.image_to_string(img, lang="eng")


class PaddleOcrBackend:
    name = "paddleocr"

    def __init__(self) -> None:
        self._engine = None

    def available(self) -> bool:
        try:
            import paddleocr  # noqa: F401

            return True
        except Exception:  # noqa: BLE001
            return False

    def recognize(self, image_bytes: bytes) -> str:
        import numpy as np
        from PIL import Image
        from paddleocr import PaddleOCR

        if self._engine is None:
            self._engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        img = np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB"))
        result = self._engine.ocr(img, cls=True)
        lines: list[str] = []
        for page in result or []:
            for item in page or []:
                try:
                    lines.append(item[1][0])
                except (IndexError, TypeError):
                    continue
        return "\n".join(lines)


class VlmBackend:
    """走 OpenAI 兼容的多模态端点。需要 GOVFIN_VLM_API_KEY。"""

    name = "vlm"

    DEFAULT_PROMPT = (
        "请把这张营业执照图片中的所有可见文字逐行完整转录出来，保持原有的"
        "『标签: 值』格式，不要添加任何解释、不要翻译、不要总结。"
    )

    def __init__(self, *, model: str | None = None, base_url: str | None = None, api_key: str | None = None) -> None:
        self.model = model or os.environ.get("GOVFIN_VLM_MODEL", "qwen-vl-max")
        self.base_url = (base_url or os.environ.get("GOVFIN_VLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")).rstrip("/")
        self.api_key = api_key or os.environ.get("GOVFIN_VLM_API_KEY", "")

    def available(self) -> bool:
        return bool(self.api_key)

    def recognize(self, image_bytes: bytes) -> str:
        import httpx

        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.DEFAULT_PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                }
            ],
            "temperature": 0.0,
        }
        try:
            with httpx.Client(timeout=90.0) as client:
                resp = client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError as exc:
            raise DataSourceUnavailable(f"VLM OCR 网络错误: {exc}") from exc
        if resp.status_code >= 400:
            raise DataSourceUnavailable(f"VLM OCR 失败 {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise ParseError(f"VLM OCR 响应结构异常: {resp.text[:300]}") from exc


def available_backends() -> list[str]:
    backends: list[OcrBackend] = [PaddleOcrBackend(), TesseractBackend(), VlmBackend()]
    return [b.name for b in backends if b.available()]


class ImageParser:
    name = "image"

    def __init__(self, backends: list[OcrBackend] | None = None) -> None:
        self._backends = backends

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")

    def _resolve_backends(self, path: Path | None) -> list[OcrBackend]:
        if self._backends is not None:
            return self._backends
        ordered: list[OcrBackend] = [PaddleOcrBackend(), TesseractBackend(), VlmBackend()]
        if path is not None:
            ordered.append(SidecarBackend(path))
        return [b for b in ordered if b.available()]

    def parse(self, path: str | Path, *, source: str | None = None, **_) -> ParsedDocument:
        p = Path(path)
        try:
            data = p.read_bytes()
        except OSError as exc:
            raise ParseError(f"无法读取图像 '{p}': {exc}") from exc
        return self.parse_bytes(data, source=source or p.name, path=p)

    def parse_bytes(self, data: bytes, *, source: str, path: Path | None = None, **_) -> ParsedDocument:
        if not data:
            raise ParseError(f"图像 '{source}' 为空文件")

        doc = ParsedDocument(source=source, modality=MODALITY_IMAGE, metadata={"bytes": len(data)})
        doc.metadata["image_size"] = _image_size(data)

        backends = self._resolve_backends(path)
        if not backends:
            raise DataSourceUnavailable(
                f"没有可用的 OCR 后端处理 '{source}'。请任选其一：安装 paddleocr / tesseract(含 chi_sim)，"
                "或设置 GOVFIN_VLM_API_KEY 走多模态模型，或提供同名 .ocr.json 边车文件。",
                detail={"available": []},
            )

        errors: list[str] = []
        for backend in backends:
            try:
                text = backend.recognize(data)
            except (DataSourceUnavailable, ParseError) as exc:
                errors.append(f"{backend.name}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - 第三方 OCR 异常类型不可控
                errors.append(f"{backend.name}: {type(exc).__name__}: {exc}")
                continue
            if text and text.strip():
                doc.metadata["ocr_backend"] = backend.name
                doc.add("text", text, locator="ocr:full")
                fields = extract_license_fields(text)
                doc.metadata["license_fields"] = fields
                for name, value in fields.items():
                    doc.add("kv", f"{name}: {value}", locator=f"ocr:field:{name}")
                return doc
            errors.append(f"{backend.name}: 返回空文本")

        raise ParseError(
            f"所有 OCR 后端都未能从 '{source}' 中识别出文本",
            detail={"attempts": errors},
        )


def _image_size(data: bytes) -> list[int] | None:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            return list(img.size)
    except Exception:  # noqa: BLE001 - 尺寸只是附加信息，取不到不影响主流程
        return None


def extract_license_fields(text: str) -> dict:
    """从 OCR 文本中抽取营业执照结构化字段。

    对 OCR 噪声的鲁棒策略：标签允许中间有空格，值允许跨行续接，
    信用代码单独做形近字纠正后再校验。
    """
    fields: dict = {}
    if not text:
        return fields

    labels = sorted(_LICENSE_FIELDS, key=len, reverse=True)
    label_pattern = "|".join(re.escape(lbl) for lbl in labels)
    pattern = re.compile(rf"^\s*({label_pattern})\s*[:：]\s*(.*)$", re.MULTILINE)

    matches = list(pattern.finditer(text))
    for index, match in enumerate(matches):
        raw_label = match.group(1).replace(" ", "")
        canonical = _LICENSE_FIELDS.get(match.group(1)) or _LICENSE_FIELDS.get(raw_label)
        if canonical is None:
            continue
        # 值要从**捕获组**的起点切，不能用 ``match.end()``：``$`` 是零宽的，
        # ``match.end()`` 落在值的**末尾**，于是每个标签切出来都是空串，整个
        # "标签 → 值"循环等于没跑过——最后只剩下面两条兜底（信用代码、企业名）
        # 幸存，注册资本、成立日期、住所、经营范围全被静默丢掉。
        # 从捕获组起点切到下一个标签的起点，是为了让跨行续接的值仍然接得上。
        start = match.start(2)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = " ".join(text[start:end].split()).strip(" ;；,，")
        if not value:
            continue
        fields[canonical] = _normalize_field(canonical, value)

    if "统一社会信用代码" not in fields:
        candidate = _USCC.search(text.translate(_USCC_CONFUSION).replace(" ", ""))
        if candidate:
            fields["统一社会信用代码"] = candidate.group(0)

    if "名称" not in fields:
        m = re.search(r"([一-龥（）()]{2,40}(?:有限公司|股份有限公司|有限责任公司|个体工商户))", text)
        if m:
            fields["名称"] = m.group(1)

    return fields


def _normalize_field(name: str, value: str):
    if name == "统一社会信用代码":
        cleaned = value.translate(_USCC_CONFUSION).replace(" ", "").upper()
        m = _USCC.search(cleaned)
        return m.group(0) if m else cleaned
    if name in ("注册资本", "注册资本(万元)"):
        m = _AMOUNT.search(value)
        if m:
            amount = float(m.group(1).replace(",", ""))
            unit = m.group(2) or "万元"
            if unit.startswith("亿"):
                amount *= 10000
            elif unit == "元":
                amount /= 10000
            return round(amount, 4)
        return value
    if name in ("成立日期", "营业期限"):
        m = _DATE.search(value)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return value
    return value
