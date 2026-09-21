"""导入完整性：进来的每一份材料都必须留下痕迹，包括失败的那一份。

这一层测的不是"解析器对不对"（那是单元测试），也不是"畸形输入会不会崩"
（那是模糊测试），而是**部署层面的静默丢失**：

> 按 `pyproject.toml` 装出来的环境，能不能真的读懂样例数据里的每一份文档？

这个问题之所以值得单独测，是因为它有三个"不会触发任何警报"的特征：

1. 开发机上永远不会复现——本机常因别的原因装过某个包，代码路径就通了；
2. 失败不抛到调用方——``ingest_dir`` 逐份 try/except，一份解析不了就跳过，
   整批导入照常成功；
3. 后果落在图的**规模**上，而图的规模没人会去核对——判决书里那家公司
   只是从图上消失了，自检照样全绿，决策照样出结论。

真实案例：``pyproject.toml`` 里声明的 PDF 依赖是 ``pypdf``，而解析器 import 的是
``pdfplumber``——一个从头到尾没被声明过的包。开发机上两个都装着，容器里只有一个，
于是一份判决书连同它带的那家企业一起消失了，而构建成功、自检通过。
"""

from __future__ import annotations

import glob
import json
import os

import pytest

from govfin.ingest.parsers import parse
from govfin.runtime import AgentRuntime

_ROOT = os.path.join(os.path.dirname(__file__), "..")

# 解析器认领的扩展名。少一个就意味着有一类材料永远进不了图。
_PARSEABLE = (".json", ".csv", ".txt", ".pdf", ".png", ".jpg", ".jpeg")


def _sample_documents() -> list[str]:
    files: list[str] = []
    for pattern in ("data/gov/*", "data/fin/*"):
        files.extend(glob.glob(os.path.join(_ROOT, pattern)))
    return sorted(f for f in files if not f.endswith(".ocr.json"))


def test_every_sample_document_is_actually_parseable():
    """样例里的每一份文档都必须解析出内容——不能只是"没报错"。

    断言的是 ``blocks`` 非空，而不是"调用没抛异常"。一份文档可以解析成功却
    产出零个块（例如 PDF 后端缺失时本该报错，但若某条路径改成静默返回空文档，
    调用方是看不出来的）。零块等于这份材料从未存在过。
    """
    documents = _sample_documents()
    assert documents, "找不到样例文档，这条测试就成了空转"

    empty: list[str] = []
    for path in documents:
        doc = parse(path)
        if not doc.blocks:
            empty.append(os.path.basename(path))

    assert not empty, (
        f"以下样例文档解析结果为空，说明对应的解析后端在按 pyproject.toml 安装的环境里不可用: {empty}"
    )


def test_pdf_backend_matches_the_declared_dependency():
    """声明为运行依赖的 PDF 后端，必须是解析器真正 import 的那个。

    这条测试卡的是一个具体的坑：声明 ``pypdf`` 而 import ``pdfplumber``。
    两个包都在本机时看不出问题，按声明装出来的干净环境里 PDF 直接解析不了。
    """
    import tomllib
    from pathlib import Path

    pyproject = Path(_ROOT) / "pyproject.toml"
    declared = " ".join(tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"])

    source = (Path(_ROOT) / "src" / "govfin" / "ingest" / "parsers" / "pdf_parser.py").read_text(
        encoding="utf-8"
    )
    assert "import pdfplumber" in source, "PDF 解析器换后端了，这条测试的假设需要更新"
    assert "pdfplumber" in declared, (
        "PDF 解析器 import pdfplumber，但 pyproject.toml 的依赖里没有它——"
        "按这份声明全新安装的环境将无法解析任何 PDF"
    )
    assert "pypdf" not in declared, "pypdf 已不再被任何代码 import，不该继续声明为运行依赖"


def test_ingest_reports_skipped_documents_instead_of_swallowing_them(tmp_path):
    """单份文档失败可以跳过整批，但**跳过了哪一份必须出现在报告里**。

    "不中断整批导入"这个决定是对的：一份坏文件不该让当天全部材料都进不来。
    但静默跳过和"这份文件本来就不存在"在报告上长得一模一样——读报告的人看到
    ``documents: 1``，会以为目录里只有一份文件，而不是"有两份、其中一份打不开"。
    """
    good = tmp_path / "工商登记.json"
    good.write_text(
        json.dumps(
            [{"记录编号": "GS-2026-9501", "企业名称": "寅装备有限公司", "注册资本": 100.0}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    broken = tmp_path / "损坏的判决书.pdf"
    broken.write_bytes(b"%PDF-1.4\nnot actually a valid pdf body\n")

    rt = AgentRuntime(in_memory=True)
    try:
        summary = rt.ingest_dir(str(tmp_path))
    finally:
        rt.close()

    assert summary["documents"] == 1, f"好文档应当照常入图: {summary}"
    assert summary["files_seen"] == 2, "文件总数要如实统计，不能只数成功的那份"
    assert len(summary["skipped"]) == 1, f"损坏的文档被静默跳过了: {summary}"
    assert summary["skipped"][0]["file"] == "损坏的判决书.pdf"
    assert summary["skipped"][0]["reason"], "跳过原因不能为空，否则无法判断是缺依赖还是文件坏了"
