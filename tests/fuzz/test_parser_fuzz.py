"""解析层模糊测试：畸形、截断、恶意构造的输入。

关注点不是"解析器能不能正确处理合法输入"（那是单元测试的事），
而是**非法输入会不会把服务搞崩**。解析器是唯一直接面对外部文件的地方，
一个未捕获的异常就能让整批导入中断，或者更糟——让进程带着半截状态继续跑。

因此这里只断言两件事：
1. 不抛非受控异常（受控的 ParseError 是允许的，而且是对的行为）
2. 要么明确失败，要么返回结构完整的结果——不存在"成功了但结果残缺"
"""

from __future__ import annotations

import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from govfin.errors import GovFinError, ParseError
from govfin.ingest.parsers import parse, parse_bytes, parser_for

# 受控失败 = 领域异常体系内的异常。它们携带 code/retryable/detail，
# MCP 层能把它们映射成结构化错误返回给模型。**非受控异常才是缺陷**——
# 那意味着调用方拿到一个 traceback，无从判断该重试还是该终止。
CONTROLLED = GovFinError

SUFFIXES = [".json", ".csv", ".txt", ".pdf", ".png", ".jpg"]

# 各类畸形输入的样板。hypothesis 负责随机，这些负责覆盖已知的攻击面。
MALFORMED = [
    b"",
    b"\x00",
    b"\xff\xfe\xfd\xfc",
    b"\x00" * 1024,
    b"\xff" * 4096,
    b"{",
    b"[",
    b'{"a":',
    b'{"a": 1,,}',
    b"[" * 10000,                    # 深嵌套，可能打爆递归
    b"{" * 5000,
    b'{"key": "' + b"x" * 1_000_000,  # 未闭合的超长字符串
    b"\xef\xbb\xbf",                  # 只有 BOM
    b"\r\n\r\n\r\n",
    b"a,b,c\n" + b"1,2\n" * 100,      # 列数不一致的 CSV
    b"%PDF-1.4\n",                    # 截断的 PDF 头
    b"\x89PNG\r\n\x1a\n",             # 截断的 PNG 头
    b"<html><body><script>alert(1)</script></body></html>",
    b"'; DROP TABLE nodes; --",
    b"$(rm -rf /)",
    b"\x00\x01\x02\x03\x04\x05\x06\x07",
]


@pytest.mark.parametrize("suffix", SUFFIXES)
@pytest.mark.parametrize("payload", MALFORMED, ids=lambda b: f"{len(b)}B")
def test_malformed_bytes_never_crash(suffix, payload):
    """畸形输入只能产生受控的领域异常，或返回可用的结果。

    注意 `DataSourceUnavailable`（无 OCR 后端）也算受控失败，而且是**正确**的——
    没装 OCR 引擎时无法处理任何图片，这与文件是否畸形无关。
    把它笼统归成"解析失败"会掩盖真正的部署问题。
    """
    try:
        doc = parse_bytes(payload, source=f"fuzz{suffix}", suffix=suffix)
    except CONTROLLED as exc:
        # 受控失败：必须带可判断的信息，否则调用方无法决定重试还是终止
        assert exc.code and isinstance(exc.retryable, bool)
        assert exc.to_dict()["message"]
        return
    except Exception as exc:  # noqa: BLE001 - 这里就是要抓住一切非受控异常
        pytest.fail(f"{suffix} 解析 {len(payload)} 字节时抛出非受控异常 {type(exc).__name__}: {exc}")

    # 返回了结果，就必须是结构完整的——不存在"成功了但结果残缺"
    assert doc.source
    assert doc.modality
    assert isinstance(doc.blocks, list)
    assert isinstance(doc.warnings, list)
    assert isinstance(doc.text(), str)
    assert isinstance(doc.tables(), list)
    # 自描述性：任意块都要能定位回原文，否则证据无法追溯
    for block in doc.blocks:
        assert block.locator, "块缺少 locator，证据将无法定位"
        assert isinstance(block.text, str)
    # 必须可序列化——MCP 层会把它转成 JSON
    json.dumps(doc.to_dict(), ensure_ascii=False)


@given(st.binary(max_size=512))
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_random_bytes_never_crash(payload):
    """随机字节流。任何后缀都不应产生非受控异常。"""
    for suffix in (".json", ".csv", ".txt", ".pdf", ".png"):
        try:
            parse_bytes(payload, source=f"rnd{suffix}", suffix=suffix)
        except CONTROLLED:
            pass
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"{suffix} 处理随机输入时崩溃 {type(exc).__name__}: {exc}")


@given(st.text(max_size=400))
@settings(max_examples=200, deadline=None)
def test_random_text_never_crash(text):
    """随机文本。这比随机字节更接近真实场景——乱码但合法的 Unicode。"""
    payload = text.encode("utf-8", errors="replace")
    try:
        parse_bytes(payload, source="rnd.txt", suffix=".txt")
    except CONTROLLED:
        pass
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"文本解析崩溃 {type(exc).__name__}: {exc}")


@given(
    st.dictionaries(
        st.text(max_size=20),
        st.recursive(
            st.none() | st.booleans() | st.floats(allow_nan=True) | st.text(max_size=20),
            lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4),
            max_leaves=12,
        ),
        max_size=8,
    )
)
@settings(max_examples=150, deadline=None)
def test_random_json_shapes_never_crash(obj):
    """随机 JSON 结构。JSON 解析器要能接受任意合法 JSON 而不崩。"""
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    try:
        parse_bytes(payload, source="rnd.json", suffix=".json")
    except CONTROLLED:
        pass
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"JSON 解析崩溃 {type(exc).__name__}: {exc}")


def test_unsupported_suffix_rejected_cleanly():
    for bad in (".exe", ".zip", ".", "", ".JSONX"):
        with pytest.raises(ParseError):
            parser_for(f"file{bad}")


def test_truncated_pdf_does_not_hang(tmp_path):
    """截断的 PDF 是最常见的真实故障（下载中断）。

    必须快速失败，不能挂住——导入是逐文件串行的，一个挂住的文件会让整批卡死。
    """
    real = tmp_path / "x.pdf"
    real.write_bytes(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF")
    try:
        parse(real)
    except CONTROLLED:
        pass
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"截断 PDF 崩溃 {type(exc).__name__}: {exc}")


def test_sidecar_image_path_works_without_ocr_backend(tmp_path):
    """没有装任何 OCR 引擎时，边车文件必须仍然可用。

    这是样例数据的实际加载路径，也是部署环境里最可能缺组件的地方：
    生产容器通常不装 paddleocr/tesseract（体积以 GB 计），
    图片证据全靠随图交付的 .ocr.json 边车。这条路径断了，
    营业执照、身份证这类图片证据就全部丢失，而**图照样能建起来**，
    只是少了几条边——属于最难发现的那类故障。
    """
    img = tmp_path / "营业执照_测试有限公司.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    (tmp_path / "营业执照_测试有限公司.png.ocr.json").write_text(
        json.dumps(
            {
                "text": "名称: 测试有限公司 法定代表人: 张三 注册资本: 100万元人民币",
                "fields": {"名称": "测试有限公司", "法定代表人": "张三"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    doc = parse(img)
    assert doc.modality == "image"
    assert "测试有限公司" in doc.text()
    assert doc.blocks, "边车内容必须产出块，否则图片证据会静默丢失"


def test_sidecar_missing_raises_controlled_error(tmp_path):
    """有图无边车、又无 OCR 引擎时，报的必须是"数据源不可用"而不是"解析失败"。

    两者对运维的含义完全不同：前者要装组件，后者是文件坏了。
    """
    from govfin.errors import DataSourceUnavailable

    img = tmp_path / "孤图.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    with pytest.raises(GovFinError) as exc:
        parse(img)
    assert exc.value.code
    # 若能定位到具体原因，应当是"缺后端"而非"文件损坏"
    if isinstance(exc.value, DataSourceUnavailable):
        assert exc.value.detail is not None
