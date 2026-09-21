"""抽取器：解析块 → 模态无关五元组。

策略是**规则优先 + LLM 兜底**，而不是无脑上大模型：

- 规则路径负责高精度识别结构化证据（18 位信用代码、条款编号、明确的标签行）。
  这部分必须确定性——同一份判决书跑两次得到不同的图谱是审计场景不能接受的。
- LLM 路径负责规则覆盖不到的表述（"因……被处以……罚款"这类自由文本），
  标注为 ``evidence_class=llm``，在置信度传播中承担更高惩罚。
- 两条路径都识别不出本体类型时，不丢弃、不猜测，而是落到 UNK 储备池。
  这正是低资源场景下载体演化的原料来源。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from govfin.errors import LLMError
from govfin.graph.schema import EVIDENCE_DIRECT, EVIDENCE_DERIVED, EVIDENCE_LLM
from govfin.ingest.multimodal import (
    UNK_ENTITY,
    UNK_RELATION,
    ExtractionResult,
    ModalityTuple,
    ParsedDocument,
    make_tuple,
)
from govfin.llm.base import LLMClient
from govfin.ontology.model import Ontology

# ---------------------------------------------------------------------------
# 实体识别模式
# ---------------------------------------------------------------------------

_USCC = re.compile(r"\b([0-9A-HJ-NPQRTUWXY]{18})\b")
_PERSON_NAME = re.compile(r"(?:法定代表人|实际控制人|负责人|经营者|股东|董事|原告|被告)[:：\s]*([一-龥]{2,4})(?![一-龥])")
_COMPANY = re.compile(r"([一-龥（）()A-Za-z0-9]{2,30}?(?:有限公司|股份有限公司|有限责任公司|合伙企业|个体工商户|集团))")

# 公司名前缀噪声：法律文书里实体名前面常挂角色/动词短语，
# 正则不知道边界，会把"被告"、"同时持有"一起吞进来。
_COMPANY_NOISE_PREFIXES = (
    "实际控制人", "法定代表人", "负责人", "经营者", "原告", "被告", "第三人",
    "同时持有", "名下另有", "该公司", "上述", "该公司为", "系", "另有", "持有",
    "被告为", "原告为", "公司名称", "企业名称", "名称", "称", "为", "由", "与",
    "及", "和", "等", "其", "该", "本",
)

# 含这些片段的名字是抽取噪声而非企业名
_COMPANY_NOISE_INFIX = ("控制人", "名下", "持有", "被告", "原告", "系上述", "法定代表人")
_CLAUSE_ID = re.compile(
    r"((?:[一-龥]{2,10})?"          # 发文机关前缀，如"银保监发"、"沪科创办"
    r"[〔\[(]\s*\d{4}\s*[〕\])]"    # 年份，全角/半角括号均可
    r"\s*\d+\s*号"                   # 文号序号
    r"(?:[-—－]?\s*[§第]?\s*[\d.]+\s*条?)?"  # 可选条号，形如 -§3.2 或 第3.2条
    r")"
)
_RECORD_ID = re.compile(r"\b((?:SS|GS|CF|SF|SW|JC|TZ)-\d{4}-?\d{2,4}-?\d{0,4})\b")
_DATE = re.compile(r"(\d{4})\s*[-年./]\s*(\d{1,2})\s*(?:[-月./]\s*(\d{1,2})\s*日?)?")
_AMOUNT = re.compile(r"([\d,]+(?:\.\d+)?)\s*(万元|元|亿元)")

# 记录编号前缀 → 政务记录子类
_RECORD_PREFIX_TYPES = {
    "SS": "社保缴纳记录",
    "GS": "工商登记",
    "CF": "行政处罚",
    "SF": "司法涉诉",
    "SW": "税务缴纳记录",
    "JC": "环保核查",
    "TZ": "财务事实",
}

# 政务记录子类 → 企业指向它的关系
_RECORD_OWNER_RELATION = {
    "社保缴纳记录": "缴纳社保",
    "工商登记": "拥有工商登记",
    "行政处罚": "受到处罚",
    "司法涉诉": "涉及诉讼",
    "税务缴纳记录": "申报税务",
    "环保核查": "遭受环保核查",
}

# 缴纳状态取值 → 是否构成风险信号
_RISK_STATUS = {"欠缴", "断缴", "未缴", "异常", "中止"}

# 政务记录类型 → 风险指标节点命名
_RISK_LABEL = {
    "社保缴纳记录": "社保缴纳异常",
    "行政处罚": "行政处罚风险",
    "司法涉诉": "司法涉诉风险",
    "税务缴纳记录": "税务异常",
    "环保核查": "环保合规风险",
    "工商登记": "工商登记异常",
}

# 标签 → (值类型, 规范字段名)
_KV_FIELD_MAP: dict[str, tuple[str, str]] = {
    "统一社会信用代码": ("uscc", "统一社会信用代码"),
    "注册号": ("text", "注册号"),
    "名称": ("company", "名称"),
    "企业名称": ("company", "名称"),
    "单位名称": ("company", "名称"),
    "法定代表人": ("person", "法定代表人"),
    "负责人": ("person", "法定代表人"),
    "实际控制人": ("person", "实际控制人"),
    "注册资本": ("amount", "注册资本"),
    "成立日期": ("date", "成立日期"),
    "缴纳月份": ("date", "缴纳月份"),
    "报告期": ("date", "报告期"),
    "缴纳基数": ("amount", "缴纳基数"),
    "实缴人数": ("int", "实缴人数"),
    "缴纳状态": ("text", "缴纳状态"),
    "处罚金额": ("amount", "处罚金额"),
    "处罚事由": ("text", "处罚事由"),
    "案由": ("text", "案由"),
    "涉案金额": ("amount", "涉案金额"),
    "营业收入": ("amount", "营业收入"),
    "净利润": ("amount", "净利润"),
    "资产负债率": ("ratio", "资产负债率"),
    "逾期次数": ("int", "逾期次数"),
    "负债总额": ("amount", "负债总额"),
    "条款编号": ("clause", "条款编号"),
    "条款原文": ("text", "条款原文"),
    "条款明确程度": ("ratio", "条款明确程度"),
    "适用对象": ("text", "适用对象"),
    "经营状态": ("text", "经营状态"),
    "行业": ("text", "行业"),
    "住所": ("text", "住所"),
    "经营范围": ("text", "经营范围"),
}

# 关系触发模式：(正则, 关系类型, 头实体组, 尾实体组)
_RELATION_PATTERNS: list[tuple[re.Pattern[str], str, str, str]] = [
    (re.compile(r"法定代表人\s*[:：]?\s*([一-龥]{2,4})"), "法定代表人", "__self_company__", "person"),
    (re.compile(r"实际控制人\s*[:：]?\s*([一-龥]{2,4})"), "实际控制人", "__self_company__", "person"),
    (re.compile(r"(?:欠缴|断缴|未缴)"), "触发风险信号", "__self_company__", "risk_signal"),
    (re.compile(r"被(?:处以|给予)?(?:罚款|行政处罚)"), "受到处罚", "__self_company__", "penalty"),
    (re.compile(r"(?:涉诉|被起诉|作为被告)"), "涉及诉讼", "__self_company__", "litigation"),
    (re.compile(r"缴纳社保|参保"), "缴纳社保", "__self_company__", "social_security"),
]


@dataclass
class ExtractionStats:
    blocks_seen: int = 0
    blocks_by_rule: int = 0
    blocks_by_llm: int = 0
    llm_failures: int = 0
    unk_entities: int = 0
    unk_relations: int = 0
    rejected: int = 0
    by_modality: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "blocks_seen": self.blocks_seen,
            "blocks_by_rule": self.blocks_by_rule,
            "blocks_by_llm": self.blocks_by_llm,
            "llm_failures": self.llm_failures,
            "unk_entities": self.unk_entities,
            "unk_relations": self.unk_relations,
            "rejected": self.rejected,
            "by_modality": self.by_modality,
        }


class Extractor:
    """规则 + LLM 混合抽取器。"""

    def __init__(
        self,
        ontology: Ontology,
        *,
        llm: LLMClient | None = None,
        use_llm: bool = True,
        min_rule_confidence: float = 0.6,
    ) -> None:
        self.ontology = ontology
        self.llm = llm
        self.use_llm = use_llm and llm is not None
        self.min_rule_confidence = min_rule_confidence

    # ------------------------------------------------------------------

    def extract(self, doc: ParsedDocument) -> ExtractionResult:
        result = ExtractionResult()
        stats = ExtractionStats()
        # 文档级上下文：标签行里的"企业名称"要绑定到本文档的主体企业上。
        # ``companies`` 记录本文档出现过的**全部**企业名，用于判断文档级兜底是
        # 不是成立（见 _backfill_owner_links）。
        context: dict = {"company": None, "uscc": None, "doc": doc.source, "companies": []}

        for block in doc.blocks:
            stats.blocks_seen += 1
            stats.by_modality[block.kind] = stats.by_modality.get(block.kind, 0) + 1
            before = len(result.tuples)
            self._extract_block(doc, block, context, result)
            gained = len(result.tuples) - before
            if gained:
                stats.blocks_by_rule += 1
            elif self.use_llm and block.kind in ("text", "kv") and len(block.text) >= 12:
                if self._extract_with_llm(doc, block, context, result):
                    stats.blocks_by_llm += 1
                else:
                    stats.llm_failures += 1

        self._flush_record(context, result)
        self._backfill_owner_links(context, result)

        stats.unk_entities = len(result.unk_entities)
        stats.unk_relations = len(result.unk_relations)
        result.stats = stats.to_dict()
        return result

    @staticmethod
    def _flush_record(context: dict, result: ExtractionResult) -> None:
        """记录结束，把攒着的元组按本记录的企业主体落定。

        政务 JSON 的字段顺序由各统筹区接口自己定，而"记录编号 / 统一社会信用
        代码"常常排在"企业名称"前面。字段一出现就落元组，这两条元组的归属就
        只能是**上一条记录**留下的那个企业名，于是整表错位一家：一份三家企业
        各一条记录的工商登记表会变成"甲的代码挂到上一家、乙的挂给甲、丙的挂给
        乙"；十期社保记录里换过主体的那几期也会挂到上一家名下。

        这类错误在图上不留任何痕迹——每家企业都还是孤零零一个节点，字段却
        是别人家的——而风险传导恰恰全靠这些边。因此归属不能定死在字段出现的
        那一刻，要等本记录的企业名称出现（或记录结束）再落定。
        """
        current = context.get("current_entity")
        if not current:
            return
        owner = current.get("owner")
        for build in current.get("pending") or []:
            result.tuples.append(build(owner))
        current["pending"] = []

    @staticmethod
    def _emit_owned_tuple(
        context: dict, result: ExtractionResult, build, *, fallback: str | None = None
    ) -> None:
        """落一条需要企业主体的元组：主体已定就直接落，未定就挂起。

        ``build`` 接一个主体名，返回元组。挂起的在 ``_flush_record`` 里统一落定。
        """
        current = context.get("current_entity")
        if current is not None:
            if _RECORD_OWNER_RELATION.get(current["type"]) is None:
                # 条款这类记录的主体就是它自己，不存在企业归属
                result.tuples.append(build(None))
                return
            if not current.get("owner"):
                current["pending"].append(build)
                return
            result.tuples.append(build(current["owner"]))
            return
        result.tuples.append(build(fallback or context.get("company")))

    @staticmethod
    def _open_record(context: dict, entity_id: str, etype: str, pending: list | None = None) -> None:
        """开一条记录（政务记录或监管条款），后续字段归属到它。

        记录主体和政务记录一样要显式留位：``owner`` 未知时先挂空，等本记录内
        出现企业名再落定（见 ``_flush_record``）。条款记录永远没有企业主体，
        因此也永远不需要落定。
        """
        context["current_entity"] = {
            "id": entity_id,
            "type": etype,
            "month": None,
            "owner": None,
            "pending": list(pending or []),
        }

    @staticmethod
    def _backfill_owner_links(context: dict, result: ExtractionResult) -> None:
        """把政务记录挂回企业主体（只在文档只描述过一个企业时才敢兜底）。

        本记录内有企业名的，归属已在 ``_flush_record`` 里定好，这里不管。剩下的
        是整条记录都没出现企业名的那种文档（企业名只在文首出现一次，后面跟了
        一长串记录）。这时**文档级**的主体是唯一答案——但只在本文档确实只讲过
        一家企业时才成立。

        多家企业的文档里不兜底。"最后出现的那家企业"不是答案，只是一个猜错的
        答案；而错挂的归属边会把甲家的处罚算到乙家头上，风控据此收紧或放松
        额度，全程没有任何异常。留一个没有归属边的记录是**看得见**的缺陷，
        错挂一条归属边是看不见的缺陷，两者之间没有可犹豫的余地。
        """
        companies = context.get("companies") or []
        owner = companies[0] if len(companies) == 1 else None
        if not owner:
            return
        for tup in result.tuples:
            if tup.relation_candidate is not None:
                continue
            relation = _RECORD_OWNER_RELATION.get(tup.entity_type)
            if relation is None:
                continue
            tup.relation_candidate = owner
            tup.relation_target = tup.entity_mention
            tup.relation_type = relation

    # ------------------------------------------------------------------
    # 规则路径
    # ------------------------------------------------------------------

    def _extract_block(self, doc: ParsedDocument, block, context: dict, result: ExtractionResult) -> None:
        text = block.text
        if not text.strip():
            return

        if block.kind == "kv":
            self._extract_kv(doc, block, context, result, text)
        elif block.kind == "table":
            self._extract_table(doc, block, context, result)
        elif block.kind == "text":
            self._extract_free_text(doc, block, context, result, text)
        # 其它块类型（如 JSON 解析器产出的 record 头块）只作为结构化载体，
        # 不参与抽取——它们的字段已经被拆成独立 kv 块了

    def _extract_kv(self, doc, block, context, result, text: str) -> None:
        if "\n" in text.strip():
            return  # 多行块不是单字段行，交由 text 路径处理
        label, sep, value = text.partition(":")
        label, value = label.strip(), value.strip()
        if not sep or not value:
            return

        if label == "记录编号":
            self._extract_record_id(doc, block, context, result, value)
            return

        if label == "映射风险维度":
            self._extract_dimension(doc, block, context, result, value)
            return

        if label == "条款编号":
            clause_id = _coerce(value, "clause") or value
            clause_type = _clause_type(clause_id, block.text, context)
            self._open_record(context, clause_id, clause_type)
            context["current_clause"] = clause_id
            result.tuples.append(
                make_tuple(
                    entity_mention=clause_id,
                    entity_type=clause_type,
                    evidence_snippet=block.text,
                    modality=doc.modality,
                    source_document=doc.source,
                    source_locator=block.locator,
                    confidence=0.95,
                    attributes={"条款编号": clause_id},
                )
            )
            return

        spec = _KV_FIELD_MAP.get(label)
        if spec is None:
            return
        value_kind, canonical = spec
        typed = _coerce(value, value_kind)
        if typed is None:
            # 丢掉这个值是对的（见 _coerce），但不能丢得无声无息：报告里写着
            # "零条被拒"而实际少了个字段，读报告的人会以为数据是全的。
            result.rejected.append(
                {
                    "field": canonical,
                    "value": value,
                    "reason": f"值不符合 {value_kind} 类型",
                    "document": doc.source,
                    "locator": block.locator,
                }
            )
            return

        if canonical == "统一社会信用代码":
            context["uscc"] = typed

            def build(owner: str | None) -> ModalityTuple:
                return make_tuple(
                    entity_mention=owner,
                    entity_type="企业",
                    relation_candidate=owner,
                    relation_target=typed,
                    relation_type="拥有工商登记",
                    evidence_snippet=block.text,
                    modality=doc.modality,
                    source_document=doc.source,
                    source_locator=block.locator,
                    confidence=0.99,
                    attributes={"统一社会信用代码": typed},
                )

            current = context.get("current_entity")
            if current is not None and current.get("owner") is None:
                # 本记录的企业名还没读到，这条代码归谁待定（见 _flush_record）
                current["pending"].append(build)
                return
            company = (current or {}).get("owner") or context.get("company")
            company = company or _company_name_from_context(doc)
            if company:
                context["company"] = company
                result.tuples.append(build(company))
            return

        if canonical == "名称":
            context["company"] = typed
            if typed not in context["companies"]:
                context["companies"].append(typed)
            current = context.get("current_entity")
            if current is not None and current.get("owner") is None:
                # 本记录的企业名出现了，攒着的元组归属已确定
                current["owner"] = typed
                self._flush_record(context, result)
            result.tuples.append(self._entity_tuple(doc, block, typed, "企业", 0.95))
            return

        if canonical in ("法定代表人", "实际控制人"):
            self._emit_owned_tuple(
                context,
                result,
                lambda owner: make_tuple(
                    entity_mention=typed,
                    entity_type="自然人",
                    relation_candidate=owner,
                    relation_target=typed,
                    relation_type=canonical,
                    evidence_snippet=block.text,
                    modality=doc.modality,
                    source_document=doc.source,
                    source_locator=block.locator,
                    confidence=0.95,
                    attributes={"姓名": typed},
                ),
                fallback=_company_name_from_context(doc),
            )
            return

        current = context.get("current_entity")
        if current is not None:
            # 政务记录/条款的属性挂在**自身的节点**上，而不是企业节点上。
            # 挂错层级会让"2026-03 那期社保断缴"退化成"这家企业断缴过"，
            # 时间维度一丢，跨文档推理的时序论据就没了。
            self._emit_owned_tuple(
                context,
                result,
                lambda owner: make_tuple(
                    entity_mention=current["id"],
                    entity_type=current["type"],
                    relation_candidate=owner,
                    relation_target=current["id"],
                    relation_type=_RECORD_OWNER_RELATION.get(current["type"]),
                    evidence_snippet=block.text,
                    modality=doc.modality,
                    source_document=doc.source,
                    source_locator=block.locator,
                    confidence=0.93,
                    attributes={canonical: typed},
                ),
            )
            if canonical == "缴纳月份":
                current["month"] = str(typed)
            if canonical == "缴纳状态" and str(typed) in _RISK_STATUS:
                self._emit_risk_signal(doc, block, context, result, current, str(typed))
            return

        owner = context.get("company")
        if owner:
            result.tuples.append(
                make_tuple(
                    entity_mention=owner,
                    entity_type="企业",
                    relation_candidate=owner,
                    relation_target=f"{canonical}={typed}",
                    relation_type=_attr_relation(canonical),
                    evidence_snippet=block.text,
                    modality=doc.modality,
                    source_document=doc.source,
                    source_locator=block.locator,
                    confidence=0.9,
                    attributes={canonical: typed},
                )
            )

    def _emit_risk_signal(self, doc, block, context, result, record: dict, status: str) -> None:
        """政务记录 → 风险指标。这是跨域推理的接缝：政务侧的"欠缴"必须显式
        变成金融侧可消费的风险指标节点，否则两个域在图上永远连不起来。"""
        month = record.get("month")
        label = f"{_RISK_LABEL.get(record['type'], '政务异常')}({month})" if month else _RISK_LABEL.get(record["type"], "政务异常")
        result.tuples.append(
            make_tuple(
                entity_mention=label,
                entity_type="风险指标",
                relation_candidate=record["id"],
                relation_target=label,
                relation_type="触发风险信号",
                evidence_snippet=f"{block.text}（判定为风险状态：{status}）",
                modality=doc.modality,
                source_document=doc.source,
                source_locator=block.locator,
                confidence=0.9,
                evidence_class=EVIDENCE_DERIVED,
                attributes={"指标名称": label, "指标值": 0.2},
                raw={"risk_status": status},
            )
        )

    def _extract_record_id(self, doc, block, context, result, record_id: str) -> None:
        """记录编号是政务数据的"主键"，按前缀判定它属于哪类政务记录。"""
        prefix = record_id.split("-")[0].upper()
        rec_type = _RECORD_PREFIX_TYPES.get(prefix)
        if rec_type is None:
            self._capture_unk_entity(
                result, doc, block, record_id, f"无法归类的记录编号前缀 '{prefix}'"
            )
            return
        # 上一条记录到此为止，它攒下的待定归属元组在这里落定
        self._flush_record(context, result)
        relation = _RECORD_OWNER_RELATION.get(rec_type)

        def build(owner: str | None) -> ModalityTuple:
            return make_tuple(
                entity_mention=record_id,
                entity_type=rec_type,
                relation_candidate=owner,
                relation_target=record_id,
                relation_type=relation,
                evidence_snippet=block.text,
                modality=doc.modality,
                source_document=doc.source,
                source_locator=block.locator,
                confidence=0.94,
                attributes={"记录编号": record_id},
            )

        # 归属先留空：本记录的企业名称排在字段表的哪一格由各统筹区接口自己定，
        # 此刻能拿到的只有**上一条记录**的企业名（见 _flush_record）。
        self._open_record(context, record_id, rec_type, pending=[build])

    def _extract_dimension(self, doc, block, context, result, dimension: str) -> None:
        """条款 → 风险维度。Layer2 内部的结构化，让规则条款变成可被路径穿越的节点。"""
        clause = context.get("current_clause")
        result.tuples.append(
            make_tuple(
                entity_mention=dimension,
                entity_type="风险维度",
                relation_candidate=clause,
                relation_target=dimension,
                relation_type="映射维度" if clause else None,
                evidence_snippet=block.text,
                modality=doc.modality,
                source_document=doc.source,
                source_locator=block.locator,
                confidence=0.86,
                evidence_class=EVIDENCE_DERIVED,
                attributes={"维度名称": dimension, "维度权重": 0.3},
            )
        )

    def _extract_table(self, doc, block, context, result) -> None:
        rows = [r for r in (block.text or "").splitlines() if r.strip()]
        header = block.metadata.get("header") or [c.strip() for c in (rows[0] if rows else "").split("|")]
        header = [h for h in header if h]
        if not header:
            return
        owner = context.get("company")
        for row_index, row in enumerate(rows):
            cells = [c.strip() for c in row.split("|")]
            pairs = dict(zip(header, cells))
            if not pairs:
                continue
            # 表内出现的公司名视为新实体（如关联方表、担保明细表）
            for cell in cells:
                company_match = _COMPANY.search(cell)
                mentioned = clean_company_name(company_match.group(1)) if company_match else None
                if mentioned and mentioned != owner:
                    result.tuples.append(
                        make_tuple(
                            entity_mention=mentioned,
                            entity_type="企业",
                            relation_candidate=owner,
                            relation_target=mentioned,
                            relation_type="企业参股" if "参股" in header[0] else None,
                            evidence_snippet=row,
                            modality=doc.modality,
                            source_document=doc.source,
                            source_locator=f"{block.locator}/row:{row_index}",
                            confidence=0.85,
                            attributes={"名称": mentioned},
                        )
                    )
            if owner:
                for key, value in pairs.items():
                    spec = _KV_FIELD_MAP.get(key)
                    if spec is None or not value:
                        continue
                    typed = _coerce(value, spec[0])
                    if typed is None:
                        continue
                    result.tuples.append(
                        make_tuple(
                            entity_mention=owner,
                            entity_type="企业",
                            relation_candidate=owner,
                            relation_target=f"{spec[1]}={typed}",
                            relation_type=_attr_relation(spec[1]),
                            evidence_snippet=f"{key}: {value}",
                            modality=doc.modality,
                            source_document=doc.source,
                            source_locator=f"{block.locator}/row:{row_index}",
                            confidence=0.88,
                            attributes={spec[1]: typed},
                        )
                    )

    def _extract_free_text(self, doc, block, context, result, text: str) -> None:
        found_any = False

        for match in _USCC.finditer(text):
            code = match.group(1)
            context.setdefault("uscc", code)
            owner = context.get("company")
            if owner:
                # 已知主体时，信用代码是它的**属性**而不是一个新实体。
                # 之前生成"统一社会信用代码 XXX"节点会让同一家企业在图上分裂成两个。
                result.tuples.append(
                    make_tuple(
                        entity_mention=owner,
                        entity_type="企业",
                        relation_candidate=owner,
                        relation_target=f"统一社会信用代码={code}",
                        relation_type="拥有工商登记",
                        evidence_snippet=_window(text, match.start(), match.end()),
                        modality=doc.modality,
                        source_document=doc.source,
                        source_locator=block.locator,
                        confidence=0.97,
                        attributes={"统一社会信用代码": code},
                    )
                )
            else:
                result.tuples.append(
                    make_tuple(
                        entity_mention=code,
                        entity_type="企业",
                        evidence_snippet=_window(text, match.start(), match.end()),
                        modality=doc.modality,
                        source_document=doc.source,
                        source_locator=block.locator,
                        confidence=0.97,
                        attributes={"统一社会信用代码": code},
                    )
                )
            found_any = True

        for match in _CLAUSE_ID.finditer(text):
            clause = match.group(1).strip()
            cls_name = "授信指引条款" if "银保监" in clause or "指引" in text[:120] else "监管条款"
            result.tuples.append(
                make_tuple(
                    entity_mention=clause,
                    entity_type=cls_name,
                    relation_candidate=None,
                    relation_target=None,
                    relation_type=None,
                    evidence_snippet=_window(text, match.start(), match.end()),
                    modality=doc.modality,
                    source_document=doc.source,
                    source_locator=block.locator,
                    confidence=0.9,
                    attributes={"条款编号": clause},
                )
            )
            found_any = True

        for match in _RECORD_ID.finditer(text):
            rid = match.group(1)
            prefix = rid.split("-")[0]
            rec_type = _RECORD_PREFIX_TYPES.get(prefix)
            if rec_type is None:
                self._capture_unk_entity(
                    result, doc, block, rid, f"无法归类记录编号前缀 '{prefix}'"
                )
                continue
            owner = context.get("company")
            result.tuples.append(
                make_tuple(
                    entity_mention=rid,
                    entity_type=rec_type,
                    relation_candidate=owner,
                    relation_target=rid,
                    relation_type="缴纳社保" if rec_type == "社保缴纳记录" else None,
                    evidence_snippet=_window(text, match.start(), match.end()),
                    modality=doc.modality,
                    source_document=doc.source,
                    source_locator=block.locator,
                    confidence=0.92,
                    attributes={"记录编号": rid},
                )
            )
            found_any = True

        for match in _COMPANY.finditer(text):
            name = clean_company_name(match.group(1))
            if name is None:
                continue
            if name == context.get("company"):
                continue
            context.setdefault("company", name)
            result.tuples.append(
                make_tuple(
                    entity_mention=name,
                    entity_type="企业",
                    relation_candidate=None,
                    relation_target=None,
                    relation_type=None,
                    evidence_snippet=_window(text, match.start(), match.end()),
                    modality=doc.modality,
                    source_document=doc.source,
                    source_locator=block.locator,
                    confidence=0.88,
                    attributes={"名称": name},
                )
            )
            found_any = True

        for pattern, relation_type, head_token, tail_kind in _RELATION_PATTERNS:
            for match in pattern.finditer(text):
                owner = context.get("company")
                if relation_type in ("法定代表人", "实际控制人"):
                    person = match.group(1)
                    subject = owner or _company_name_from_context(doc)
                    if not subject:
                        continue
                    result.tuples.append(
                        make_tuple(
                            entity_mention=person,
                            entity_type="自然人",
                            relation_candidate=subject,
                            relation_target=person,
                            relation_type=relation_type,
                            evidence_snippet=_window(text, match.start(), match.end()),
                            modality=doc.modality,
                            source_document=doc.source,
                            source_locator=block.locator,
                            confidence=0.93,
                            attributes={"姓名": person},
                        )
                    )
                    found_any = True
                else:
                    if not owner:
                        # 有风险信号但不知道主体是谁——本体覆盖不到这个表述，进储备池
                        self._capture_unk_relation(
                            result, doc, block, match.group(0), text, f"关系 '{relation_type}' 缺少主体绑定"
                        )
                        continue
                    result.tuples.append(
                        make_tuple(
                            entity_mention=match.group(0),
                            entity_type="风险指标",
                            relation_candidate=owner,
                            relation_target=match.group(0),
                            relation_type=relation_type,
                            evidence_snippet=_window(text, match.start(), match.end()),
                            modality=doc.modality,
                            source_document=doc.source,
                            source_locator=block.locator,
                            confidence=0.8,
                            evidence_class=EVIDENCE_DERIVED,
                            attributes={"指标名称": relation_type},
                        )
                    )
                    found_any = True

        if not found_any and _looks_like_unknown_term(text):
            self._capture_unk_entity(result, doc, block, _unknown_term(text), "规则未覆盖的表述")

    # ------------------------------------------------------------------
    # LLM 路径
    # ------------------------------------------------------------------

    def _extract_with_llm(self, doc, block, context, result: ExtractionResult) -> bool:
        assert self.llm is not None
        prompt = _build_extraction_prompt(block.text, self.ontology, context)
        try:
            payload = self.llm.complete_json(prompt, system=_EXTRACTION_SYSTEM)
        except LLMError:
            return False

        entities = payload.get("entities") if isinstance(payload, dict) else None
        relations = payload.get("relations") if isinstance(payload, dict) else None
        if not isinstance(entities, list):
            return False

        produced = 0
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            mention = str(ent.get("text", "")).strip()
            etype = str(ent.get("type", "")).strip()
            if not mention:
                continue
            resolved = self._resolve_type(etype, mention)
            if resolved is None:
                self._capture_unk_entity(result, doc, block, mention, f"LLM 给出未定义类型 '{etype}'")
                continue
            confidence = _safe_float(ent.get("confidence"), 0.75)
            if confidence < self.min_rule_confidence:
                result.rejected.append(
                    {"mention": mention, "type": resolved, "reason": f"置信度 {confidence} 低于阈值"}
                )
                continue
            result.tuples.append(
                make_tuple(
                    entity_mention=mention,
                    entity_type=resolved,
                    relation_candidate=None,
                    relation_target=None,
                    relation_type=None,
                    evidence_snippet=block.text,
                    modality=doc.modality,
                    source_document=doc.source,
                    source_locator=block.locator,
                    confidence=confidence,
                    evidence_class=EVIDENCE_LLM,
                    attributes=ent.get("attributes") or {},
                    raw={"llm_type": etype},
                )
            )
            produced += 1

        for rel in relations if isinstance(relations, list) else []:
            if not isinstance(rel, dict):
                continue
            head = str(rel.get("head", "")).strip()
            tail = str(rel.get("tail", "")).strip()
            rtype = str(rel.get("type", "")).strip()
            if not head or not tail:
                continue
            resolved_rel = self.ontology.properties.get(rtype)
            if resolved_rel is None:
                self._capture_unk_relation(result, doc, block, rtype, block.text, "LLM 给出未定义关系类型")
                continue
            confidence = _safe_float(rel.get("confidence"), 0.7)
            result.tuples.append(
                make_tuple(
                    entity_mention=head,
                    entity_type=self.ontology.get_class(resolved_rel.domain).name if self.ontology.get_class(resolved_rel.domain) else UNK_ENTITY,
                    relation_candidate=head,
                    relation_target=tail,
                    relation_type=rtype,
                    evidence_snippet=block.text,
                    modality=doc.modality,
                    source_document=doc.source,
                    source_locator=block.locator,
                    confidence=confidence,
                    evidence_class=EVIDENCE_LLM,
                    raw={"llm_relation": rtype},
                )
            )
            produced += 1

        return produced > 0

    # ------------------------------------------------------------------
    # UNK 捕获
    # ------------------------------------------------------------------

    def _capture_unk_entity(self, result: ExtractionResult, doc, block, mention: str, reason: str) -> None:
        result.unk_entities.append(
            make_tuple(
                entity_mention=mention,
                entity_type=UNK_ENTITY,
                relation_candidate=None,
                relation_target=None,
                relation_type=None,
                evidence_snippet=block.text,
                modality=doc.modality,
                source_document=doc.source,
                source_locator=block.locator,
                confidence=0.5,
                evidence_class=EVIDENCE_DIRECT,
                raw={"reason": reason},
            )
        )

    def _capture_unk_relation(
        self, result: ExtractionResult, doc, block, mention: str, context_text: str, reason: str
    ) -> None:
        result.unk_relations.append(
            make_tuple(
                entity_mention=mention,
                entity_type=UNK_ENTITY,
                relation_candidate=mention,
                relation_target=None,
                relation_type=UNK_RELATION,
                evidence_snippet=context_text,
                modality=doc.modality,
                source_document=doc.source,
                source_locator=block.locator,
                confidence=0.45,
                evidence_class=EVIDENCE_LLM,
                raw={"reason": reason},
            )
        )

    # ------------------------------------------------------------------

    def _entity_tuple(self, doc, block, mention: str, etype: str, confidence: float) -> ModalityTuple:
        return make_tuple(
            entity_mention=mention,
            entity_type=etype,
            relation_candidate=None,
            relation_target=None,
            relation_type=None,
            evidence_snippet=block.text,
            modality=doc.modality,
            source_document=doc.source,
            source_locator=block.locator,
            confidence=confidence,
            attributes={"名称": mention} if etype == "企业" else {},
        )

    def _resolve_type(self, claimed: str, mention: str) -> str | None:
        """把 LLM 给的类型名对齐到本体。对齐不上就返回 None 走 UNK。"""
        if not claimed:
            return _guess_type_by_shape(mention, self.ontology)
        resolved = self.ontology.resolve_alias(claimed)
        if resolved:
            return resolved
        return _guess_type_by_shape(mention, self.ontology)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def clean_company_name(raw: str) -> str | None:
    """剥掉公司名前误吞的角色/动词前缀，返回可信的企业名或 None。

    宁可返回 None 走 UNK，也不要造出"被告丙建材有限公司"这种节点——
    它会在图上与真实的"丙建材有限公司"分裂成两个实体，跨文档推理直接断链。
    """
    name = (raw or "").strip().strip("、，。；：,.;:")
    if not name:
        return None

    changed = True
    while changed:
        changed = False
        for prefix in _COMPANY_NOISE_PREFIXES:
            if name.startswith(prefix) and len(name) > len(prefix) + 1:
                name = name[len(prefix) :]
                changed = True
                break

    if any(bad in name for bad in _COMPANY_NOISE_INFIX):
        return None
    if len(name) < 4 or len(name) > 24:
        return None
    if not name.endswith(("有限公司", "股份有限公司", "有限责任公司", "合伙企业", "个体工商户", "集团")):
        return None
    return name


def _clause_type(clause_id: str, text: str, context: dict) -> str:
    """按发文机关与上下文判定条款子类，决定它挂在 Layer2 的哪个分支下。"""
    if any(k in clause_id for k in ("银保监", "人民银行", "银发")):
        return "授信指引条款"
    if any(k in clause_id for k in ("数据", "共享", "规范")):
        return "数据规范"
    if any(k in clause_id for k in ("科创办", "发改委", "工信", "财政")):
        return "政策条款"
    if "LPR" in text or "利率" in text:
        return "定价规则条款"
    return "监管条款"


def _coerce(value: str, kind: str):
    v = (value or "").strip()
    if not v:
        return None
    if kind == "uscc":
        cleaned = v.replace(" ", "").upper()
        m = _USCC.search(cleaned)
        return m.group(0) if m else None
    if kind == "int":
        m = re.search(r"-?\d+", v.replace(",", ""))
        return int(m.group(0)) if m else None
    if kind == "amount":
        m = re.search(r"([\d,]+(?:\.\d+)?)", v)
        if not m:
            return None
        amount = float(m.group(1).replace(",", ""))
        if "亿" in v:
            amount *= 10000
        return round(amount, 4)
    if kind == "ratio":
        m = re.search(r"([\d.]+)\s*%?", v)
        if not m:
            return None
        ratio = float(m.group(1))
        return round(ratio / 100.0, 6) if ratio > 1 else ratio
    if kind == "date":
        m = _DATE.search(v)
        return _normalize_date(m) if m else None
    if kind == "company":
        m = _COMPANY.search(v)
        # 匹配不上就说明这根本不是企业名，返回 None 而不是原样收下。
        # "企业名称: 12345" 这类脏值若照收，图上就会长出一个名字叫 "12345" 的
        # 企业节点——而节点一旦存在，关联方匹配、风险传导、授信额度都会把它
        # 当成一个真实主体来对待，没有任何环节会再回头看它像不像企业名。
        # 少一个字段是看得见的缺陷，多一个假主体是看不见的缺陷。
        return m.group(1) if m else None
    if kind == "person":
        m = re.search(r"[一-龥]{2,4}", v)
        return m.group(0) if m else None
    if kind == "clause":
        m = _CLAUSE_ID.search(v)
        return m.group(1) if m else v
    return v


def _normalize_date(match: re.Match) -> str | None:
    if match is None:
        return None
    year, month, day = match.group(1), match.group(2), match.group(3)
    if not year or not month:
        return None
    try:
        y, mo = int(year), int(month)
    except (TypeError, ValueError):
        return None
    if not (1 <= mo <= 12) or not (1900 <= y <= 2200):
        return None
    if day:
        try:
            d = int(day)
        except (TypeError, ValueError):
            return None
        if not 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}"
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return f"{y:04d}-{mo:02d}"


def _window(text: str, start: int, end: int, radius: int = 40) -> str:
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    return " ".join(text[lo:hi].split())


def _company_name_from_context(doc: ParsedDocument) -> str | None:
    for block in doc.blocks:
        m = _COMPANY.search(block.text or "")
        if m:
            return m.group(1)
    return None


def _attr_relation(field: str) -> str:
    return "拥有工商登记" if field in ("注册资本", "成立日期", "经营状态", "行业", "住所", "经营范围") else "触发风险信号"


_TERM_HINTS = ("情况说明", "补充材料", "附注", "备注", "说明")


def _looks_like_unknown_term(text: str) -> bool:
    stripped = text.strip()
    return 2 <= len(stripped) <= 60 and not any(h in stripped for h in _TERM_HINTS)


def _unknown_term(text: str) -> str:
    return " ".join(text.split())[:60]


def _guess_type_by_shape(mention: str, ontology: Ontology) -> str | None:
    """按字符串形态猜类型。这是低资源场景下最实用的兜底：即使没有模型，
    18 位信用代码一定是企业，带"有限公司"后缀的一定是企业。"""
    m = mention.strip()
    if _USCC.fullmatch(m):
        return "企业" if "企业" in ontology.classes else None
    if _COMPANY.fullmatch(m):
        return "企业"
    if _CLAUSE_ID.fullmatch(m):
        return "监管条款"
    prefix = m.split("-")[0] if "-" in m else ""
    if prefix in _RECORD_PREFIX_TYPES:
        return _RECORD_PREFIX_TYPES[prefix]
    if re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", m):
        return "风险指标"
    return None


_EXTRACTION_SYSTEM = (
    "你是政务与金融领域的知识抽取引擎。只输出 JSON，不要任何解释文字。"
    "严格遵守：只抽取文本中明确出现的事实，不要推测、不要补全、不要引入外部知识。"
    "如果某个实体或关系无法对应到给定本体中的类型，把它标成 UNK-ENTITY 或 UNK-RELATION，"
    "不要强行归类——误归类会污染知识图谱，漏抽只是少一条边。"
)


def _build_extraction_prompt(text: str, ontology: Ontology, context: dict) -> str:
    types = "、".join(ontology.node_types())
    relations = "、".join(sorted(ontology.properties))
    doc_context = f"已知文档主体：{context.get('company') or '未识别'}"
    return (
        f"从下面的行业文档片段中抽取实体和关系。\n\n"
        f"{doc_context}\n\n"
        f"【可用实体类型】{types}\n"
        f"【可用关系类型】{relations}\n\n"
        f"【文档片段】\n{text[:3000]}\n\n"
        f"输出 JSON，结构如下：\n"
        f'{{"entities": [{{"text": "实体原文", "type": "上述类型之一或 UNK-ENTITY", '
        f'"confidence": 0.0-1.0, "attributes": {{}}}}], '
        f'"relations": [{{"head": "头实体原文", "tail": "尾实体原文", '
        f'"type": "上述关系之一或 UNK-RELATION", "confidence": 0.0-1.0}}]}}'
    )


def _safe_float(value, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default
