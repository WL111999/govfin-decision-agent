"""变异验证：把每个修复回退掉，断言对应的测试必须变红。

**一个不会变红的测试等于没有测试。** 测试在修复之前写、在修复之后跑绿，这只说明
"现在不报错"，不说明它盯住了那个缺陷——它完全可能因为别的原因通过，或者压根没走到
出问题的那条分支。唯一能证明它有效的方法，是把修复撤掉再看一遍。

这个脚本做的是自动化版本：逐条把源码改回有缺陷的写法，跑指定测试，跑完立刻还原。
任何一条**没有变红**，就说明那个缺陷目前没有测试在盯——这是需要补用例的信号，
不是可以忽略的噪音。（首轮跑出 2 处未被检出，都落在 LLM 抽取通道上，
补了两条针对性用例后才全部通过。）

跑法：
    PYTHONPATH=src python -X utf8 scripts/verify_mutations.py
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PYTHON = sys.executable
TEST_FILE = "tests/chaos/test_adversarial.py"

# (说明, 相对路径, 原文本, 回退成什么, 应当变红的测试)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "OCR 营业执照取值切回 match.end()（值恒为空）",
        "src/govfin/ingest/parsers/image_parser.py",
        "        start = match.start(2)\n",
        "        start = match.end()\n",
        f"{TEST_FILE}::test_injected_document_cannot_launder_provenance",
    ),
    (
        "企业名脏值原样收下（图上长出假主体）",
        "src/govfin/ingest/extractor.py",
        "        return m.group(1) if m else None\n",
        "        return m.group(1) if m else v\n",
        f"{TEST_FILE}::test_a_corrupted_document_cannot_take_down_the_batch",
    ),
    (
        "装载报告不并入抽取期拒绝（丢值无声）",
        "src/govfin/ingest/loader.py",
        "        report.rejected.extend(result.rejected)\n",
        "        pass\n",
        f"{TEST_FILE}::test_a_corrupted_document_cannot_take_down_the_batch",
    ),
    (
        "来源列表截断不计数",
        "src/govfin/graph/store.py",
        '    if dropped > 0:\n        out["sources_dropped"] = int(out.get("sources_dropped") or 0) + dropped\n',
        "",
        f"{TEST_FILE}::test_provenance_truncation_is_counted_not_silent",
    ),
    (
        "节点缓存短路跳过合并（后到字段洗白既有值）",
        "src/govfin/ingest/loader.py",
        "            self.store.merge_from_source(cached, props, source=_source_of(tup))\n            return cached\n",
        "            return cached\n",
        f"{TEST_FILE}::test_llm_attributes_obey_the_same_merge_rules_as_rule_attributes",
    ),
    (
        "数据属性绕开来源归属，改走 update_node_props",
        "src/govfin/ingest/loader.py",
        "            self.store.merge_from_source(node_id, patch, source=_source_of(tup))\n",
        "            self.store.update_node_props(node_id, patch)\n",
        f"{TEST_FILE}::test_forged_record_cannot_whitewash_an_existing_risk_signal",
    ),
    (
        "OCR 纠正表连合法字符 B 一起改",
        "src/govfin/ingest/parsers/image_parser.py",
        '        cleaned = value.translate(_USCC_CONFUSION).replace(" ", "").upper()\n',
        '        cleaned = value.translate(_OCR_CONFUSION).replace(" ", "").upper()\n',
        f"{TEST_FILE}::test_ocr_correction_never_rewrites_a_legal_character",
    ),
    (
        "多企业文档里用「最后出现的那家」兜底归属",
        "src/govfin/ingest/extractor.py",
        "        owner = companies[0] if len(companies) == 1 else None\n",
        "        owner = companies[-1] if companies else None\n",
        f"{TEST_FILE}::test_multi_entity_document_never_guesses_a_document_level_owner",
    ),
    (
        "数值按字符串比（5000 与 5000.0 变成假冲突）",
        "src/govfin/graph/store.py",
        "    if isinstance(old, (int, float)) and isinstance(new, (int, float)):\n        return float(old) == float(new)\n",
        "",
        f"{TEST_FILE}::test_equivalent_numbers_from_different_sources_do_not_raise_a_phantom_conflict",
    ),
    (
        "PDF 依赖声明改回那个从没被 import 的 pypdf",
        "pyproject.toml",
        '    "pdfplumber>=0.11",\n',
        '    "pypdf>=4.0",\n',
        "tests/test_ingest_completeness.py::test_pdf_backend_matches_the_declared_dependency",
    ),
    (
        "导入时静默跳过解析失败的文档",
        "src/govfin/runtime.py",
        '                totals["skipped"].append(\n'
        '                    {"file": path.name, "reason": f"{type(exc).__name__}: {exc}"}\n'
        "                )\n"
        "                continue\n",
        "                continue\n",
        "tests/test_ingest_completeness.py::test_ingest_reports_skipped_documents_instead_of_swallowing_them",
    ),
]


def _run(test: str) -> bool:
    """跑一条测试，返回是否通过。"""
    result = subprocess.run(
        [PYTHON, "-X", "utf8", "-m", "pytest", test, "-q", "-p", "no:randomly", "--no-header", "-x"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode == 0


def main() -> int:
    undetected: list[str] = []
    for label, rel, original_text, broken_text, test in MUTATIONS:
        path = ROOT / rel
        source = path.read_text(encoding="utf-8")
        if original_text not in source:
            print(f"[锚点失效] {label}  —— {rel} 中找不到待替换文本，脚本需要更新")
            undetected.append(label)
            continue
        path.write_text(source.replace(original_text, broken_text, 1), encoding="utf-8")
        try:
            still_green = _run(test)
        finally:
            path.write_text(source, encoding="utf-8")
        if still_green:
            print(f"[未检出] {label}")
            print(f"         回退后 {test.split('::')[-1]} 仍然通过——该缺陷目前没有测试盯住")
            undetected.append(label)
        else:
            print(f"[已检出] {label}")

    print()
    if undetected:
        print(f"{len(undetected)}/{len(MUTATIONS)} 处未被检出，需要补用例：")
        for label in undetected:
            print(f"  - {label}")
        return 1
    print(f"{len(MUTATIONS)}/{len(MUTATIONS)} 处变异全部被检出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
