"""共享测试夹具：所有测试用同一套从真实样例文档构建的图，避免各自造数漂移。"""

from __future__ import annotations

import glob
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from govfin.ingest.parsers import parse  # noqa: E402
from govfin.runtime import AgentRuntime  # noqa: E402

DATA_GLOB = ("data/gov/*", "data/fin/*")


def _sample_documents() -> list[str]:
    root = os.path.join(os.path.dirname(__file__), "..")
    files: list[str] = []
    for pattern in DATA_GLOB:
        files.extend(glob.glob(os.path.join(root, pattern)))
    return sorted(f for f in files if not f.endswith(".ocr.json"))


def _build_runtime() -> AgentRuntime:
    rt = AgentRuntime(in_memory=True)
    for path in _sample_documents():
        rt.loader.load(rt.extractor.extract(parse(path)))
    rt.loader.consolidate()
    rt.binder.bind()
    return rt


@pytest.fixture(scope="session")
def runtime() -> AgentRuntime:
    """全量导入样例数据后的运行时。session 级：建图比断言本身贵得多。

    **这个夹具是只读的**。会话级夹具被所有测试共享，任何一个测试改动了图，
    后续测试就会看到被污染的世界——这类失败表现为"单独跑通过、全量跑失败"，
    排查成本极高。需要改动图的测试请用 ``mutable_runtime``。
    """
    return _build_runtime()


@pytest.fixture
def mutable_runtime() -> AgentRuntime:
    """独占一份图，供会改动数据的测试使用，用完即弃。"""
    return _build_runtime()


@pytest.fixture(scope="session")
def tools(runtime: AgentRuntime):
    from govfin.tools import DomainTools

    return DomainTools(runtime)
