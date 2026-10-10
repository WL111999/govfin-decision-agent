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
    (
        "导入工具对非法 base64 也报成功",
        "src/govfin/mcp/server.py",
        '            return {"ok": False, "error": f"content 不是合法的 base64: {exc}"}\n',
        '            return {"ok": True}\n',
        "tests/test_mcp_a2a.py::test_ingest_document_rejects_bad_input_without_raising",
    ),
    (
        "导入工具报出的增量与图的实际变化不符",
        "src/govfin/mcp/server.py",
        '                "nodes": after.get("nodes", 0) - before.get("nodes", 0),\n',
        '                "nodes": 0,\n',
        "tests/test_mcp_a2a.py::test_ingest_document_actually_puts_material_on_the_graph",
    ),
    (
        "占位 agent_id 改回 0（界面会判成格式错误）",
        "scripts/build_nexent_bundle.py",
        "PLACEHOLDER_AGENT_ID = 1\n",
        "PLACEHOLDER_AGENT_ID = 0\n",
        "tests/test_nexent_bundle.py::test_placeholder_agent_id_is_truthy",
    ),
    (
        "约束提示词不再要求 <code> 块",
        "scripts/build_nexent_bundle.py",
        "调用工具**必须写成 Python 代码，放在 `<code>` 块里**：\n",
        "调用工具时请按规范格式书写。\n",
        "tests/test_nexent_bundle.py::test_constraint_prompt_mandates_code_block_calls",
    ),
    (
        "约束提示词不再点名禁止 DSML 标记",
        "scripts/build_nexent_bundle.py",
        "**绝对不要**输出工具调用标记——不要 `DSML`、不要 `<tool_call>`、不要 JSON 形式的\n",
        "",
        "tests/test_nexent_bundle.py::test_constraint_prompt_mandates_code_block_calls",
    ),
    (
        "few-shots 不再给可照抄的调用示例",
        "scripts/build_nexent_bundle.py",
        "## 第一轮：核验真实性\n\n<code>\n",
        "## 第一轮：核验真实性\n\n",
        "tests/test_nexent_bundle.py::test_few_shots_show_real_code_blocks",
    ),
    (
        "步骤表引用了不存在的工具名",
        "app/server.py",
        '"tool": "gov_judicial_scan",\n        "label": "司法与处罚",',
        '"tool": "gov_judicial_scann",\n        "label": "司法与处罚",',
        "tests/test_desktop_app.py::test_every_step_tool_is_actually_registered",
    ),
    (
        "步骤表给工具传了它不接受的参数",
        "app/server.py",
        'lambda subject, decision_id: {"subject": subject, "persist": True},',
        'lambda subject, decision_id: {"subject": subject, "persist": True, "bogus": 1},',
        "tests/test_desktop_app.py::test_step_argument_names_match_the_tool_signatures",
    ),
    (
        "写 .env 时整体覆盖（丢掉注释与别的键）",
        "app/paths.py",
        "    with path.open(\"w\", encoding=\"utf-8\", newline=\"\\n\") as fh:\n"
        "        fh.write(\"\\n\".join(out) + \"\\n\")\n",
        "    with path.open(\"w\", encoding=\"utf-8\", newline=\"\\n\") as fh:\n"
        "        fh.write(\"\\n\".join(f\"{k}={v}\" for k, v in updates.items()) + \"\\n\")\n",
        "tests/test_desktop_app.py::test_env_write_preserves_untouched_lines",
    ),
    (
        "写 .env 时把换行符改成 CRLF",
        "app/paths.py",
        "    with path.open(\"w\", encoding=\"utf-8\", newline=\"\\n\") as fh:\n"
        "        fh.write(\"\\n\".join(out) + \"\\n\")\n",
        "    with path.open(\"w\", encoding=\"utf-8\") as fh:\n"
        "        fh.write(\"\\r\\n\".join(out) + \"\\r\\n\")\n",
        "tests/test_desktop_app.py::test_env_write_preserves_line_endings",
    ),
    (
        "项目目录只看 pyproject.toml（误认别人的包目录）",
        "app/paths.py",
        'PROJECT_MARKERS = ("pyproject.toml", "deploy/Dockerfile")',
        'PROJECT_MARKERS = ("pyproject.toml",)',
        "tests/test_desktop_app.py::test_looks_like_project_requires_both_markers",
    ),
    (
        "首页路由没被替换（打开来还是旧控制台）",
        "app/server.py",
        "            route.endpoint = desktop_index\n"
        "            route.dependant.call = desktop_index\n"
        "            return\n",
        "            return\n",
        "tests/test_desktop_app.py::test_root_route_serves_the_desktop_page",
    ),
    (
        "不补标准流（无控制台时 uvicorn 直接崩）",
        "app/main.py",
        "    logging_setup.install_streams()\n",
        "    pass\n",
        "tests/test_desktop_app.py::test_main_fills_streams_before_starting_the_service",
    ),
    (
        "日志流不实现 isatty（uvicorn 上色判断失败）",
        "app/logging_setup.py",
        "    def isatty(self) -> bool:\n        return False\n\n",
        "",
        "tests/test_desktop_app.py::test_log_stream_survives_the_calls_libraries_actually_make",
    ),
    (
        "补标准流时把好的也一起替换",
        "app/logging_setup.py",
        "    if sys.stdout is None:\n"
        "        sys.stdout = _LogStream(\"stdout\")\n"
        "    if sys.stderr is None:\n"
        "        sys.stderr = _LogStream(\"stderr\")\n",
        "    sys.stdout = _LogStream(\"stdout\")\n"
        "    sys.stderr = _LogStream(\"stderr\")\n",
        "tests/test_desktop_app.py::test_install_streams_fills_only_missing_ones",
    ),
    (
        "一键启动里偷偷构建镜像",
        "app/deploy.py",
        '    job.log("")\n'
        '    job.log("启动容器")\n'
        '    console_env = _console_env_args(project)\n'
        '    for name, spec in CONTAINERS.items():\n'
        '        state = container_state(name)',
        '    for _n, _s in CONTAINERS.items():\n'
        '        run([docker, "build", "-f", _s["dockerfile"], "-t", _s["image"], _s["context"]], timeout=600)\n'
        '    job.log("")\n'
        '    job.log("启动容器")\n'
        '    console_env = _console_env_args(project)\n'
        '    for name, spec in CONTAINERS.items():\n'
        '        state = container_state(name)',
        "tests/test_desktop_app.py::test_start_services_never_builds_images",
    ),
    (
        "镜像缺失时静默跳过（用户对着空白进度条干等）",
        "app/deploy.py",
        '    missing = [spec["image"] for spec in CONTAINERS.values() if not image_exists(spec["image"])]',
        '    missing = []',
        "tests/test_desktop_app.py::test_missing_image_is_reported_not_silently_rebuilt",
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
