"""规模压力测试：十万级节点上，导入、检索、推理是否还成立。

单元测试验证"逻辑对不对"，规模测试验证的是**另一类失效**——那些只在数据量
上去之后才出现的。这里盯住三类：

1. **静默丢数据**。节点 ID 取自自然键（名称/信用代码），重名主体会被写入时
   的 ``INSERT OR REPLACE`` 悄悄吞掉。十万个节点里少三个，没人看得出来；
   但它意味着三家企业的风险被算到了一家头上，而且没有任何异常可捕获。
2. **缓存失效策略在规模下反转**。邻接表是全量重建的。若读操作会推进写代数，
   一次决策里的十几次路径搜索就变成十几次全表重扫——小图上完全不可见（毫秒级），
   十万边上就是每次查询都超时。
3. **预算截断**。稠密图上不受限的 DFS 会走到天荒地老。设计上要求"触顶时如实
   上报 truncated 而不是挂住"，这条只有在真的大图上才验证得了。

压力测试默认不跑（见 pyproject 的 ``addopts``）。显式运行：

    pytest -m stress -s

``-s`` 是有用的：各阶段耗时通过 print 输出，是这套测试的主要产物之一。
"""

from __future__ import annotations

import time

import pytest

from govfin.graph.schema import EVIDENCE_DERIVED
from govfin.graph.store import GraphStore
from govfin.ontology.seed import build_seed_ontology
from govfin.reasoning.constraints import CONTAGION, PathConstraint
from govfin.reasoning.decision import DecisionSynthesizer
from govfin.reasoning.path import PathEngine

pytestmark = pytest.mark.stress

# 规模参数。企业数乘以 4 决定节点总量，乘 4 再加欠缴企业数决定边总量。
ENTERPRISES = 22_000
NATURAL_PERSONS = ENTERPRISES // 2
DIMENSION_COUNT = 4
ABNORMAL = len(range(0, ENTERPRISES, 3))  # 每三家企业一条欠缴记录

EXPECTED_NODES = ENTERPRISES * 4 + NATURAL_PERSONS + DIMENSION_COUNT * 3
EXPECTED_EDGES = ENTERPRISES * 4 + ABNORMAL + DIMENSION_COUNT * 3

CLARITY = 0.8  # 条款明确程度，与条款节点上的属性一致


def _phase(label: str, started: float) -> float:
    elapsed = time.perf_counter() - started
    print(f"    [stress] {label}: {elapsed:.2f}s")
    return time.perf_counter()


def _build_graph() -> tuple[GraphStore, dict, list[str]]:
    """建一张十万级的三层图。

    用 ``add_nodes_bulk`` / ``add_edges_bulk`` 而不是逐条写：这不只是为了快，
    更是因为逐条写会走十多万次单条事务提交，SQLite 在 WAL 下的 fsync 开销
    会让建图时间从秒级变成分钟级，压力测试本身就成了瓶颈。
    """
    store = GraphStore(in_memory=True, ontology=build_seed_ontology())
    timings: dict[str, float] = {}

    t0 = time.perf_counter()
    node_rows: list[dict] = []
    for i in range(ENTERPRISES):
        node_rows.append(
            {
                "ntype": "企业",
                "props": {
                    "名称": f"压力测试企业{i:05d}",
                    "统一社会信用代码": f"91TEST{i:012d}",
                    "注册资本": 1000 + i % 500,
                    "资产总额": 1000.0,
                    "负债总额": float(700 + i % 120),
                },
            }
        )
        node_rows.append({"ntype": "工商登记", "props": {"记录编号": f"GS{i:08d}", "登记状态": "存续"}})
        node_rows.append(
            {
                "ntype": "社保缴纳记录",
                "props": {
                    "记录编号": f"SB{i:08d}",
                    "缴纳状态": "欠缴" if i % 3 == 0 else "正常",
                    "缴纳月份": "2026-05",
                },
            }
        )
        node_rows.append(
            {
                "ntype": "风险指标",
                "props": {
                    "记录编号": f"ZB{i:08d}",
                    "指标名称": "欠缴月数" if i % DIMENSION_COUNT == 0 else "资产负债率",
                    "指标值": float(70 + i % 20),
                },
            }
        )
    for i in range(NATURAL_PERSONS):
        node_rows.append({"ntype": "自然人", "props": {"姓名": f"压力测试法人{i:05d}"}})
    for i in range(DIMENSION_COUNT):
        node_rows.append(
            {
                "ntype": "授信指引条款",
                "props": {
                    "条款编号": f"ZP{i:03d}",
                    "名称": f"压力测试条款{i}",
                    "条款原文": "企业连续欠缴社会保险费的，应审慎核定授信额度。",
                    "条款明确程度": CLARITY,
                },
            }
        )
        node_rows.append(
            {"ntype": "风险维度", "props": {"记录编号": f"WD{i:03d}", "名称": f"压力维度{i}"}}
        )
        # 阈值名称里的量词决定判定语义（见 decision._is_triggered）：
        # "上限" 触达即触发。维度 0 用计数量词，走"观测值 = 指标条数"分支；
        # 其余维度用取值阈值，走"观测值 = 同类指标最大值"分支。
        node_rows.append(
            {
                "ntype": "阈值",
                "props": {
                    "记录编号": f"YZ{i:03d}",
                    "阈值名称": "连续异常月数上限" if i == 0 else "资产负债率上限",
                    "阈值": 3 if i == 0 else 70,
                    "比较方向": "高于",
                },
            }
        )

    # 节点 ID 由存储层按自然键生成（``_make_id``），信用代码的优先级高于名称，
    # 因此在测试里手写 "企业:压力测试企业00000" 是错的。这里直接用存储层返回的
    # ID 建索引——既不用在测试里复刻一遍 ID 生成规则，也顺带验证了每条记录
    # 都拿到了**互不相同**的 ID。
    ids = store.add_nodes_bulk(node_rows)
    assert len(set(ids)) == len(ids), "批量写入返回了重复 ID，说明有节点的自然键发生了碰撞"
    timings["建节点"] = time.perf_counter() - t0
    t0 = _phase(f"建节点 {len(node_rows)} 个", t0)

    ent_ids = [ids[i * 4] for i in range(ENTERPRISES)]
    gs_ids = [ids[i * 4 + 1] for i in range(ENTERPRISES)]
    sb_ids = [ids[i * 4 + 2] for i in range(ENTERPRISES)]
    zb_ids = [ids[i * 4 + 3] for i in range(ENTERPRISES)]
    pn_base = ENTERPRISES * 4
    rule_base = pn_base + NATURAL_PERSONS
    dim_ids = [ids[rule_base + i * 3 + 1] for i in range(DIMENSION_COUNT)]

    def clause_of(i: int) -> str:
        """第 i 个风险指标所属维度对应的条款编号。

        派生的边必须带 ``evidence.source_document`` 指向条款，否则置信度传播
        取不到"条款明确程度"，只按边自身置信度算——这正是路径引擎那个静默
        失效 bug 的形状，测试数据必须覆盖到。
        """
        return f"ZP{i % DIMENSION_COUNT:03d}"

    edge_rows: list[dict] = []
    for i in range(ENTERPRISES):
        edge_rows.append({"src": ent_ids[i], "dst": gs_ids[i], "etype": "拥有工商登记"})
        edge_rows.append({"src": ent_ids[i], "dst": sb_ids[i], "etype": "缴纳社保"})
        edge_rows.append(
            {"src": ent_ids[i], "dst": ids[pn_base + i % NATURAL_PERSONS], "etype": "法定代表人"}
        )
        # 欠缴记录才会触发风险信号：图上必须存在"没事的企业"，否则
        # 阈值判定的负样本就没了，"触发"这个结论也就无从检验。
        if i % 3 == 0:
            edge_rows.append(
                {
                    "src": sb_ids[i],
                    "dst": zb_ids[i],
                    "etype": "触发风险信号",
                    "confidence": 0.9,
                    "evidence": {"source_document": clause_of(i), "source_locator": f"row:{i}"},
                }
            )
        # 方向必须符合本体：对应指标的 domain 是风险维度、range 是风险指标。
        # 写反了会被写入校验挡下——这正是本文件当初发现批量写入不校验的那条边。
        edge_rows.append(
            {
                "src": dim_ids[i % DIMENSION_COUNT],
                "dst": zb_ids[i],
                "etype": "对应指标",
                "evidence": {"source_document": clause_of(i), "source_locator": f"row:{i}"},
            }
        )
    for i in range(DIMENSION_COUNT):
        clause, dim_node, threshold = ids[rule_base + i * 3 : rule_base + i * 3 + 3]
        edge_rows.append(
            {
                "src": clause,
                "dst": dim_node,
                "etype": "映射维度",
                "evidence": {"source_document": f"ZP{i:03d}", "source_locator": "映射表"},
            }
        )
        edge_rows.append(
            {
                "src": clause,
                "dst": threshold,
                "etype": "设定阈值",
                "evidence": {"source_document": f"ZP{i:03d}", "source_locator": "阈值表"},
            }
        )
        edge_rows.append({"src": threshold, "dst": dim_node, "etype": "约束维度"})

    # 分批写边：``add_edges_bulk`` 每条边要做几次节点存在性查询，
    # 一次性塞十几万条会让那几次查询的中间态全留在内存里。
    batch = 10_000
    for start in range(0, len(edge_rows), batch):
        store.add_edges_bulk(edge_rows[start : start + batch])
    timings["建边"] = time.perf_counter() - t0
    _phase(f"建边 {len(edge_rows)} 条", t0)
    return store, timings, ent_ids, dim_ids


@pytest.fixture(scope="module")
def big() -> GraphStore:
    store, timings, ent_ids, dim_ids = _build_graph()
    store.timings = timings  # type: ignore[attr-defined]
    store.ent_ids = ent_ids  # type: ignore[attr-defined]
    store.dim_ids = dim_ids  # type: ignore[attr-defined]
    yield store
    store.close()


def _dimension_indicators(store: GraphStore, dim_index: int) -> list[dict]:
    """取某风险维度下的全部指标节点。观测值就是在这批数据上聚合出来的。"""
    dim_id = store.dim_ids[dim_index]
    return [
        node
        for node in (store.get_node(edge["dst"]) for edge in store.find_edges(src=dim_id, etype="对应指标"))
        if node is not None
    ]


# ----------------------------------------------------------------------


def test_scale_matches_expectation_exactly(big: GraphStore):
    """节点与边的数量必须与构造时的意图**逐条对上**。

    这条断言看似多余（"我写了多少就是多少"），但重名或 ID 碰撞导致的
    静默覆盖正是这样被发现的：写进去 22000 家企业，图里只剩 21998，
    而所有下游查询都照常工作，只是有两家的数据被并进了别人身上。
    """
    stats = big.stats()
    assert stats["nodes"] == EXPECTED_NODES, (
        f"节点数 {stats['nodes']} != 预期 {EXPECTED_NODES}，可能发生了 ID 碰撞导致的静默覆盖"
    )
    assert stats["edges"] == EXPECTED_EDGES, f"边数 {stats['edges']} != 预期 {EXPECTED_EDGES}"

    layers = stats["by_layer"]
    assert layers["实体图"]["nodes"] > 90_000
    assert layers["规则图"]["nodes"] == DIMENSION_COUNT * 3


def test_integrity_clean_at_scale(big: GraphStore):
    """十万级图上结构完整性必须干净。

    这条检查走的是和单元测试同一段代码，但数据量让它有机会真的报出问题：
    本文件当初就是靠它发现 ``add_edges_bulk`` 完全绕过了跨层边校验。
    """
    report = big.integrity_report()
    assert report["healthy"] is True, report["problems"]
    assert report["structure_check"] == "ok"
    assert report["foreign_key_check"] == 0


def test_reads_never_advance_write_generation(big: GraphStore):
    """读操作不得推进写代数。

    写代数是所有缓存的失效依据。读也推进的话，邻接表会在每次查询后
    被整表重建——十万边上这是每个请求几百毫秒的纯浪费，而且症状是
    "图越大越慢"，很容易被误诊为存储性能问题。"""
    before = big.write_generation()
    for i in range(50):
        big.neighbors(big.ent_ids[i], direction="both")
        big.query_nodes(ntype="风险指标", limit=10)
        big.query_nodes(ntype="风险维度", limit=10)
    assert big.write_generation() == before, "读路径推进了写代数，所有缓存会因此反复失效"


def test_adjacency_rebuilt_once_per_write_generation(big: GraphStore, monkeypatch):
    """一次完整决策的邻接表重建次数必须与**搜索次数无关**。

    决策内部要跑几十次路径搜索，每次都调 ``neighbors``。重建次数若与搜索次数
    同阶，规模一大就直接不可用。这条断言把"缓存确实生效"变成一个可观测的
    数字：几十次查找，至多一次重建。

    断言写成 ``<= 1`` 而不是 ``== 1``：夹具被上个测试预热过时重建次数就是 0，
    那不是缺陷而是缓存命中的证据。
    """
    rebuilds = 0
    lookups = 0
    original_ensure = GraphStore._ensure_adjacency
    original_neighbors = GraphStore.neighbors

    def counting_ensure(self):
        nonlocal rebuilds
        if self._adjacency_generation != self._write_generation:
            rebuilds += 1
        return original_ensure(self)

    def counting_neighbors(self, node_id, **kwargs):
        nonlocal lookups
        lookups += 1
        return original_neighbors(self, node_id, **kwargs)

    monkeypatch.setattr(GraphStore, "_ensure_adjacency", counting_ensure)
    monkeypatch.setattr(GraphStore, "neighbors", counting_neighbors)

    engine = PathEngine(big)
    decision = DecisionSynthesizer(big, engine=engine).synthesize(big.ent_ids[0])

    assert decision.verdict
    assert lookups > 20, f"只发生了 {lookups} 次邻接查找，这条断言验证不到缓存行为"
    assert rebuilds <= 1, f"{lookups} 次查找触发了 {rebuilds} 次邻接表重建，缓存没起作用"


def test_decision_completes_and_is_bounded(big: GraphStore):
    """十万级图上跑一次完整决策，结论必须与构造数据一致，且展开量有界。

    构造时该企业欠缴社保、风险指标值全部越过阈值，因此"触发监管阈值"
    是**已知答案**。规模测试若只断言"返回了结果"，就漏掉了最要命的一类
    退化：搜索结果被截断、置信度被算错、阈值判定为空——结论照样返回，
    只是错的。所以这里把答案写死。

    用第 3 号企业（欠缴、指标挂在维度 3 上，该维度的阈值按取值判定）。
    """
    engine = PathEngine(big)
    t0 = time.perf_counter()
    decision = DecisionSynthesizer(big, engine=engine).synthesize(big.ent_ids[3])
    elapsed = time.perf_counter() - t0
    print(f"    [stress] 单次决策: {elapsed:.3f}s")

    assert decision.accepted_paths, "该企业欠缴社保，必然能走到风险指标"
    assert decision.judgements, "欠缴 + 指标值超线，阈值判定不该为空"
    assert any(j.triggered for j in decision.judgements), "构造数据必然触发监管阈值"
    assert 0.0 < decision.confidence <= 1.0
    assert len(decision.rejected_paths) < 20000

    # 取值型阈值：观测值是同维度指标的**最大值**——注意是这个维度下全部
    # 指标的极值，而不是本企业那一条的取值。后者看起来更"合理"，但会让
    # 风险维度的含义从"这一类的整体状况"退化成"这一家的一个数"。
    indicators = _dimension_indicators(big, 3)
    assert len(indicators) > 1_000, "该维度下应有数千条指标，否则这条断言证明不了聚合口径"
    value_judgement = next(j for j in decision.judgements if "资产负债率" in j.threshold_name)
    assert value_judgement.observed_value == pytest.approx(
        max(node["props"]["指标值"] for node in indicators)
    )
    assert value_judgement.observed_value > 70, "构造数据必然越过 70 的阈值"
    assert value_judgement.triggered is True
    assert "最大取值" in value_judgement.observed_basis


def test_count_unit_threshold_uses_indicator_count_not_value(big: GraphStore):
    """阈值名称带计数量词时，观测值必须取指标**条数**而不是最大取值。

    这是"连续三个月欠缴"这类条款的正确读法：判定对象是事件发生的规模，
    不是某个指标的大小。取错分支不会报错，只会让"连续三个月"被一条
    指标值 89 判成触发——结论方向对，依据却是错的，而且审计时看不出来。
    """
    decision = DecisionSynthesizer(big, engine=PathEngine(big)).synthesize(big.ent_ids[0])

    count_judgement = next(j for j in decision.judgements if "月数" in j.threshold_name)
    expected = len(_dimension_indicators(big, 0))
    assert count_judgement.threshold_value == pytest.approx(3.0)
    assert count_judgement.observed_value == pytest.approx(float(expected))
    assert expected > 1_000, "该维度下应有数千条指标，否则取条数与取极值区分不出来"
    assert "同维度风险指标出现" in count_judgement.observed_basis
    assert count_judgement.triggered is True


def test_expansion_budget_reports_truncation_instead_of_hanging(big: GraphStore):
    """稠密图上不受约束的搜索必须触顶并**如实上报**，而不是挂住或静默截断。

    这是 DoS 抵抗的核心：调用方拿到 truncated=True 才知道结果不完整、
    该收紧约束重来。若这里改成静默返回部分结果，模型会把残缺路径当成全集，
    据此作出的授信结论查不到任何异常——最危险的一种"看起来正常"。

    约束刻意放宽到只限终点类型与一条必需边，模拟"调用方忘记设边界"：
    风险维度上有五千多条出边，不设预算的话 DFS 会在这一层炸开。
    """
    wide_open = PathConstraint(
        name="无边界展开压力",
        description="只限定终点与一条必经边，其余全部放开",
        required_edge_types=("对应指标",),
        end_types=("阈值",),
        max_hops=6,
        expansion_budget=1_500,
    )
    engine = PathEngine(big)
    t0 = time.perf_counter()
    result = engine.search(big.ent_ids[0], wide_open)
    elapsed = time.perf_counter() - t0
    print(f"    [stress] 预算触顶搜索: {elapsed:.3f}s")

    assert result.truncated is True, "无边界搜索没有触顶，说明预算没有生效"
    assert "预算" in result.truncated_reason
    assert result.explored_edges == 1_500, "触顶后应当停止展开，而不是继续扫"
    assert elapsed < 30, f"触顶搜索耗时 {elapsed:.1f}s，说明截断没有真正生效"


def test_expansion_budget_does_not_fire_on_constrained_search(big: GraphStore):
    """同一起点上，有边界的约束**不应**触顶。

    与上一条成对：只验证"会截断"是不够的，还得验证截断不会在正常业务约束下
    误触发。否则把预算调小就能让所有测试通过，而线上表现为大量查询静默返回
    残缺结果。CONTAGION 的边类型白名单把扇出限制在个位数。
    """
    result = PathEngine(big).search(big.ent_ids[0], CONTAGION)
    assert result.truncated is False
    assert result.explored_edges < 1_000, f"有边界约束仍展开了 {result.explored_edges} 条边"
    assert result.paths, "该企业欠缴，关联方传导应当有采纳路径"


def test_multi_hop_reachability_terminates_at_scale(big: GraphStore):
    """递归 CTE 的多跳可达查询在十万边上必须收敛。

    这条路径在数据库内迭代，没有 Python 层的展开预算兜底——真出现环，
    它会把 CPU 吃光而不返回。图里存在"企业→自然人"与"自然人→企业"这类
    天然成对的边，正是最容易形成环的形状。
    """
    t0 = time.perf_counter()
    reached = big.neighbors_within_hops(big.ent_ids[0], 3, direction="out")
    elapsed = time.perf_counter() - t0
    print(f"    [stress] 3 跳可达: {len(reached)} 个节点 / {elapsed:.3f}s")

    assert reached, "该企业有登记、社保、法人三类出边，3 跳内必然可达"
    assert elapsed < 20
    assert all(d >= 1 for d in reached.values())
    # 自身不应出现在可达集合里
    assert big.ent_ids[0] not in reached


def test_bulk_read_export_scales_linearly(big: GraphStore):
    """全量导出是 O(节点数 + 边数)，且结果能被下游解析器接受。

    导出走的是全表迭代，最容易出的问题是每行一次 JSON 解析或字符串拼接
    导致的隐式平方复杂度。断言"第二次比第一次快"在负载波动的机器上会 flaky，
    因此这里只断言**总量正确**与**有界耗时**。
    """
    t0 = time.perf_counter()
    edges = big.all_edges()
    first = time.perf_counter() - t0
    t0 = time.perf_counter()
    edges_again = big.all_edges()
    second = time.perf_counter() - t0
    print(f"    [stress] all_edges 首次 {first:.3f}s / 缓存后 {second:.3f}s")

    assert len(edges) == EXPECTED_EDGES
    assert len(edges_again) == EXPECTED_EDGES
    assert second <= max(first * 2, 0.5), "缓存命中后不该比首次重建更慢"
    assert all(e["src"] and e["dst"] and e["etype"] for e in edges)


def test_derived_edges_carry_clause_clarity_at_scale(big: GraphStore):
    """规模数据里派生边必须真的拿到条款明确程度加权。

    这条索引按写代数惰性重建，且要靠边的 ``evidence.source_document``
    接回条款节点。任何一环断掉，索引就是空的——路径分数照算，只是系统性偏高，
    「措辞越模糊、推导越不可靠」整个设计静默失效。
    """
    engine = PathEngine(big)
    clarity = engine._rule_clarity
    derived_total = sum(
        1 for e in big.all_edges() if e.get("evidence_class") == EVIDENCE_DERIVED
    )
    print(f"    [stress] 条款明确程度索引覆盖 {len(clarity)} / {derived_total} 条派生边")

    assert clarity, "索引为空：派生边全部退化为按自身置信度加权"
    assert set(clarity.values()) == {CLARITY}
    # 三分之一企业有触发风险信号边，加上全部对应指标与映射维度边
    assert len(clarity) >= ABNORMAL + ENTERPRISES


def test_write_generation_is_monotonic_at_scale(big: GraphStore):
    """写代数必须严格单调，且一次批量写入只推进一代。

    缓存方靠它判断失效。若批量写推进 N 代，缓存会在一批数据内反复作废；
    若不推进，缓存会永远停在旧图上——后者更危险，因为查询结果不会报错，
    只是永远看不到新数据。
    """
    before_gen = big.write_generation()
    before_nodes = big.stats()["nodes"]

    node_id = big.add_node(
        "企业", props={"名称": "写代数探针企业", "统一社会信用代码": "91PROBE000000000001"}
    )
    assert big.write_generation() == before_gen + 1
    assert big.stats()["nodes"] == before_nodes + 1

    assert big.delete_node(node_id) == 1
    assert big.write_generation() == before_gen + 2
    assert big.stats()["nodes"] == before_nodes, "探针节点未被清理，会污染后续测试"


def test_replace_on_existing_edge_keeps_edge_count_stable(big: GraphStore):
    """重复写入同一条边不得让边数增长。

    边的唯一键是 ``(src, dst, etype, layer)``，重写走 ``INSERT OR REPLACE``。
    这条约束在批量导入时是防重的关键：政务数据经常整表重发，
    如果重发导致边翻倍，置信度传播会把这些重复边当成多份独立证据。
    """
    before = big.stats()["edges"]
    edge = big.find_edges(src=big.ent_ids[0], etype="拥有工商登记")[0]
    big.add_edge(edge["src"], edge["dst"], edge["etype"], confidence=0.9)
    assert big.stats()["edges"] == before, "重写同一条边不应产生新边"
    assert big.get_edge(edge["id"])["confidence"] == pytest.approx(0.9)

    # 复原，避免影响同模块内其他测试对置信度的假设
    big.add_edge(edge["src"], edge["dst"], edge["etype"], confidence=1.0)
    assert big.get_edge(edge["id"])["confidence"] == pytest.approx(1.0)
