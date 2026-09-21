"""跨层绑定器：把 Layer1 实体、Layer2 规则、Layer1 指标接起来。

没有这一层，三层图只是三个互不相干的子图——政务侧的"社保断缴"永远走不到
金融侧的"经营稳定性"。绑定器负责三件事：

1. ``受约束于``：按条款的"适用对象"把条款挂到匹配的主体类型上（L1→L2）。
2. ``对应指标``：把 Layer1 的风险指标实例挂到 Layer2 的风险维度下（L2→L1）。
3. ``设定阈值``：把条款中的数值边界抽成阈值节点。

第 2 步的匹配不靠硬编码词表，而是用**条款原文与指标名称的最长公共子串**来
判定。理由是：指标与维度的关联必须能从监管文本本身推导出来，否则本体演化
（新条款、新指标）时这张映射表会立刻失效。匹配到的公共子串会作为证据写入边，
审计时能看到"这条指标挂到该维度下，是因为 §3.2 原文同时提到了『社保』"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from govfin.graph.schema import EVIDENCE_DERIVED, LAYER_ENTITY, LAYER_RULE
from govfin.graph.store import GraphStore
from govfin.ontology.model import Ontology

# 阈值抽取：条款中的数值边界表述。
# 监管文本里数字常用中文大写（"百分之七十"），正则必须同时吃数字与汉字数字，
# 否则最容易命中的那几条阈值反而抽不出来。
_NUM = r"(?:[零一二三四五六七八九十百千]+|[\d.]+)"
_THRESHOLD_PATTERNS = [
    (re.compile(rf"资产负债率高于(?:百分之)?({_NUM})"), "资产负债率上限", "低于", 100.0),
    (re.compile(rf"连续({_NUM})个月"), "连续异常月数上限", "低于", 1.0),
    (re.compile(rf"不超过({_NUM})个?基点"), "利率加点上限", "低于", 1.0),
    (re.compile(rf"不低于({_NUM})%"), "最低达标比例", "高于", 100.0),
    (re.compile(rf"超过({_NUM})%"), "占比上限", "低于", 100.0),
]

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}

MIN_MATCH_LEN = 2


@dataclass
class BindingReport:
    constrained_edges: int = 0
    indicator_edges: int = 0
    threshold_nodes: int = 0
    threshold_dimension_edges: int = 0
    unmatched_dimensions: list[str] = field(default_factory=list)
    matches: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "constrained_edges": self.constrained_edges,
            "indicator_edges": self.indicator_edges,
            "threshold_nodes": self.threshold_nodes,
            "threshold_dimension_edges": self.threshold_dimension_edges,
            "unmatched_dimensions": self.unmatched_dimensions,
            "matches": self.matches[:50],
        }


class RuleBinder:
    def __init__(self, store: GraphStore, ontology: Ontology | None = None) -> None:
        self.store = store
        self.ontology = ontology or store.ontology

    def bind(self) -> BindingReport:
        report = BindingReport()
        clauses = self._collect_clauses()
        if not clauses:
            return report

        self._bind_constraints(clauses, report)
        self._bind_thresholds(clauses, report)
        self._bind_indicators(clauses, report)
        return report

    # ------------------------------------------------------------------

    def _collect_clauses(self) -> list[dict]:
        clause_types = {
            name for name in self.ontology.node_types(LAYER_RULE)
            if self.ontology.is_subclass_of(name, "监管规范")
        }
        clauses: list[dict] = []
        for ntype in clause_types:
            clauses.extend(self.store.query_nodes(ntype=ntype))
        return clauses

    def _subject_types(self, clause: dict) -> list[str]:
        """条款的适用对象 → 本体中实际存在的实体类型集合。"""
        raw = str(clause["props"].get("适用对象", "")).strip()
        if not raw:
            raw = "主体"
        candidates: list[str] = []
        for token in re.split(r"[、,，/;；\s]+", raw):
            token = token.strip()
            if not token:
                continue
            if token in self.ontology.classes:
                candidates.append(token)
            else:
                for alias, target in self._alias_index().items():
                    if alias and alias in token:
                        candidates.append(target)
        if not candidates:
            candidates = ["主体"]
        # 展开到子类：条款说"企业"，小微企业/中型企业都要挂上
        expanded: list[str] = []
        for cand in candidates:
            expanded.extend(self.ontology.descendants(cand))
        return sorted(set(expanded) or {"主体"})

    def _alias_index(self) -> dict[str, str]:
        index: dict[str, str] = {}
        for cls in self.ontology.classes.values():
            for alias in cls.aliases:
                index[alias] = cls.name
        return index

    def _bind_constraints(self, clauses: list[dict], report: BindingReport) -> None:
        for clause in clauses:
            for subject_type in self._subject_types(clause):
                if subject_type not in self.ontology.classes:
                    continue
                if subject_type in ("主体",):
                    continue
                subjects = self.store.query_nodes(ntype=subject_type)
                for subject in subjects:
                    if self._has_edge(subject["id"], clause["id"], "受约束于"):
                        continue
                    self.store.add_edge(
                        subject["id"],
                        clause["id"],
                        "受约束于",
                        evidence={
                            "snippet": f"条款「{clause['label']}」适用对象为 {subject_type}",
                            "modality": "rule",
                            "source_document": clause["props"].get("条款编号", clause["id"]),
                            "source_locator": "binder:applicability",
                            "reason": "条款适用对象与企业类型匹配",
                        },
                        confidence=float(clause["props"].get("条款明确程度", 0.8) or 0.8),
                        evidence_class=EVIDENCE_DERIVED,
                        validate=False,
                    )
                    report.constrained_edges += 1

    def _bind_thresholds(self, clauses: list[dict], report: BindingReport) -> None:
        for clause in clauses:
            text = str(clause["props"].get("条款原文", ""))
            for pattern, name, direction, scale in _THRESHOLD_PATTERNS:
                match = pattern.search(text)
                if not match:
                    continue
                value = _parse_number(match, scale)
                if value is None:
                    continue
                label = f"{name}@{clause['id']}"
                try:
                    node_id = self.store.upsert_node(
                        "阈值",
                        layer=LAYER_RULE,
                        props={"阈值名称": name, "阈值": value, "比较方向": direction},
                        node_id=label,
                    )
                except Exception:  # noqa: BLE001 - 阈值抽取失败不应阻断绑定
                    continue
                report.threshold_nodes += 1
                if not self._has_edge(clause["id"], node_id, "设定阈值"):
                    self.store.add_edge(
                        clause["id"],
                        node_id,
                        "设定阈值",
                        evidence={
                            "snippet": match.group(0),
                            "modality": "rule",
                            "source_document": clause["props"].get("条款编号", clause["id"]),
                            "source_locator": "binder:threshold",
                        },
                        confidence=0.9,
                        validate=False,
                    )
                report.threshold_dimension_edges += self._link_threshold_dimensions(
                    clause, node_id, match.group(0)
                )

    def _link_threshold_dimensions(self, clause: dict, threshold_id: str, snippet: str) -> int:
        """阈值 → 风险维度。

        不建这条边的话，推理时问"偿债能力的判定边界是多少"必须先从阈值跳回条款、
        再跳到维度，一跳变两跳，且中间那条 `映射维度` 边的证据与阈值无关。
        直接连起来，约束才能作为可独立引用的判定依据进入决策链。
        """
        linked = 0
        for edge in self.store.find_edges(src=clause["id"], etype="映射维度"):
            if self._has_edge(threshold_id, edge["dst"], "约束维度"):
                continue
            self.store.add_edge(
                threshold_id,
                edge["dst"],
                "约束维度",
                evidence={
                    "snippet": snippet,
                    "modality": "rule",
                    "source_document": clause["props"].get("条款编号", clause["id"]),
                    "source_locator": "binder:threshold-dimension",
                },
                confidence=0.85,
                evidence_class=EVIDENCE_DERIVED,
                validate=False,
            )
            linked += 1
        return linked

    def _bind_indicators(self, clauses: list[dict], report: BindingReport) -> None:
        dimensions = self.store.query_nodes(ntype="风险维度")
        if not dimensions:
            return

        # 每个维度收集其关联条款的原文，作为匹配语料
        dimension_text: dict[str, list[tuple[str, str]]] = {}
        for dim in dimensions:
            texts: list[tuple[str, str]] = []
            for clause in clauses:
                if self._has_edge(clause["id"], dim["id"], "映射维度"):
                    texts.append((clause["id"], str(clause["props"].get("条款原文", ""))))
            dimension_text[dim["id"]] = texts

        indicators = self.store.query_nodes(ntype="风险指标")
        for indicator in indicators:
            base = re.sub(r"[（(].*?[）)]", "", indicator["label"]).strip()
            if not base:
                continue
            best: tuple[int, str, str, str] | None = None  # (长度, 子串, 维度ID, 条款ID)
            for dim_id, texts in dimension_text.items():
                for clause_id, text in texts:
                    if not text:
                        continue
                    common = _longest_common_substring(base, text)
                    if len(common) >= MIN_MATCH_LEN and (best is None or len(common) > best[0]):
                        best = (len(common), common, dim_id, clause_id)
            if best is None:
                for dim in dimensions:
                    report.unmatched_dimensions.append(f"{indicator['label']} ↔ {dim['label']}")
                continue

            _, common, dim_id, clause_id = best
            if self._has_edge(dim_id, indicator["id"], "对应指标"):
                continue
            clause = self.store.get_node(clause_id)
            self.store.add_edge(
                dim_id,
                indicator["id"],
                "对应指标",
                evidence={
                    "snippet": f"指标「{base}」与条款原文共享表述「{common}」",
                    "modality": "rule",
                    "source_document": clause["props"].get("条款编号", clause_id) if clause else clause_id,
                    "source_locator": "binder:indicator-match",
                    "matched_span": common,
                },
                confidence=min(0.95, 0.6 + 0.05 * len(common)),
                evidence_class=EVIDENCE_DERIVED,
                validate=False,
            )
            report.indicator_edges += 1
            report.matches.append(
                {
                    "indicator": indicator["label"],
                    "dimension": self.store.get_node(dim_id)["label"],
                    "clause": clause_id,
                    "matched_span": common,
                }
            )

    # ------------------------------------------------------------------

    def _has_edge(self, src: str, dst: str, etype: str) -> bool:
        return bool(self.store.find_edges(src=src, dst=dst, etype=etype))


def _longest_common_substring(a: str, b: str) -> str:
    """最长公共子串。用经典的滚动数组 DP，O(len(a)*len(b)) 时间、O(len(b)) 空间。

    匹配语料是单条条款原文（数百字），指标名（十几个字），规模完全够用。
    """
    if not a or not b:
        return ""
    best_len = 0
    best_end = 0
    previous = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        current = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                current[j] = previous[j - 1] + 1
                if current[j] > best_len:
                    best_len = current[j]
                    best_end = i
        previous = current
    return a[best_end - best_len : best_end]


def _parse_number(match: re.Match, scale: float = 1.0) -> float | None:
    raw = next((g for g in match.groups() if g is not None), None)
    if raw is None:
        return None
    value = _cn_to_number(raw)
    if value is None:
        return None
    # 百分比归一：条款既可能写"百分之七十"（70），也可能写"0.7"。
    # 只在数值本身是小数（≤1）时套用 scale，否则会把 70 错放大成 7000。
    if scale != 1.0 and value <= 1.0:
        value *= scale
    return round(value, 4)


def _cn_to_number(text: str) -> float | None:
    """中文数字 → float。支持"七十"、"一百二十"、以及混排的"3"、"2.5"。"""
    text = text.strip()
    if not text:
        return None
    if re.fullmatch(r"[\d.]+", text):
        try:
            return float(text)
        except ValueError:
            return None

    total = 0
    section = 0
    current = 0
    saw_digit = False
    for ch in text:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
            saw_digit = True
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            # "十七" 里的十前面没有数字，按 1 处理
            section += (current or 1) * unit
            current = 0
            saw_digit = True
        else:
            return None
    if not saw_digit:
        return None
    return float(total + section + current)
