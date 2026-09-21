"""对抗测试：把"输入端不可信"当成前提，而不是当成意外。

前面几类故障测的是系统**自己**会不会坏（脏读、事务泄漏、LLM 挂了）。这一类的
前提完全不同：上游送进来的数据本身就是冲着结论去的。政务数据在现实里的来源
至少有三方——登记机关的接口、影像库的 OCR、以及各家企业自己报送的材料——它们
之间没有仲裁者，系统就是那个仲裁者。

本文件盯住六类攻击：

1. **风险洗白**。重发一条记录、把"欠缴"改成"正常缴纳"。风控体系里最划算的
   攻击：不需要删数据，只需要让同一个记录编号再说一次话。
2. **身份盗用**。拿别家的统一社会信用代码去登记一家不存在的公司。信用代码是
   实体归并的唯一依据，一旦被冒用，两家企业的事实会并到一起。
3. **归属投毒**。字段顺序本身是攻击面：把"统一社会信用代码"排在"企业名称"
   前面，就能让 A 家的处罚记录挂到 B 家名下。
4. **OCR 混淆纠正制造错误**。纠正表把**合法字符**也一起改了，等于系统自己
   伪造字段值——比收到错数据更糟，因为没有任何来源能解释这个改动。
5. **注入串**。往节点 id、名称、属性里塞 Cypher 语句、控制字符、零宽字符。
6. **溯源洗白**。把一份伪造文档挂到既有实体上，冒充它的数据来源。

每一条的断言都写成"这个具体的坏结果没有发生"，而不是"没有抛异常"——攻击成功
的标志恰恰是不抛异常。
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil

import pytest

from govfin.ingest.multimodal import ParsedDocument
from govfin.ingest.parsers import parse, parse_bytes
from govfin.graph.schema import MODALITY_TEXT
from govfin.runtime import AgentRuntime
from govfin.tools import DomainTools

pytestmark = pytest.mark.chaos

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_ATTACKER = "伪造_社保缴纳记录.json"

# 攻击者控制的公司。刻意用既有的信用代码：盗用的前提就是"别人已经验过这个号"。
_FORGED_IDENTITY = [
    {
        "记录编号": "GS-2026-9001",
        "统一社会信用代码": "91310115MA1K3XYA01",  # 甲科技有限公司的真实代码
        "企业名称": "丁咨询服务有限公司",
        "注册资本": 10.0,
        "登记状态": "存续",
    }
]


def _load_json(rt: AgentRuntime, records: list[dict], source: str) -> dict:
    """把一份内存里的 JSON 送进抽取—装载管道，返回装载报告。"""
    raw = json.dumps(records, ensure_ascii=False).encode("utf-8")
    doc = parse_bytes(raw, source=source, suffix=".json")
    return rt.loader.load(rt.extractor.extract(doc)).to_dict()


def _sample(path: str) -> str:
    return os.path.join(_ROOT, "data", path)


def _record_node(store, record_id: str) -> dict | None:
    for node in store.query_nodes(limit=None):
        if str(node["props"].get("记录编号") or "") == record_id:
            return node
    return None


def _owner_of(store, record: dict) -> str:
    """这条记录被挂在哪家企业名下。"""
    for edge in store.find_edges(dst=record["id"]):
        src = store.get_node(edge["src"])
        if src is not None and src["ntype"] == "企业":
            return str(src["label"])
    return ""


def _conflicts(node: dict) -> list[dict]:
    return list(node["props"].get("__冲突__") or [])


# ----------------------------------------------------------------------
# 一、风险洗白
# ----------------------------------------------------------------------


def test_forged_record_cannot_whitewash_an_existing_risk_signal(mutable_runtime: AgentRuntime):
    """重发一条社保记录、把"欠缴"改成"正常缴纳"，不得把风险信号洗掉。

    这是性价比最高的攻击，因为它完全不像攻击：政务数据本来就整表重发，一条
    记录编号重复出现是日常。系统若按"后到的说了算"合并，洗白就成立了，而且
    事后**看不出发生过什么**——节点的 first_seen 还指着最初那份文件，审计追
    到这里会以为那句话就是原文写的。

    因此断言分三层，缺一层都不算防住：值没被改掉、伪造来源被记在案、决策当时
    看见的旧值与现在对不上这件事能被审计发现。
    """
    tools = DomainTools(mutable_runtime)
    store = mutable_runtime.store

    before = tools.risk_decision("乙贸易有限公司")
    assert before["ok"] and before["verdict"] == "审慎核定", "样例数据本该判定为审慎核定"

    record_id = "社保缴纳记录:SS-202601-003"
    original = store.get_node(record_id)
    assert original["props"]["缴纳状态"] == "欠缴"
    assert original["props"]["实缴人数"] == 4

    _load_json(
        mutable_runtime,
        [
            {
                "id": "SS-202601-003",
                "企业名称": "乙贸易有限公司",
                "统一社会信用代码": "91310115MA1K3XYB02",
                "费款所属期": "2026-01",
                "缴费基数": 8200.0,
                "参保人数": 12,
                "征缴状态": "正常",
            }
        ],
        _ATTACKER,
    )

    after = store.get_node(record_id)
    assert after["props"]["缴纳状态"] == "欠缴", "伪造记录把风险状态洗成了正常缴纳"
    assert after["props"]["实缴人数"] == 4, "伪造记录改写了实缴人数"

    forged = [c for c in _conflicts(after) if c["来源"].get("document") == _ATTACKER]
    assert forged, "两个来源说法不一致，节点上却没有留下任何冲突记录"
    assert {c["字段"] for c in forged} >= {"缴纳状态", "实缴人数"}
    assert all(c["保留值"] in ("欠缴", 4) for c in forged), "冲突表把伪造值记成了保留值"

    # 审计这一侧：旧决策冻结的欠缴状态与现状对不上，必须报出来
    drift = mutable_runtime.trail.detect_drift(before["provenance"]["decision_id"])
    changed = {(d["prop"], d["frozen"], d["current"]) for d in drift}
    assert ("缴纳状态", "欠缴", "正常") not in changed, "沉淀后的节点值仍是欠缴，不该出现这种漂移"
    assert not any(prop == "缴纳状态" for prop, _, _ in changed), (
        f"记录值本来就没被改动，审计却报了漂移: {drift}"
    )

    # 洗白没成功，结论就不该松动
    again = tools.risk_decision("乙贸易有限公司")
    assert again["verdict"] == before["verdict"], "伪造记录改变了授信结论"
    assert again["confidence"] <= before["confidence"] + 1e-9


def test_whitelisted_rewrite_is_still_visible_to_a_later_decision(mutable_runtime: AgentRuntime):
    """就算走的是"系统自己改数据"的正规入口，改动也必须能被审计看见。

    与上一条是一对：上一条证明**来源文档**改不动既有值，这一条证明改动本身
    不会消失。上游数据修正在业务上确实需要，系统不能一律拒绝；它必须做到的是
    改动**留痕**——冻结快照与现状的差异可以被逐条问出来。否则"这次决策当时
    看到的是什么"就成了不可回答的问题。
    """
    tools = DomainTools(mutable_runtime)
    store = mutable_runtime.store

    decision = tools.risk_decision("乙贸易有限公司")
    assert decision["ok"]

    store.update_node_props("社保缴纳记录:SS-202601-003", {"缴纳状态": "正常"})
    drift = mutable_runtime.trail.detect_drift(decision["provenance"]["decision_id"])
    hit = [d for d in drift if d["prop"] == "缴纳状态"]
    assert hit, "上游记录被改成了正常缴纳，审计却查不出这次决策依据的变化"
    assert hit[0]["frozen"] == "欠缴" and hit[0]["current"] == "正常"


# ----------------------------------------------------------------------
# 二、身份盗用
# ----------------------------------------------------------------------


def test_forged_registration_cannot_steal_an_identity(mutable_runtime: AgentRuntime):
    """拿甲科技有限公司的信用代码去登记一家不存在的公司，不得改写真实主体。

    统一社会信用代码是实体归并的唯一依据，因此它也是最有价值的攻击目标：冒用
    别家的号，自己报送的材料就会并进那家企业的名下——注册资本、法定代表人、
    处罚记录全都算在对方头上，而图上看不出任何异常。

    这里要求的是**最保守**的结果：真实企业的名称与注册资本一个字节都不许变，
    冒用事实必须以冲突的形式留在案卷里。至于冒用者本身能不能进图，是业务流程
    该不该受理这份材料的问题，不是数据层该替业务方决定的事。
    """
    store = mutable_runtime.store
    before = store.find_by_prop("名称", "甲科技有限公司", ntype="企业")[0]
    name_before, capital_before = before["props"]["名称"], before["props"]["注册资本"]

    _load_json(mutable_runtime, _FORGED_IDENTITY, "伪造_工商登记.json")
    report = mutable_runtime.loader.consolidate().to_dict()

    after = store.find_by_prop("名称", "甲科技有限公司", ntype="企业")[0]
    assert after["props"]["名称"] == name_before, "冒用者改写了真实企业的名称"
    assert after["props"]["注册资本"] == capital_before, "冒用者改写了真实企业的注册资本"
    assert after["props"]["统一社会信用代码"] == "91310115MA1K3XYA01"

    assert report["conflict_count"] >= 1, "同一信用代码下出现两个名称，归并却没有报冲突"
    assert any(
        c["prop"] == "名称" and c["kept"] == "甲科技有限公司" and c["dropped"] == "丁咨询服务有限公司"
        for c in report["conflicts"]
    ), f"冲突内容不对: {report['conflicts']}"

    # 冒用者不得留下一个带着真实代码的独立节点
    impostors = [
        n
        for n in store.query_nodes(ntype="企业", limit=None)
        if n["props"].get("统一社会信用代码") == "91310115MA1K3XYA01" and n["props"]["名称"] != "甲科技有限公司"
    ]
    assert not impostors, f"图上留下了冒用同一信用代码的企业节点: {[n['id'] for n in impostors]}"
    assert store.integrity_report()["healthy"] is True


# ----------------------------------------------------------------------
# 三、归属投毒
# ----------------------------------------------------------------------


def test_field_order_cannot_shift_record_ownership(mutable_runtime: AgentRuntime):
    """字段顺序不得改变记录的归属。

    政务 JSON 的字段顺序由各统筹区接口自己定，"统一社会信用代码"排在"企业名称"
    前面是常见的一种。若归属在字段出现的那一刻就定死，这批元组只能拿到**上一条
    记录**留下的企业名，一份两家企业各一条记录的表就会整体错位一家：甲的代码与
    登记记录挂到上一家名下，乙的挂给甲。风控据此把 A 家的处罚算到 B 家头上。

    这是本文件里唯一一条"攻击者只需要合法提交材料"的用例——不需要伪造任何
    字段值，只需要决定字段的排列顺序。
    """
    store = mutable_runtime.store

    _load_json(
        mutable_runtime,
        [
            {
                "记录编号": "GS-2026-7101",
                "统一社会信用代码": "91310115MA1K3XYD04",
                "企业名称": "戊物流有限公司",
                "法定代表人": "李四",
                "注册资本": 300.0,
            },
            {
                "记录编号": "GS-2026-7102",
                "统一社会信用代码": "91310115MA1K3XYE05",
                "企业名称": "己仓储有限公司",
                "法定代表人": "王五",
                "注册资本": 400.0,
            },
        ],
        "顺序投毒_工商登记.json",
    )

    expected = {"GS-2026-7101": "戊物流有限公司", "GS-2026-7102": "己仓储有限公司"}
    for record_id, company in expected.items():
        record = _record_node(store, record_id)
        assert record is not None, f"记录 {record_id} 没有进图"
        assert _owner_of(store, record) == company, (
            f"记录 {record_id} 挂到了 {_owner_of(store, record)!r} 名下，应为 {company!r}"
        )

    # 信用代码必须落在自己的企业上，不能整体错位一家
    codes = {
        n["props"].get("名称"): n["props"].get("统一社会信用代码")
        for n in store.query_nodes(ntype="企业", limit=None)
    }
    assert codes["戊物流有限公司"] == "91310115MA1K3XYD04", f"信用代码错位: {codes}"
    assert codes["己仓储有限公司"] == "91310115MA1K3XYE05", f"信用代码错位: {codes}"


def test_multi_entity_document_never_guesses_a_document_level_owner(mutable_runtime: AgentRuntime):
    """记录里没写企业名时，多家企业的文档不得拿"最后出现的那家"去兜底。

    企业名只在文首出现一次、后面跟一长串记录的文档是存在的。这时文档级主体
    只在**本文档确实只讲过一家企业**时才成立；一旦文档里出现过两家以上，兜底
    就变成了猜测，而猜错的归属边会把一家的记录算到另一家头上。

    没有归属边的记录是看得见的缺陷，错挂一条归属边是看不见的缺陷。
    """
    store = mutable_runtime.store
    raw = json.dumps(
        [
            {"记录编号": "GS-2026-8101", "企业名称": "庚租赁有限公司", "注册资本": 100.0},
            {"记录编号": "GS-2026-8102", "注册资本": 200.0},  # 没有企业名
            {"记录编号": "GS-2026-8103", "企业名称": "辛劳务有限公司", "注册资本": 300.0},
        ],
        ensure_ascii=False,
    ).encode("utf-8")
    doc = parse_bytes(raw, source="多家无主_工商登记.json", suffix=".json")
    mutable_runtime.loader.load(mutable_runtime.extractor.extract(doc))

    orphan = _record_node(store, "GS-2026-8102")
    assert orphan is not None, "没有企业名的记录没有进图"
    owner = _owner_of(store, orphan)
    assert owner == "", f"文档里出现过两家企业，无主记录却被猜给了 {owner!r}"
    assert _owner_of(store, _record_node(store, "GS-2026-8103")) == "辛劳务有限公司"


# ----------------------------------------------------------------------
# 四、OCR 混淆纠正
# ----------------------------------------------------------------------


def _ocr_sidecar(tmp_path, text: str) -> str:
    """造一份带边车 OCR 文本的营业执照影像。"""
    source = _sample(os.path.join("gov", "营业执照_甲科技有限公司.png"))
    image = tmp_path / "伪造营业执照.png"
    shutil.copyfile(source, image)
    (tmp_path / "伪造营业执照.png.ocr.json").write_text(
        json.dumps({"text": text, "source": "攻击者自建 OCR 服务"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(image)


@pytest.mark.parametrize(
    "printed,expected",
    [
        # B 是 GB 32100 字符集里的合法字符，纠正表不许碰它
        ("91310115MA1K3XYB02", "91310115MA1K3XYB02"),
        ("91310115MA1K3XY802", "91310115MA1K3XY802"),
        # I / O / S / Z 不在字符集里，出现即 OCR 看错，纠正是对的
        ("91310115MA1K3XYI02", "91310115MA1K3XY102"),
        ("913l0115MA1K3XYB02", "91310115MA1K3XYB02"),
    ],
    ids=["含合法B", "含数字8", "非法I", "小写l"],
)
def test_ocr_correction_never_rewrites_a_legal_character(tmp_path, printed, expected):
    """OCR 纠正表只许纠正**不可能合法出现**的字符。

    统一社会信用代码的字符集是 0-9A-HJ-NPQRTUWXY，天然不含 I、O、S、V、Z，
    所以把这五个字母纠成数字是在修复 OCR。但 **B 是合法字符**：把 B 纠成 8
    不是修复，是伪造——91310115MA1K3XYB02 与 91310115MA1K3XY802 是两个不同
    的代码，后者看起来同样"合法"，于是它会安静地写进图里，与登记机关的原值
    冲突、把企业身份并到别的节点上，而没有任何异常可捕获。

    系统自己改坏一个字段值，比收到一个坏字段值更严重：后者至少能追到来源，
    前者连"谁改的"都答不上来。
    """
    doc = parse(_ocr_sidecar(tmp_path, f"统一社会信用代码: {printed}\n名称: 壬实业有限公司"))
    doc_text = "\n".join(b.text for b in doc.blocks if "信用代码" in b.text)
    assert expected in doc_text, f"信用代码被改成了别的值: {doc_text!r}"


# ----------------------------------------------------------------------
# 五、注入串
# ----------------------------------------------------------------------

_HOSTILE = "甲') DETACH DELETE n //"
_LITERAL = re.compile(r"'(?:\\.|[^'\\])*'")


def _strip_literals(cypher: str) -> str:
    """去掉所有 Cypher 字符串字面量。剩下的才是会被**执行**的部分。"""
    return _LITERAL.sub("''", cypher)


def _unquote(literal: str) -> str:
    return literal[1:-1].replace("\\'", "'").replace("\\\\", "\\")


def test_hostile_strings_are_contained_by_the_cypher_export():
    """节点 id、名称、属性里的注入串必须被关在字面量里，且原值可还原。

    导出的是 Neo4j 可直接执行的语句文本，任何一处没转义就是一个注入口。但把
    值"洗干净"同样是错的——节点名被静默改写，图上就多了一个谁也认不出来的
    实体。要求是两条：注入不生效，且值能一字不差地还原。
    """
    from govfin.graph.store import GraphStore
    from govfin.ontology.seed import build_seed_ontology

    store = GraphStore(in_memory=True, ontology=build_seed_ontology())
    hostile_props = {
        "名称": _HOSTILE,
        "统一社会信用代码": "91310115MA1K3XYF06",
        "经营范围": "'; MATCH (n) DETACH DELETE n; //​\x00" + "长" * 5000,
    }
    node_id = store.add_node("企业", label=_HOSTILE, props=hostile_props, node_id=_HOSTILE)
    other = store.add_node("企业", props={"名称": "正常企业", "统一社会信用代码": "91310115MA1K3XYG07"})
    store.add_edge(node_id, other, "参股", validate=False)

    cypher = store.to_cypher()
    executable = _strip_literals(cypher)

    assert "DETACH DELETE" not in executable, "注入串逃出了字符串字面量"
    assert "MATCH (n)" not in executable, "注入串逃出了字符串字面量"

    # 去掉字面量后，剩下的语句条数必须与图的实际规模一一对上。注入串若成功另起
    # 一条语句，这里就会多出来——这是"注入有没有生效"最直接的证据。
    assert executable.count("MERGE (n:GovFin:") == store.stats()["nodes"], "节点语句数不对"
    assert executable.count("MERGE (a)-[r:") == store.stats()["edges"], "关系语句数不对"
    assert store.stats()["nodes"] == 2 and store.integrity_report()["healthy"] is True

    # 值必须能原样还原——转义要可逆，不是把脏字符删掉
    literals = [_unquote(m.group(0)) for m in _LITERAL.finditer(cypher)]
    assert _HOSTILE in literals, "注入串被改写了，而不是被转义"
    assert hostile_props["经营范围"] in literals, "超长属性值没有原样导出"
    store.close()


# ----------------------------------------------------------------------
# 六、溯源洗白
# ----------------------------------------------------------------------


def test_injected_document_cannot_launder_provenance(mutable_runtime: AgentRuntime, tmp_path):
    """伪造成既有实体的来源文档，不得把原始出处顶掉。

    ``provenance["first_seen"]`` 回答的是"这个实体第一次是在哪份文件里出现的"，
    它是审计链的起点。一份后到的文档若能改写它，就能让整条证据链指向自己选的
    文件——之后无论怎么追，追到的都是攻击者准备好的那一份。

    这里走影像通道攻击真实存在的甲科技有限公司：它的注册资本与出身都来自那张
    营业执照，而营业执照正是最容易伪造的一类材料（一张重制的图 + 一份边车文本
    就够了）。要求是三条：first_seen 不动、伪造来源被追加而不是顶替、伪造的值
    进不了节点。
    """
    store = mutable_runtime.store
    target = store.find_by_prop("名称", "甲科技有限公司", ntype="企业")[0]
    first_before = dict(target["provenance"]["first_seen"])
    capital_before = target["props"]["注册资本"]
    assert first_before.get("document"), "样例企业应当已有出处，否则这条测试是空转"

    doc = parse(
        _ocr_sidecar(
            tmp_path,
            "统一社会信用代码: 91310115MA1K3XYA01\n"
            "名称: 甲科技有限公司\n"
            "注册资本: 99999万元",
        )
    )
    mutable_runtime.loader.load(mutable_runtime.extractor.extract(doc))

    after = store.find_by_prop("名称", "甲科技有限公司", ntype="企业")[0]
    assert after["provenance"]["first_seen"] == first_before, (
        "后到的文档改写了 first_seen，审计链的起点被顶掉了"
    )
    sources = after["provenance"].get("sources") or []
    assert any(s.get("document") == "伪造营业执照.png" for s in sources), (
        "伪造文档的字段进了节点，来源列表里却没有它——字段与来源对不上号"
    )
    assert after["props"]["注册资本"] == capital_before, "伪造营业执照改写了注册资本"
    forged = [c for c in _conflicts(after) if c["来源"].get("document") == "伪造营业执照.png"]
    assert any(c["字段"] == "注册资本" for c in forged), "注册资本被顶撞却没有留痕"
    assert store.integrity_report()["healthy"] is True


def test_provenance_truncation_is_counted_not_silent(mutable_runtime: AgentRuntime):
    """来源列表被截断时必须记数，不能让人把半份名单当成全部。

    一个被反复引用的主体（一家企业出现在十几期社保记录里）会攒出几十条来源，
    列表有上限是对的。但截断而不做声，与"从来就只有这些来源"在读的人眼里是
    同一个样子——审计会拿着半份名单当成全部名单，而它恰好就是用来回答"这个
    字段是谁给的"的那份名单。
    """
    store = mutable_runtime.store

    def _sources_of(node_id: str) -> tuple[list, int]:
        prov = store.get_node(node_id)["provenance"]
        listed = prov.get("sources") or []
        return listed, int(prov.get("sources_dropped") or 0)

    listed, dropped = _sources_of("企业:乙贸易有限公司")
    assert listed, "样例数据里这家企业应当有多个来源"
    overwritten, dropped_after = _sources_of("企业:乙贸易有限公司")
    assert len(overwritten) <= 20, "来源列表没有上限，热点节点会无限增长"
    assert dropped == dropped_after

    # 再来一份文档：列表已满，新来源只能挤掉最旧的一条，被挤掉的必须计数
    _load_json(
        mutable_runtime,
        [{"记录编号": "GS-2026-9401", "统一社会信用代码": "91310115MA1K3XYB02", "企业名称": "乙贸易有限公司"}],
        "溢出_工商登记.json",
    )
    after_listed, after_dropped = _sources_of("企业:乙贸易有限公司")
    assert len(after_listed) <= 20
    if len(listed) >= 20:
        assert after_dropped > dropped, "来源被截断却没有计数，读者会把半份名单当成全部"
    assert store.integrity_report()["healthy"] is True


class _ScriptedExtractionLlm:
    """按剧本产出实体的假 LLM，用来把属性送进**另一条通道**。

    规则路径的属性名受本体字段表约束（``_KV_FIELD_MAP``），取值也被 ``_coerce``
    归一过；LLM 给出的属性名与取值都是自由的——可能是本体外的字段名，也可能是
    个 int 而图上存的是 float。合并纪律必须对两条通道一视同仁，否则"这个字段
    能不能进图"就取决于它是哪条通道读来的，而那种差异在图上完全看不出来。
    """

    def __init__(self, *batches: list[dict]) -> None:
        self._batches = list(batches)
        self.calls = 0

    def complete_json(self, prompt, *, system=None):
        batch = self._batches[min(self.calls, len(self._batches) - 1)]
        self.calls += 1
        return {"entities": batch, "relations": []}


def _llm_document(source: str, *paragraphs: str) -> ParsedDocument:
    """一份只有正文段的文档，段落数量与 LLM 被调用的次数一一对应。

    正文刻意不含企业名后缀、编号、关系词，规则路径一条元组都产不出来，
    抽取器于是会把每一段都交给 LLM（见 ``Extractor.extract``）。
    """
    doc = ParsedDocument(source=source, modality=MODALITY_TEXT)
    for index, text in enumerate(paragraphs):
        doc.add("text", text, locator=f"para:{index}")
    return doc


def test_llm_attributes_obey_the_same_merge_rules_as_rule_attributes(mutable_runtime: AgentRuntime):
    """LLM 通道送来的属性，合并纪律必须与规则通道完全一致。

    合并逻辑在装载层只有一处，但有两条入口：一条是规则抽取的元组，一条是 LLM
    抽取的元组。若只在其中一条上做合并，后果是**顺序决定成败**——同一个字段，
    在"图上还没有这家企业"时能进图，在"图上是已有企业"时被悄悄丢掉。这类差异
    在图上不留任何痕迹，却让同一份材料在不同的导入顺序下得到不同的图。

    LLM 的属性名是自由的（不像规则路径受字段表约束），所以这里用一个本体外的
    字段名：它没有 datatype 声明，只能靠装载层的通用合并落地。
    """
    from govfin.ingest.extractor import Extractor

    rt = mutable_runtime
    target = "甲科技有限公司"
    llm = _ScriptedExtractionLlm(
        [{"text": target, "type": "企业", "confidence": 0.9, "attributes": {"税务评级": "A"}}],
        [{"text": target, "type": "企业", "confidence": 0.9, "attributes": {"税务评级": "D"}}],
    )
    extractor = Extractor(rt.ontology, llm=llm, use_llm=True)
    doc = _llm_document("LLM_税务评级.json", "该主体报告期内纳税情况见附表说明文字。", "该主体报告期内纳税情况另有补充说明文字。")
    assert doc.blocks[0].text and len(doc.blocks) == 2

    report = rt.loader.load(extractor.extract(doc)).to_dict()
    assert report["rejected_count"] == 0, report["rejected_sample"]

    node = rt.store.find_by_prop("名称", target, ntype="企业")[0]
    assert node["props"].get("税务评级") == "A", (
        f"LLM 给出的属性没有落地，装载层漏了这条通道: {node['props']}"
    )
    forged = [c for c in _conflicts(node) if c["字段"] == "税务评级"]
    assert forged, "同一字段两个说法，装载层却没有留痕"
    assert forged[0]["保留值"] == "A" and forged[0]["另一来源的值"] == "D"

    sources = [s.get("document") for s in (node["provenance"].get("sources") or [])]
    assert "LLM_税务评级.json" in sources, "LLM 通道的属性进了节点，来源列表里却没有它"


def test_equivalent_numbers_from_different_sources_do_not_raise_a_phantom_conflict(
    mutable_runtime: AgentRuntime,
):
    """同一个数换个写法不算冲突——冲突表一旦充满噪声，真正的那条就淹没了。

    上游送来的 ``5000`` 和 ``5000.0`` 是同一个事实，只是字面不同：JSON 接口给
    int，抽取归一给 float，各家 OCR 服务又各有各的写法。若逐字比字符串，正常
    重导就会在节点上堆出一串并不存在的"冲突"。冲突表是用来让人一眼看到"这份
    材料与已有记录对不上"的，它一旦开始报假警，读的人就会开始忽略它——留痕
    机制不会因为不记而失效，只会因为记了太多假的而失效。
    """
    from govfin.ingest.extractor import Extractor

    rt = mutable_runtime
    target = "甲科技有限公司"
    before = rt.store.find_by_prop("名称", target, ntype="企业")[0]
    capital = before["props"]["注册资本"]
    assert isinstance(capital, float), f"样例注册资本应当是 float，否则这条测试是空转: {capital!r}"

    llm = _ScriptedExtractionLlm(
        [{"text": target, "type": "企业", "confidence": 0.9, "attributes": {"注册资本": int(capital)}}]
    )
    extractor = Extractor(rt.ontology, llm=llm, use_llm=True)
    doc = _llm_document("LLM_注册资本.json", "该主体注册资本金额与登记簿所载数额一致。")

    rt.loader.load(extractor.extract(doc))

    after = rt.store.find_by_prop("名称", target, ntype="企业")[0]
    assert after["props"]["注册资本"] == capital
    assert not [c for c in _conflicts(after) if c["字段"] == "注册资本"], (
        f"同一个数的两种写法被当成了冲突: {_conflicts(after)}"
    )


def test_a_corrupted_document_cannot_take_down_the_batch(mutable_runtime: AgentRuntime):
    """单条畸形记录不得让整批导入失败——但也不许静默消失。

    政务文件是整批送达的，一份文件里混进一条坏记录（字段类型不对、必填缺失）
    是常态。让它把整批导入毒死，代价是当天全部数据都进不来；让它静默丢弃，
    代价是没人知道少了什么。两条都不行，所以要求是：坏记录进 rejected 清单，
    好记录照常入图。
    """
    rt = mutable_runtime
    before = rt.store.stats()["nodes"]
    raw = json.dumps(
        [
            {"记录编号": "GS-2026-9301", "企业名称": "癸机械有限公司", "注册资本": 500.0},
            {"记录编号": "GS-2026-9302", "企业名称": 12345, "注册资本": "不是数字"},
            {"记录编号": "GS-2026-9303", "企业名称": "子材料有限公司", "注册资本": 600.0},
        ],
        ensure_ascii=False,
    ).encode("utf-8")
    doc = parse_bytes(raw, source="含坏记录_工商登记.json", suffix=".json")
    report = rt.loader.load(rt.extractor.extract(doc)).to_dict()

    assert rt.store.stats()["nodes"] > before, "好记录也没能入图"
    for record_id in ("GS-2026-9301", "GS-2026-9303"):
        assert _record_node(rt.store, record_id) is not None, f"好记录 {record_id} 没能入图"
    assert rt.store.integrity_report()["healthy"] is True

    # 坏记录本身仍要留在图上：记录编号与其它字段都是可用的，缺的只是一个归属。
    # 整条记录连坐丢弃会把"有一条记录没挂上企业"变成"这天没有这条记录"。
    assert _record_node(rt.store, "GS-2026-9302") is not None, "坏记录整条消失了"

    # 脏值绝不能被当成企业名收下——那样会在图上凭空长出一个叫 "12345" 的主体，
    # 而之后没有任何环节会再回头看它像不像企业名。
    fabricated = [
        n
        for n in rt.store.query_nodes(ntype="企业", limit=None)
        if str(n["props"].get("名称") or n["label"]) == "12345"
    ]
    assert not fabricated, "畸形企业名在图上造出了一个假主体"

    # 而"丢了一个字段"这件事必须可查：报告说零条被拒、实际少了个字段，
    # "全部导入成功"和"少了一个字段"在报告上长得一模一样。
    assert report["rejected_count"] >= 1, f"畸形值被静默丢弃，报告里查不到: {report}"
    assert any(r.get("field") == "名称" for r in report["rejected_sample"]), report["rejected_sample"]
