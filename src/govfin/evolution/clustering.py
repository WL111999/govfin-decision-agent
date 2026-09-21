"""UNK 候选的语义聚类：把"看起来是一回事"的提及归到一起。

**为什么不用向量嵌入。** 低资源域（尤其是政务/金融垂域）没有可信的领域嵌入
模型，调通用嵌入 API 又会把"低资源"这个前提抹掉——而且嵌入相似度不可解释，
审计时无法回答"这两条为什么被归成一类"。这里用三条**可解释**的信号做融合：

1. **字符 n-gram TF-IDF 余弦**：中文专业术语的构词高度复用（"经营异常名录"与
   "异常经营名录"共享绝大多数 n-gram），字符级表示在无分词器时反而更稳。
2. **包含度**：``|grams(A) ∩ grams(B)| / min(|A|, |B|)``。专有名词常常是彼此
   的加长版（"社保缴纳记录" vs "社保缴纳记录信息"），余弦会被长度差稀释，
   包含度不会。
3. **文档共现**：同一份文档里反复一起出现的两个提及，语义相关的先验更高。

融合后做**平均连接的层次聚类**。用平均连接而不是单连接，是因为单连接的链式
效应在术语图上非常致命——"资产负债表"→"资产总计"→"负债合计"会一路串成
一个巨型簇，把整轮演化提案搅成一锅粥。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from govfin.evolution.unk_pool import KIND_ENTITY, UnkCandidate

NGRAM_SIZES = (1, 2, 3)

# 融合权重：余弦管"整体像不像"，包含度管"是不是同一概念的加长版"，
# 共现只是弱先验，权重刻意压得最低。
W_COSINE = 0.5
W_CONTAINMENT = 0.35
W_COOCCUR = 0.15

HINT_BONUS = 0.08      # 抽取器对两者给出的类型提示一致时的小幅加成
MERGE_THRESHOLD = 0.46  # 平均连接相似度下限，低于此值不合并
EVICT_THRESHOLD = 0.32  # 簇内成员与簇均值的下限，低于此值被拆出去

MAX_PAIRWISE = 4000  # 超过这个规模就先用廉价前缀分桶，避免 O(n^2) 爆炸


@dataclass
class UnkCluster:
    cluster_id: str
    kind: str
    canonical: str
    members: list[str] = field(default_factory=list)
    member_ids: list[str] = field(default_factory=list)
    total_observations: int = 0
    cohesion: float = 0.0
    documents: list[str] = field(default_factory=list)
    sample_contexts: list[str] = field(default_factory=list)
    hint_types: list[str] = field(default_factory=list)
    merge_evidence: list[dict] = field(default_factory=list)

    def document_count(self) -> int:
        return len(self.documents)

    def to_dict(self) -> dict:
        return {
            "cluster_id": self.cluster_id,
            "kind": self.kind,
            "canonical": self.canonical,
            "members": self.members,
            "total_observations": self.total_observations,
            "cohesion": round(self.cohesion, 4),
            "documents": self.documents[:8],
            "sample_contexts": self.sample_contexts[:4],
            "hint_types": self.hint_types,
            "merge_evidence": self.merge_evidence[:6],
        }


def _ngrams(text: str, sizes: tuple[int, ...] = NGRAM_SIZES) -> dict[str, int]:
    grams: dict[str, int] = {}
    for n in sizes:
        if len(text) < n:
            continue
        for i in range(len(text) - n + 1):
            gram = text[i : i + n]
            grams[gram] = grams.get(gram, 0) + 1
    return grams


def _tfidf_vectors(texts: list[str]) -> list[dict[str, float]]:
    raw = [_ngrams(t) for t in texts]
    df: dict[str, int] = {}
    for grams in raw:
        for gram in grams:
            df[gram] = df.get(gram, 0) + 1
    total = len(texts) or 1

    vectors: list[dict[str, float]] = []
    for grams in raw:
        vec: dict[str, float] = {}
        for gram, count in grams.items():
            tf = 1.0 + math.log(count)
            idf = math.log((total + 1) / (df.get(gram, 0) + 1)) + 1.0
            vec[gram] = tf * idf
        vectors.append(vec)
    return vectors


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = sum(weight * b.get(gram, 0.0) for gram, weight in a.items())
    if dot <= 0.0:
        return 0.0
    na = math.sqrt(sum(w * w for w in a.values()))
    nb = math.sqrt(sum(w * w for w in b.values()))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (na * nb)


def _containment(a: dict[str, float], b: dict[str, float]) -> float:
    """双向包含度取大：专有名词加长/缩短两个方向都算同一概念的形态变体。"""
    if not a or not b:
        return 0.0
    shared = len(set(a) & set(b))
    return shared / max(1, min(len(a), len(b)))


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class _Pair:
    i: int
    j: int
    score: float
    signals: dict[str, float]


class UnkClusterer:
    def __init__(
        self,
        *,
        merge_threshold: float = MERGE_THRESHOLD,
        evict_threshold: float = EVICT_THRESHOLD,
    ) -> None:
        self.merge_threshold = merge_threshold
        self.evict_threshold = evict_threshold

    def cluster(self, candidates: list[UnkCandidate]) -> list[UnkCluster]:
        if not candidates:
            return []

        # 实体与关系分开聚类：两者即使文本相同，本体里的处理方式也完全不同
        # （一个是类/个体，一个是属性），混在一起聚出来的簇无法生成合法提案。
        out: list[UnkCluster] = []
        for kind in sorted({c.kind for c in candidates}):
            group = [c for c in candidates if c.kind == kind]
            out.extend(self._cluster_kind(group, kind))
        out.sort(key=lambda c: (-c.total_observations, c.canonical))
        return out

    def _cluster_kind(self, group: list[UnkCandidate], kind: str) -> list[UnkCluster]:
        if not group:
            return []
        if len(group) == 1:
            return [self._singleton(group[0], kind)]

        texts = [c.text for c in group]
        vectors = _tfidf_vectors(texts)
        doc_sets = [set(c.source_documents) for c in group]

        pairs: list[_Pair] = []
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                pair = self._score(i, j, group, vectors, doc_sets)
                if pair.score >= self.evict_threshold:
                    pairs.append(pair)

        parent = list(range(len(group)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        members: dict[int, list[int]] = {i: [i] for i in range(len(group))}
        # 贪心按相似度降序合并，但要求合并后簇内的**平均**两两相似度仍然达标。
        # 这就是平均连接：单链会串成大杂烩，全连接又过于严苛导致明明同义的
        # 长短变体分不到一起。
        for pair in sorted(pairs, key=lambda p: -p.score):
            if pair.score < self.merge_threshold:
                break
            ra, rb = find(pair.i), find(pair.j)
            if ra == rb:
                continue
            merged = members[ra] + members[rb]
            if self._average_linkage(merged, pairs) < self.merge_threshold:
                continue
            parent[rb] = ra
            members[ra] = merged
            del members[rb]

        clusters: list[UnkCluster] = []
        for idx, member_idx in members.items():
            member_idx = self._evict_outliers(member_idx, pairs, group, vectors)
            members[idx] = member_idx
            clusters.append(self._build(member_idx, group, vectors, pairs, kind))
        return clusters

    def _score(
        self,
        i: int,
        j: int,
        group: list[UnkCandidate],
        vectors: list[dict[str, float]],
        doc_sets: list[set],
    ) -> _Pair:
        cos = _cosine(vectors[i], vectors[j])
        cont = _containment(vectors[i], vectors[j])
        cooc = _jaccard(doc_sets[i], doc_sets[j])
        signals = {"ngram_cosine": cos, "containment": cont, "cooccurrence": cooc}
        score = W_COSINE * cos + W_CONTAINMENT * cont + W_COOCCUR * cooc
        hi, hj = group[i].hint_type, group[j].hint_type
        if hi and hi == hj and not hi.startswith("UNK-"):
            score += HINT_BONUS
            signals["hint_match"] = 1.0
        return _Pair(i, j, min(score, 1.0), signals)

    def _average_linkage(self, cluster: list[int], pairs: list[_Pair]) -> float:
        if len(cluster) < 2:
            return 1.0
        index = {v: k for k, v in enumerate(cluster)}
        scores: list[float] = []
        for pair in pairs:
            if pair.i in index and pair.j in index:
                scores.append(pair.score)
        expected = len(cluster) * (len(cluster) - 1) / 2
        if len(scores) < expected:
            # 缺失的对（相似度低于 evict 阈值、当时没进 pairs）按 0 计入分母，
            # 否则"只统计已记录的对"会系统性地高估簇的紧密度。
            scores.extend([0.0] * (expected - len(scores)))
        return sum(scores) / len(scores)

    def _evict_outliers(
        self,
        member_idx: list[int],
        pairs: list[_Pair],
        group: list[UnkCandidate],
        vectors: list[dict[str, float]],
    ) -> list[int]:
        """第二轮：把与簇均值不像的成员拆出去，缓解贪心合并的溢出。"""
        if len(member_idx) < 3:
            return member_idx
        scores: dict[int, list[float]] = {i: [] for i in member_idx}
        index = set(member_idx)
        for pair in pairs:
            if pair.i in index and pair.j in index:
                scores[pair.i].append(pair.score)
                scores[pair.j].append(pair.score)
        kept = [
            i
            for i in member_idx
            if scores[i] and sum(scores[i]) / len(scores[i]) >= self.evict_threshold
        ]
        return kept or member_idx

    def _singleton(self, cand: UnkCandidate, kind: str) -> UnkCluster:
        return UnkCluster(
            cluster_id=f"clu:{kind}:{cand.candidate_id.split(':')[-1]}",
            kind=kind,
            canonical=cand.text,
            members=[cand.text],
            member_ids=[cand.candidate_id],
            total_observations=cand.observations,
            cohesion=1.0,
            documents=list(cand.source_documents),
            sample_contexts=list(cand.contexts[:2]),
            hint_types=[cand.hint_type] if cand.hint_type else [],
        )

    def _build(
        self,
        member_idx: list[int],
        group: list[UnkCandidate],
        vectors: list[dict[str, float]],
        pairs: list[_Pair],
        kind: str,
    ) -> UnkCluster:
        members = [group[i] for i in member_idx]
        index = set(member_idx)
        internal = [p for p in pairs if p.i in index and p.j in index]

        centrality: dict[int, float] = {i: 0.0 for i in member_idx}
        for pair in internal:
            centrality[pair.i] += pair.score
            centrality[pair.j] += pair.score
        denom = max(1, len(member_idx) - 1)
        for i in member_idx:
            centrality[i] /= denom

        # 代表元：观测频次与簇内中心性都要高。纯按频次会选到"最常出现的错误
        # 写法"，纯按中心性会选到只在一条文档里出现过的孤例。频次取对数压一下
        # 量纲，否则高频词会无视形状直接胜出。
        def _strength(pos: int) -> float:
            cand = group[member_idx[pos]]
            return 0.45 * math.log1p(cand.observations) + 0.55 * centrality[member_idx[pos]]

        best_pos = max(range(len(members)), key=_strength)
        canonical = members[best_pos]

        documents: list[str] = []
        contexts: list[str] = []
        hints: list[str] = []
        for cand in members:
            for doc in cand.source_documents:
                if doc not in documents:
                    documents.append(doc)
            for ctx in cand.contexts:
                if ctx and ctx not in contexts:
                    contexts.append(ctx)
            if cand.hint_type and cand.hint_type not in hints:
                hints.append(cand.hint_type)

        cohesion = sum(p.score for p in internal) / len(internal) if internal else 1.0
        evidence = [
            {
                "pair": [group[p.i].text, group[p.j].text],
                "score": round(p.score, 4),
                "signals": {k: round(v, 4) for k, v in p.signals.items()},
            }
            for p in sorted(internal, key=lambda x: -x.score)[:6]
        ]
        return UnkCluster(
            cluster_id=f"clu:{kind}:{members[0].candidate_id.split(':')[-1]}",
            kind=kind,
            canonical=canonical.text,
            members=[c.text for c in members],
            member_ids=[c.candidate_id for c in members],
            total_observations=sum(c.observations for c in members),
            cohesion=cohesion,
            documents=documents,
            sample_contexts=contexts[:8],
            hint_types=hints,
            merge_evidence=evidence,
        )


__all__ = ["UnkCluster", "UnkClusterer", "MERGE_THRESHOLD", "KIND_ENTITY"]
