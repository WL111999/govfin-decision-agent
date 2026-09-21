"""符号对齐：先用已有公理去解释 UNK，解释不了才允许新增本体元素。

神经符号管道的"符号"一半就在这里。它反过来压制 LLM 提案的自由度：
**能靠别名学习消化的候选，绝不允许长成一个新类。**

理由很实在。低资源域里 LLM 面对一个没见过的词，几乎一定会热情地建议"新增一个
类"——它不知道本体的当前状态，也不知道这个名字其实只是某个已有类的另一种写法。
放任下去，本体几轮演化后会出现三个几乎同义的类，而图上同一个现实实体被拆成
三个节点、跨文档推理直接失效。所以顺序必须是：先剥词缀找同义 → 再查别名 →
都不行才走新类，且新类必须挂到由结构证据推出来的父类下。

父类推断用两路证据融合，都可以逐条打印出来给仲裁者看：

- **形态证据**：候选与已有类名/别名的字符 n-gram 相似度（"某某异常名录"与
  "环保核查"无关，但与"经营异常"高度共享构词）。
- **结构证据**：候选的上下文里，哪些已有类的实例名反复出现。一个提及如果总是
  出现在"社保缴纳记录"的上下文里，它大概率属于政务记录这一支。

形态证据单独用会犯"长得像但语义远"的错，结构证据单独用会被共现噪声带偏，
两者相乘再归一，是这个规模下最稳的折中。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from govfin.evolution.clustering import UnkCluster, _cosine, _containment, _tfidf_vectors
from govfin.evolution.unk_pool import KIND_RELATION
from govfin.graph.schema import LAYER_ENTITY, LAYER_RULE
from govfin.ontology.model import Ontology, OwlClass

# 中文专业术语的常见限定/后缀。剥掉后仍能命中已有类，说明这是别名而非新概念。
STRIPPABLE = (
    "信息",
    "数据",
    "记录",
    "信息表",
    "明细",
    "详情",
    "表",
    "库",
    "字段",
    "编号",
    "号码",
    "代码",
    "事项",
    "情况",
    "状态",
    "结果",
    "类型",
)

# 至少要有这么多字符才有资格成为一个类名；"3"、"合计"、"其他" 这类碎片
# 是抽取噪声，不是等待被发现的领域概念。
MIN_CONCEPT_LEN = 3
MIN_RELATION_LEN = 2

ALIAS_SCORE_FLOOR = 0.55
NEW_CLASS_SCORE_FLOOR = 0.30
HINT_MULTIPLIER = 1.45  # 抽取器类型提示命中时的乘数，见 _score_parents

_TRAILING_PAREN = re.compile(r"[（(][^）)]*[）)]\s*$")
_NUMERICISH = re.compile(r"^[\d.%\-—/、，,：:]+$")


@dataclass
class AlignmentResult:
    cluster_id: str
    kind: str
    decision: str  # alias | new_class | reject
    canonical: str
    target_class: str | None = None       # decision == alias
    new_aliases: list[str] = field(default_factory=list)
    parent_class: str | None = None       # decision == new_class
    domain_class: str | None = None       # 关系候选用
    range_class: str | None = None
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "cluster_id": self.cluster_id,
            "kind": self.kind,
            "decision": self.decision,
            "canonical": self.canonical,
            "target_class": self.target_class,
            "new_aliases": self.new_aliases,
            "parent_class": self.parent_class,
            "domain_class": self.domain_class,
            "range_class": self.range_class,
            "confidence": round(self.confidence, 4),
            "reasons": self.reasons,
            "conflicts": self.conflicts,
            "evidence": self.evidence,
        }


class SymbolicAligner:
    def __init__(self, ontology: Ontology) -> None:
        self.ontology = ontology
        self._class_names = list(ontology.classes)
        self._vectors = _tfidf_vectors(self._class_names) if self._class_names else []
        self._vector_of = dict(zip(self._class_names, self._vectors))
        self._instance_index = self._build_instance_index()

    # ------------------------------------------------------------------

    def align(self, cluster: UnkCluster) -> AlignmentResult:
        canonical = cluster.canonical
        if self._is_noise(canonical, cluster):
            return AlignmentResult(
                cluster_id=cluster.cluster_id,
                kind=cluster.kind,
                decision="reject",
                canonical=canonical,
                confidence=0.0,
                reasons=["候选形如噪声（长度不足或纯符号/数字），不足以构成领域概念"],
            )

        alias_hit = self._try_alias(cluster)
        if alias_hit is not None:
            return alias_hit

        if cluster.kind == KIND_RELATION:
            return self._align_relation(cluster)
        return self._align_entity(cluster)

    # ------------------------------------------------------------------

    def _is_noise(self, canonical: str, cluster: UnkCluster) -> bool:
        text = _TRAILING_PAREN.sub("", canonical).strip()
        # 关系的长度门槛必须比类低：中文关系动词大量是双字（控股、担保、任职、
        # 触发），拿三条字的标准去筛会把最正常的关系词全判成噪声。
        floor = MIN_RELATION_LEN if cluster.kind == KIND_RELATION else MIN_CONCEPT_LEN
        if len(text) < floor:
            return True
        if _NUMERICISH.match(text):
            return True
        # 单次观测 + 只出现在一份文档 + 簇内无同伴：信息量不足以支撑本体变更
        return cluster.total_observations < 2 and cluster.document_count() < 2 and len(cluster.members) < 2

    def _try_alias(self, cluster: UnkCluster) -> AlignmentResult | None:
        """别名学习：候选（或剥掉词缀后的候选）能对上已有类，就不该新增类。"""
        if cluster.kind == KIND_RELATION:
            return self._try_property_alias(cluster)

        for name in [cluster.canonical, *cluster.members]:
            hits = self._resolve_with_stripping(name)
            if hits is None:
                continue
            target, matched_as, how = hits
            new_aliases = [
                m for m in dict.fromkeys([cluster.canonical, *cluster.members])
                if m != matched_as and m != target and self.ontology.resolve_alias(m) is None
            ]
            reason = (
                f"候选「{matched_as}」经{how}后命中已有类「{target}」，"
                "应作别名学习而非新增类——新增会造出同义类并让同一实体在图上分裂"
            )
            return AlignmentResult(
                cluster_id=cluster.cluster_id,
                kind=cluster.kind,
                decision="alias",
                canonical=cluster.canonical,
                target_class=target,
                new_aliases=new_aliases,
                confidence=min(0.99, ALIAS_SCORE_FLOOR + 0.05 * cluster.total_observations),
                reasons=[reason],
                evidence={
                    "matched_as": matched_as,
                    "match_method": how,
                    "observations": cluster.total_observations,
                    "documents": cluster.document_count(),
                },
            )
        return None

    def _try_property_alias(self, cluster: UnkCluster) -> AlignmentResult | None:
        """关系候选先跟已有的对象属性做归并，再考虑新增属性。

        判据是双向包含：``触发`` ⊂ ``触发风险信号``，``实际控制`` ⊂ ``实际控制人``。
        关系的措辞在不同文书里本来就长短不一，同一现实关系写成两种边类型会让
        路径搜索漏边——这比多一个类严重得多，边类型分裂是**静默**漏边。
        """
        properties = [
            p for p in self.ontology.properties.values() if p.kind == "object"
        ]
        best: tuple[float, object] | None = None
        for name in dict.fromkeys([cluster.canonical, *cluster.members]):
            cleaned = _TRAILING_PAREN.sub("", name).strip()
            if len(cleaned) < MIN_CONCEPT_LEN:
                continue
            for prop in properties:
                if cleaned == prop.name or cleaned in prop.aliases:
                    return self._property_alias_result(cluster, prop.name, cleaned, "精确名称/别名匹配")
                if cleaned in prop.name or prop.name in cleaned:
                    shorter, longer = sorted((len(cleaned), len(prop.name)))
                    ratio = shorter / longer
                    if ratio >= 0.6 and (best is None or ratio > best[0]):
                        best = (ratio, prop, cleaned)
        if best is None:
            return None
        _, prop, cleaned = best
        return self._property_alias_result(cluster, prop.name, cleaned, "双向包含匹配")

    def _property_alias_result(
        self, cluster: UnkCluster, target: str, matched_as: str, how: str
    ) -> AlignmentResult:
        new_aliases = [
            m
            for m in dict.fromkeys([cluster.canonical, *cluster.members])
            if m != matched_as
            and m != target
            and self.ontology.resolve_property_alias(m) is None
            and self.ontology.resolve_alias(m) is None
        ]
        return AlignmentResult(
            cluster_id=cluster.cluster_id,
            kind=cluster.kind,
            decision="alias",
            canonical=cluster.canonical,
            target_class=target,
            new_aliases=new_aliases,
            confidence=min(0.95, ALIAS_SCORE_FLOOR + 0.04 * cluster.total_observations),
            reasons=[
                f"关系候选「{matched_as}」经{how}命中已有对象属性「{target}」；"
                "归并到同一属性而非新增，否则同一现实关系会分裂成两种边类型，"
                "路径搜索将静默漏边"
            ],
            evidence={
                "matched_as": matched_as,
                "match_method": how,
                "target_kind": "object_property",
                "observations": cluster.total_observations,
            },
        )

    def _resolve_with_stripping(self, name: str) -> tuple[str, str, str] | None:
        cleaned = _TRAILING_PAREN.sub("", name).strip()
        direct = self.ontology.resolve_alias(cleaned)
        if direct is not None:
            return direct, cleaned, "精确名称/别名匹配"

        for suffix in sorted(STRIPPABLE, key=len, reverse=True):
            if cleaned.endswith(suffix) and len(cleaned) - len(suffix) >= MIN_CONCEPT_LEN:
                stem = cleaned[: -len(suffix)]
                hit = self.ontology.resolve_alias(stem)
                if hit is not None:
                    return hit, stem, f"剥离后缀「{suffix}」"

        for suffix in sorted(STRIPPABLE, key=len, reverse=True):
            if cleaned.startswith(suffix) and len(cleaned) - len(suffix) >= MIN_CONCEPT_LEN:
                stem = cleaned[len(suffix) :]
                hit = self.ontology.resolve_alias(stem)
                if hit is not None:
                    return hit, stem, f"剥离前缀「{suffix}」"
        return None

    # ------------------------------------------------------------------

    def _align_entity(self, cluster: UnkCluster) -> AlignmentResult:
        scored = self._score_parents(cluster)
        if not scored:
            return AlignmentResult(
                cluster_id=cluster.cluster_id,
                kind=cluster.kind,
                decision="reject",
                canonical=cluster.canonical,
                reasons=["找不到任何结构或形态上可挂靠的父类，暂不进入本体变更"],
            )

        parent, score, evidence = scored[0]
        candidate = OwlClass(
            name=cluster.canonical,
            parent=parent,
            definition=f"由 UNK 候选演化而来的概念（{cluster.total_observations} 次观测，"
            f"{cluster.document_count()} 份文档）",
            layer=self.ontology.classes[parent].layer,
        )
        conflicts = self.ontology.check_conflicts(candidate)
        reasons = [
            f"父类「{parent}」综合得分 {score:.3f}"
            f"（形态 {evidence['morphology']:.3f} / 结构 {evidence['structural']:.3f}"
            f" / 类型提示 {evidence['hint']:.3f}）"
        ]
        if any(c["severity"] == "error" for c in conflicts):
            return AlignmentResult(
                cluster_id=cluster.cluster_id,
                kind=cluster.kind,
                decision="reject",
                canonical=cluster.canonical,
                parent_class=parent,
                confidence=0.0,
                reasons=reasons + ["候选与现有公理存在阻断性冲突，拒绝自动演化"],
                conflicts=conflicts,
                evidence=evidence,
            )

        if score < NEW_CLASS_SCORE_FLOOR:
            return AlignmentResult(
                cluster_id=cluster.cluster_id,
                kind=cluster.kind,
                decision="reject",
                canonical=cluster.canonical,
                parent_class=parent,
                confidence=score,
                reasons=reasons + [f"最可信父类得分低于门槛 {NEW_CLASS_SCORE_FLOOR}，挂靠位置不可靠"],
                conflicts=conflicts,
                evidence=evidence,
            )

        return AlignmentResult(
            cluster_id=cluster.cluster_id,
            kind=cluster.kind,
            decision="new_class",
            canonical=cluster.canonical,
            parent_class=parent,
            confidence=score,
            reasons=reasons,
            conflicts=conflicts,
            evidence=evidence,
        )

    def _score_parents(self, cluster: UnkCluster) -> list[tuple[str, float, dict]]:
        """给每个已有类打一个"当候选父类"的分。三路证据相乘再开方。

        相乘而不是相加，是因为三条证据必须**同时**成立才说明挂靠合理：
        只像名字但结构证据为零，或只在上下文里共现但构词毫无关系，
        都不足以支撑一次本体变更。
        """
        target_vec = _tfidf_vectors([cluster.canonical])[0]
        contexts = " ".join(cluster.sample_contexts)
        hints = {h for h in cluster.hint_types if h and not h.startswith("UNK-")}

        results: list[tuple[str, float, dict]] = []
        for name, vec in self._vector_of.items():
            cls = self.ontology.classes[name]
            # 只允许挂到实体层/规则层之下。允许挂到运行时层（决策、推理步骤）或
            # 元层（本体提案）会产出一个语义上荒谬的类层级，而且这类错误一旦提交
            # 就再也回不来了。
            if cls.layer not in (LAYER_ENTITY, LAYER_RULE):
                continue
            morph = max(
                _cosine(target_vec, vec),
                _containment(target_vec, vec),
            )
            structural = self._structural_score(name, contexts)
            # 只用精确的 hint_type（来自属性 range 的精确结构事实）。
            # 刻意**不**给"别名出现在上下文里"任何 hint 权重：企业文书的每一段里
            # 都写着"公司""企业"，那种共现说明不了这条提及是什么，只会让最泛化的
            # 类永远胜出，把每个新概念都挂到层级最浅的节点上。共现信息已经由
            # structural 那一路承担了。
            hint = 1.0 if name in hints else 0.0
            specificity = HINT_MULTIPLIER if hint >= 0.7 else 1.0
            base = (morph + 0.15) * (structural + 0.15) * (hint + 0.25) * specificity
            score = base ** (1 / 3)
            results.append(
                (
                    name,
                    score,
                    {
                        "morphology": round(morph, 4),
                        "structural": round(structural, 4),
                        "hint": round(hint, 4),
                        "specificity": specificity,
                        "cluster_members": cluster.members[:6],
                    },
                )
            )
        results.sort(key=lambda x: -x[1])
        return results[:3]

    def _build_instance_index(self) -> dict[str, list[str]]:
        """类名 → 该类在领域文本里可能出现的中文表述。

        用类名本身与别名做代理，而不是去图上扫全部实例节点：演化管道可能在
        图还没建成时就要跑（例如冷启动阶段），依赖图内容会让对齐结果随图规模
        漂移，不可复现。
        """
        index: dict[str, list[str]] = {}
        for name, cls in self.ontology.classes.items():
            if cls.layer not in (LAYER_ENTITY, LAYER_RULE):
                continue
            tokens = [name, *cls.aliases]
            index[name] = [t for t in tokens if len(t) >= 2]
        return index

    def _structural_score(self, class_name: str, contexts: str) -> float:
        if not contexts:
            return 0.0
        tokens = self._instance_index.get(class_name) or []
        if not tokens:
            return 0.0
        hits = sum(1 for token in tokens if token in contexts)
        return min(1.0, hits / len(tokens))

    # ------------------------------------------------------------------

    def _align_relation(self, cluster: UnkCluster) -> AlignmentResult:
        """关系候选：必须在文本里找到 domain 与 range 两侧的证据，否则不提案。

        关系比类危险得多——一个 domain/range 定错的对象属性，会让后续所有
        跨层路径搜索走出错误的路由。宁可不提，也不能猜。
        """
        before: dict[str, int] = {}
        after: dict[str, int] = {}
        for ctx in cluster.sample_contexts:
            for match in re.finditer(re.escape(cluster.canonical), ctx):
                left = ctx[max(0, match.start() - 40) : match.start()]
                right = ctx[match.end() : match.end() + 40]
                for cls, tokens in self._instance_index.items():
                    for token in tokens:
                        if token in left:
                            before[cls] = before.get(cls, 0) + 1
                        if token in right:
                            after[cls] = after.get(cls, 0) + 1

        if not before or not after:
            return AlignmentResult(
                cluster_id=cluster.cluster_id,
                kind=cluster.kind,
                decision="reject",
                canonical=cluster.canonical,
                confidence=0.0,
                reasons=[
                    "关系候选的上下文中无法同时定位 domain 与 range 两侧证据；"
                    "domain/range 猜错会污染路径搜索的路由，因此不提案"
                ],
                evidence={"domain_candidates": before, "range_candidates": after},
            )

        def _rank(table: dict[str, int]) -> list[str]:
            return [k for k, _ in sorted(table.items(), key=lambda kv: (-kv[1], kv[0]))]

        domains, ranges = _rank(before), _rank(after)
        # 只保留与已定义对象属性一致的组合；如果某个组合已经存在同名/同向属性，
        # 那本身就是别名，交由上层继续处理。
        domain = next((d for d in domains if d in self.ontology.classes), None)
        rng = next((r for r in ranges if r in self.ontology.classes and r != domain), None)
        if domain is None or rng is None:
            return AlignmentResult(
                cluster_id=cluster.cluster_id,
                kind=cluster.kind,
                decision="reject",
                canonical=cluster.canonical,
                confidence=0.0,
                reasons=["关系两端候选类型无法落在本体已定义类上"],
                evidence={"domain_candidates": domains[:5], "range_candidates": ranges[:5]},
            )

        support = min(before.get(domain, 0), after.get(rng, 0))
        total = sum(before.values()) + sum(after.values())
        confidence = min(0.9, 0.35 + 0.55 * (support / max(1, total)))
        return AlignmentResult(
            cluster_id=cluster.cluster_id,
            kind=cluster.kind,
            decision="new_class",  # 关系对应的编辑动作是 add_property，由提案层翻译
            canonical=cluster.canonical,
            domain_class=domain,
            range_class=rng,
            confidence=confidence,
            reasons=[
                f"上下文证据：提及前出现「{domain}」{before.get(domain, 0)} 次、"
                f"提及后出现「{rng}」{after.get(rng, 0)} 次，推得 domain/range"
            ],
            evidence={
                "domain_candidates": domains[:5],
                "range_candidates": ranges[:5],
                "support": support,
            },
        )


__all__ = ["AlignmentResult", "SymbolicAligner", "NEW_CLASS_SCORE_FLOOR", "ALIAS_SCORE_FLOOR"]
