"""演化管道演示：真实文档建图 → UNK 观测 → 一轮演化 → 图重索引。

跑法：
    PYTHONPATH=src python -X utf8 scripts/demo_evolution.py            # 无 LLM，走人工仲裁队列
    PYTHONPATH=src python -X utf8 scripts/demo_evolution.py --llm      # 走 DeepSeek 生成提案
"""

from __future__ import annotations

import argparse
import glob
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from govfin.evolution import OntologyEvolutionPipeline  # noqa: E402
from govfin.evolution.unk_pool import KIND_ENTITY, KIND_RELATION  # noqa: E402
from govfin.graph.binder import RuleBinder  # noqa: E402
from govfin.graph.store import GraphStore  # noqa: E402
from govfin.ingest.extractor import Extractor  # noqa: E402
from govfin.ingest.loader import GraphLoader  # noqa: E402
from govfin.ingest.multimodal import make_tuple  # noqa: E402
from govfin.ingest.parsers import parse  # noqa: E402
from govfin.ontology.seed import build_seed_ontology  # noqa: E402

# 低资源域的典型 UNK 观测：现有本体里没有"经营异常名录"这个概念，
# 但它反复出现在工商登记、行政处罚的上下文里。同一概念的三种写法同时出现，
# 用来验证聚类是否能把它们归到一起、别名学习是否会先于新增类生效。
UNK_OBSERVATIONS = [
    ("经营异常名录", KIND_ENTITY, "doc:gs-2026-0041", "企业被列入经营异常名录，登记状态异常", "工商登记"),
    ("经营异常名录", KIND_ENTITY, "doc:cf-2026-0117", "因未按期年报被列入经营异常名录", "行政处罚"),
    ("经营异常名录信息", KIND_ENTITY, "doc:gs-2026-0058", "工商登记经营异常名录信息显示异常", "工商登记"),
    ("经营异常名录信息", KIND_ENTITY, "doc:sf-2026-0233", "涉诉记录提及该企业经营异常名录信息", "司法涉诉"),
    ("异常经营名录", KIND_ENTITY, "doc:gs-2026-0072", "全国企业信用信息公示系统显示异常经营名录", "工商登记"),
    ("异常经营名录", KIND_ENTITY, "doc:sw-2026-0031", "税务缴纳记录中标注异常经营名录", "税务缴纳记录"),
    # 别名学习用例：带后缀的写法应当剥掉后缀命中已有的「社保缴纳记录」
    ("社保缴纳记录信息", KIND_ENTITY, "doc:ss-2026-0002", "社保缴纳记录信息显示实缴人数下降", "社保缴纳记录"),
    ("社保缴纳记录信息", KIND_ENTITY, "doc:ss-2026-0003", "调取社保缴纳记录信息核验用工规模", "社保缴纳记录"),
    # 关系候选 A：上下文两侧都能定位到已有类（企业 → 行政处罚），可推导 domain/range
    ("触发", KIND_RELATION, "doc:cf-2026-0117", "该企业行为触发行政处罚立案调查", None),
    ("触发", KIND_RELATION, "doc:cf-2026-0120", "企业违规触发行政处罚并记入公示", None),
    # 关系候选 B：range 侧是尚未被本体承认的概念（经营异常名录），应当保守拒绝
    ("被列入", KIND_RELATION, "doc:gs-2026-0041", "企业在工商登记中被列入经营异常名录", None),
    ("被列入", KIND_RELATION, "doc:cf-2026-0118", "企业在行政处罚中被列入异常名录", None),
    # 噪声：形如符号碎片，应当在符号层被直接拒绝，而不是长成一个类
    ("合计", KIND_ENTITY, "doc:fin-2026-001", "合计 1,234.56", None),
    ("合计", KIND_ENTITY, "doc:fin-2026-002", "合计 987.65", None),
]


def build_graph() -> tuple[GraphStore, object, int]:
    ontology = build_seed_ontology()
    store = GraphStore(in_memory=True, ontology=ontology)
    loader = GraphLoader(store, ontology)
    extractor = Extractor(ontology, use_llm=False)

    files = [
        f
        for f in sorted(glob.glob("data/gov/*") + glob.glob("data/fin/*"))
        if not f.endswith(".ocr.json")
    ]
    for path in files:
        loader.load(extractor.extract(parse(path)))
    report = RuleBinder(store, ontology).bind()
    return store, report, len(files)


def observe(pipeline: OntologyEvolutionPipeline) -> dict:
    """把 UNK 观测灌进储备池。

    真实链路上这一步由 ``pipeline.observe_from(extraction_result)`` 自动完成；
    这里手工构造，是为了让演示不依赖抽取器能否恰好踩中 UNK 分支。
    """
    for text, kind, doc, ctx, hint in UNK_OBSERVATIONS:
        pipeline.pool.observe(
            text,
            kind,
            source_document=doc,
            context=ctx,
            hint_type=hint,
        )
    # 另造一条真正的五元组，验证"重放"链路：概念被接纳后这条证据要能重新进图
    for i in range(3):
        tup = make_tuple(
            entity_mention="经营异常名录",
            entity_type="UNK-ENTITY",
            relation_candidate="甲科技有限公司",
            relation_target="经营异常名录",
            relation_type="UNK-RELATION",
            evidence_snippet="甲科技有限公司被列入经营异常名录",
            modality="text",
            source_document=f"doc:replay-{i}",
            source_locator=f"para:{i}",
            confidence=0.8,
        )
        pipeline.pool.observe(
            tup.entity_mention,
            KIND_ENTITY,
            source_document=tup.source_document,
            context=tup.evidence_snippet,
            hint_type="工商登记",
            tuple_payload=tup.to_dict(),
        )
    return pipeline.pool.stats()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true", help="调用 DeepSeek 生成提案（默认走启发式）")
    ap.add_argument("--arbitrate", action="store_true", help="模拟人工仲裁队列全部采纳")
    args = ap.parse_args()

    store, bind_report, n_files = build_graph()
    print(f"[1] 建图完成：{n_files} 份文档，节点 {store.stats()['nodes']}，边 {store.stats()['edges']}")
    print(f"    绑定：{bind_report.to_dict()}")

    client = None
    if args.llm:
        from govfin.llm.factory import build_client

        client = build_client()
        print("[2] 已启用 DeepSeek 生成提案")
    else:
        print("[2] 未启用 LLM，提案走确定性启发式（置信度上限 0.45，必然转人工仲裁）")

    pipeline = OntologyEvolutionPipeline(
        store,
        ontology=store.ontology,
        client=client,
        use_llm=args.llm,
        min_observations=2,
        persist_path=Path("data/ontology.json"),
    )
    print(f"[3] 储备池：{observe(pipeline)}")

    report = pipeline.run_cycle()
    data = report.to_dict()
    print("[4] 演化报告：")
    for key in (
        "version_before",
        "version_after",
        "clustered",
        "alias_learned",
        "classes_added",
        "properties_added",
        "auto_approved",
        "pending_arbitration",
        "replayable_tuples",
        "reindexed_nodes",
        "reindexed_edges",
    ):
        print(f"    {key:22} {data[key]}")
    if data["rejected"]:
        print(f"    拒绝 {len(data['rejected'])} 条，样例：")
        for item in data["rejected"][:5]:
            print(f"      - {item['canonical']}：{item['reason'][:80]}")

    pending = pipeline.pending_proposals()
    if pending:
        print(f"[5] 人工仲裁队列（{len(pending)} 条）：")
        for item in pending[:5]:
            print(f"    候选「{item['候选']}」 动作 {item['动作']} 置信度 {item['置信度']:.2f}")
            print(f"      编辑: {item['编辑']}")
            print(f"      等待原因: {item['等待原因']}")

    if args.arbitrate and pending:
        print("[6] 模拟人工仲裁：全部采纳，观察本体变更如何把此前无家可归的证据拉回图中")
        before = store.stats()
        for item in pending:
            pipeline.arbitrate(item["proposal_node"], approve=True, actor="reviewer", note="演示通过")
        after = store.stats()
        print(f"    节点 {before['nodes']} → {after['nodes']}，边 {before['edges']} → {after['edges']}")
        print(f"    本体：{store.ontology.version}，类 {len(store.ontology.classes)}")
        for name in (store.ontology.classes.keys() - build_seed_ontology().classes.keys()):
            cls = store.ontology.classes[name]
            print(f"    新类「{name}」父类 {cls.parent} 引入版本 {cls.introduced_in_version}")

    print(f"[7] 三图规模：{store.stats()}")
    print(f"    本体版本链：{[(h['version'], h['actor']) for h in store.ontology_version_history()]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
