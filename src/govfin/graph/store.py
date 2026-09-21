"""三层属性图存储（SQLite 后端）。

选型理由：赛题要求"智能体基于 Nexent 平台可运行"，而 Nexent 是 Docker Compose
编排的。把图存储做成嵌入式（零外部服务、单文件、可随仓库分发）能保证智能体在
任何环境一键起来；同时通过 ``to_cypher()`` 导出，需要 Neo4j 演示时可直接灌库。

并发模型：每线程一个连接（``threading.local``），WAL 模式允许一写多读。
邻接缓存带代际号，写入即失效——避免多线程下读到陈旧邻接关系导致路径搜索丢边。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from govfin.config import GraphConfig, get_settings
from govfin.errors import GraphError, NodeNotFound, ValidationError
from govfin.graph.schema import (
    DEFAULT_EDGE_CONFIDENCE,
    DEFAULT_EVIDENCE_CLASS,
    EVIDENCE_DIRECT,
    LAYER_META,
    LAYER_NAMES,
    cross_layer_rule_for,
    is_cross_layer,
)
from govfin.ontology.model import Ontology
from govfin.ontology.seed import build_seed_ontology

# 冲突留痕用的保留键与上限。带双下划线前缀，避免与本体属性重名。
CONFLICT_KEY = "__冲突__"
MAX_CONFLICTS = 20

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    layer       INTEGER NOT NULL,
    ntype       TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    props       TEXT NOT NULL DEFAULT '{}',
    provenance  TEXT NOT NULL DEFAULT '{}',
    ontology_version TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_layer_type ON nodes(layer, ntype);
CREATE INDEX IF NOT EXISTS idx_nodes_label ON nodes(label);

CREATE TABLE IF NOT EXISTS edges (
    id          TEXT PRIMARY KEY,
    src         TEXT NOT NULL,
    dst         TEXT NOT NULL,
    etype       TEXT NOT NULL,
    layer       INTEGER NOT NULL,
    cross_layer INTEGER NOT NULL DEFAULT 0,
    props       TEXT NOT NULL DEFAULT '{}',
    evidence    TEXT NOT NULL DEFAULT '{}',
    confidence  REAL NOT NULL DEFAULT 1.0,
    evidence_class TEXT NOT NULL DEFAULT 'direct',
    ontology_version TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    UNIQUE(src, dst, etype, layer),
    FOREIGN KEY(src) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY(dst) REFERENCES nodes(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src, etype);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst, etype);
CREATE INDEX IF NOT EXISTS idx_edges_layer ON edges(layer, etype);
CREATE INDEX IF NOT EXISTS idx_edges_class ON edges(evidence_class);

CREATE TABLE IF NOT EXISTS ontology_versions (
    version     TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    rationale   TEXT NOT NULL DEFAULT '',
    actor       TEXT NOT NULL DEFAULT '',
    snapshot    TEXT NOT NULL,
    history     TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS llm_edge_feedback (
    etype       TEXT PRIMARY KEY,
    successes   INTEGER NOT NULL DEFAULT 0,
    failures    INTEGER NOT NULL DEFAULT 0
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(raw: str | None) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _merge_sources(provenance: dict, source: dict | None, *, cap: int = 20) -> dict:
    """把新来源追加进 provenance 的来源列表。

    节点的属性可能来自多份文档（企业基本信息在工商登记里、社保状态在社保记录里），
    而 ``provenance["first_seen"]`` 按定义只记得住第一份。少了这个列表，节点上
    的字段就无法回答"这个值是谁给的"——审计追到节点就断了。

    列表有上限（一个被反复引用的主体能攒出几十条），但**截断必须记数**。
    来源列表被截短而不做声，与"从来就只有这些来源"在读的人眼里是同一个样子，
    于是审计会拿着半份名单当成全部名单——这正是本文件其他部分反复要消灭的
    那类失效，只不过换到了来源信息自己身上。
    """
    if not source:
        return provenance
    out = dict(provenance or {})
    sources = list(out.get("sources") or [])
    entry = {k: v for k, v in source.items() if v not in (None, "")}
    if entry and entry not in sources:
        sources.append(entry)
    dropped = len(sources) - cap
    out["sources"] = sources[-cap:]
    if dropped > 0:
        out["sources_dropped"] = int(out.get("sources_dropped") or 0) + dropped
    return out


def _same_value(old: Any, new: Any) -> bool:
    """两个来源对同一字段的说法是不是同一件事。

    数值必须按数值比：抽取器会把"注册资本 5000"读成 int，OCR 读成 float，
    同一个事实于是有了 ``5000`` 和 ``5000.0`` 两个字面。若按字符串比，正常
    重导就会在节点上堆出一串并不存在的"冲突"，而冲突表一旦充满噪声，真正
    的那条冒用记录就淹没了——这正是留痕机制最容易被自己废掉的死法。
    """
    if isinstance(old, bool) or isinstance(new, bool):
        return old == new
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return float(old) == float(new)
    return str(old) == str(new)


class GraphStore:
    """三层属性图。"""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        ontology: Ontology | None = None,
        config: GraphConfig | None = None,
        in_memory: bool = False,
    ) -> None:
        self.config = config or get_settings().graph
        self.in_memory = in_memory
        if in_memory:
            self.db_path = ":memory:"
            # 内存库无法跨连接共享，只能用单一常驻连接 + 可重入锁
            self._shared_conn: sqlite3.Connection | None = self._connect_raw(":memory:")
            self._lock = threading.RLock()
        else:
            self.db_path = str(db_path or self.config.db_path)
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._shared_conn = None
            self._lock = threading.RLock()
        self._local = threading.local()
        self.ontology = ontology or build_seed_ontology()
        self._adjacency: dict[str, list[dict]] = defaultdict(list)
        self._reverse_adjacency: dict[str, list[dict]] = defaultdict(list)
        self._adjacency_generation = -1
        self._write_generation = 0
        self._init_schema()
        self._persist_ontology_version(self.ontology.version, rationale="初始化种子本体")

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def _connect_raw(self, path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(path, check_same_thread=False, timeout=self.config.busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={self.config.busy_timeout_ms}")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._shared_conn is not None:
            return self._shared_conn
        existing = getattr(self._local, "conn", None)
        if existing is None:
            existing = self._connect_raw(self.db_path)
            self._local.conn = existing
        return existing

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """一个写事务：成功即提交，**失败必回滚**。

        回滚不是保险动作，是这个存储层能不能用的分界线。sqlite3 的隐式事务在
        第一条 DML 时就地开启，之后一直挂在这条连接上，直到有人 commit 或
        rollback。所有写入路径原本都是"execute 然后 commit"，异常直接往外抛——
        于是任何一次失败的写入都会把**已开启的写事务留在连接上**，而 WAL 的写锁
        是库级的：这条连接不放手，别的连接一个字也写不进去。

        症状极具误导性。一次重复的节点写入（政务数据整表重发，最常见的操作）
        被拒之后，同进程其他线程（以及多 worker 部署下的其他进程）会在
        busy_timeout 耗尽后拿到 "database is locked"，报错现场与真正的起因
        隔着十万八千里，而且只有那条连接的持有者再写一次才会意外痊愈。

        因此写入一律走这里，别再手写 execute + commit：漏掉回滚的代价由整个
        数据库承担，而不是由写错这一行的人承担。
        """
        with self._lock:
            try:
                yield self.conn
            except BaseException:
                self.conn.rollback()
                raise
            self.conn.commit()

    def _fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """读全部行。内存库上必须与写互斥。

        文件库走每线程连接 + WAL，一条读语句拿到的是快照，看不到别人未提交的
        事务。内存库没有这条路——``:memory:`` 无法跨连接共享，所有线程只能用
        同一条连接，而共用连接意味着读者是**站在写者的事务里**读的，会直接
        看见批量写入的中间状态：一次 ``add_nodes_bulk`` 写到一半，读者能数出
        其中一部分节点，也能读到只有节点没有边的半张图。实测在四万节点的导入
        中，读者观察到过 2、4、7、17、38……2673 这样一串中间计数。若此时
        恰好重建了邻接表，那份残缺快照还会被盖上当前写代数的戳，之后一直是错
        的，直到下一次写入才偶然修好。

        所以只在共用连接时取锁：文件库路径的开销一行不加。返回 ``fetchall``
        而不是留一个惰性游标，是因为锁必须在游标读完之前一直持有——把游标交
        出去等于把锁提前还掉。语句级一致而非整个方法级一致：跨多条语句的
        快照（例如"节点数"与"边数"取自同一瞬间）在文件库上同样做不到，不在
        这里假装能做到。
        """
        if self._shared_conn is not None:
            with self._lock:
                return self.conn.execute(sql, params).fetchall()
        return self.conn.execute(sql, params).fetchall()

    def _fetch_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        """读单行。取锁规则同 ``_fetch_all``。"""
        if self._shared_conn is not None:
            with self._lock:
                return self.conn.execute(sql, params).fetchone()
        return self.conn.execute(sql, params).fetchone()

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(_SCHEMA_SQL)
            self.conn.commit()

    def close(self) -> None:
        if self._shared_conn is not None:
            self._shared_conn.close()
            self._shared_conn = None
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            existing.close()
            self._local.conn = None

    def __enter__(self) -> "GraphStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 邻接缓存
    # ------------------------------------------------------------------

    def _invalidate(self) -> None:
        self._write_generation += 1

    def _ensure_adjacency(self) -> None:
        if self._adjacency_generation == self._write_generation:
            return
        with self._lock:
            if self._adjacency_generation == self._write_generation:
                return
            forward: dict[str, list[dict]] = defaultdict(list)
            reverse: dict[str, list[dict]] = defaultdict(list)
            for row in self.conn.execute(
                "SELECT id, src, dst, etype, layer, cross_layer, confidence, evidence_class, "
                "props, evidence FROM edges"
            ):
                rec = dict(row)
                rec["props"] = _loads(rec["props"])
                rec["evidence"] = _loads(rec["evidence"])
                rec["cross_layer"] = bool(rec["cross_layer"])
                forward[rec["src"]].append(rec)
                reverse[rec["dst"]].append(rec)
            self._adjacency = forward
            self._reverse_adjacency = reverse
            self._adjacency_generation = self._write_generation

    def all_edges(self) -> list[dict]:
        """全量边。邻接表按 src 分桶，每条边只会出现在一个桶里，无需再去重。"""
        self._ensure_adjacency()
        return [edge for bucket in self._adjacency.values() for edge in bucket]

    def write_generation(self) -> int:
        """写代数。任何写操作都会 +1。

        供上层缓存做失效判断用：缓存索引的模块不该反过来被存储层通知，
        那会把存储层耦合到每一个缓存方；暴露一个单调递增的代数值，
        由缓存方自己比对，是耦合最小的做法。
        """
        return self._write_generation

    def adjacency_stats(self) -> dict:
        self._ensure_adjacency()
        return {
            "generation": self._adjacency_generation,
            "distinct_sources": len(self._adjacency),
            "distinct_targets": len(self._reverse_adjacency),
        }

    # ------------------------------------------------------------------
    # 写入：节点
    # ------------------------------------------------------------------

    def add_node(
        self,
        ntype: str,
        *,
        layer: int | None = None,
        label: str | None = None,
        props: dict | None = None,
        provenance: dict | None = None,
        node_id: str | None = None,
        source: dict | None = None,
        validate: bool = True,
    ) -> str:
        cls = self.ontology.get_class(ntype)
        if cls is None:
            raise ValidationError(f"类型 '{ntype}' 未在本体 {self.ontology.version} 中定义")
        resolved_layer = layer if layer is not None else (cls.layer if cls.layer is not None else LAYER_META)

        payload = dict(props or {})
        if node_id is not None:
            payload.setdefault("id", node_id)
        if validate:
            violations = self.ontology.validate_instance(ntype, payload)
            hard = [v for v in violations if v.severity == "Violation"]
            if hard:
                raise ValidationError(
                    f"节点不满足 SHACL 约束: {hard[0].message}",
                    detail={"violations": [v.to_dict() for v in violations]},
                )

        nid = node_id or self._make_id(ntype, payload)
        now = _now()
        provenance = _merge_sources(provenance or {}, source)
        try:
            with self._write() as conn:
                conn.execute(
                    "INSERT INTO nodes(id, layer, ntype, label, props, provenance, ontology_version, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        nid,
                        resolved_layer,
                        ntype,
                        label or payload.get("名称") or payload.get("姓名") or nid,
                        _dumps(payload),
                        _dumps(provenance or {}),
                        self.ontology.version,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise GraphError(f"节点 '{nid}' 已存在") from exc
        self._invalidate()
        return nid

    def upsert_node(self, ntype: str, **kwargs) -> str:
        """幂等写入：已存在则合并属性，不存在则创建。导入管道的主力接口。

        "不存在则创建"这一步必须在锁内完成。曾经是先在外面 ``get_node`` 判空、
        再进锁写入，两个线程同时判到"不存在"就会有一个撞上主键冲突并抛
        ``GraphError``——而"幂等"的承诺正是"重复写入不报错"。政务数据整表重发、
        多 worker 同时导入同一份文件，都会走到这条路上。
        """
        payload = dict(kwargs.get("props") or {})
        node_id = kwargs.get("node_id") or self._make_id(ntype, payload)

        with self._lock:
            existing = self.get_node(node_id)
            if existing is None:
                kwargs["node_id"] = node_id
                return self.add_node(ntype, **kwargs)

            merged = {**existing["props"], **payload}
            merged["id"] = node_id
            provenance = _merge_sources(existing["provenance"], kwargs.get("source"))
            with self._write() as conn:
                conn.execute(
                    "UPDATE nodes SET props=?, label=?, provenance=?, updated_at=?, ontology_version=? WHERE id=?",
                    (
                        _dumps(merged),
                        kwargs.get("label") or existing["label"],
                        _dumps(provenance),
                        _now(),
                        self.ontology.version,
                        node_id,
                    ),
                )
        self._invalidate()
        return node_id

    def add_nodes_bulk(self, records: Sequence[dict]) -> list[str]:
        """批量写入，单事务。导入大图谱时比逐条 add_node 快一个数量级。"""
        ids: list[str] = []
        now = _now()
        rows = []
        for rec in records:
            ntype = rec["ntype"]
            cls = self.ontology.get_class(ntype)
            if cls is None:
                raise ValidationError(f"类型 '{ntype}' 未定义")
            payload = dict(rec.get("props") or {})
            nid = rec.get("node_id") or self._make_id(ntype, payload)
            payload["id"] = nid
            ids.append(nid)
            rows.append(
                (
                    nid,
                    rec.get("layer", cls.layer if cls.layer is not None else LAYER_META),
                    ntype,
                    rec.get("label") or payload.get("名称") or payload.get("姓名") or nid,
                    _dumps(payload),
                    _dumps(rec.get("provenance") or {}),
                    self.ontology.version,
                    now,
                    now,
                )
            )
        with self._write() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO nodes(id, layer, ntype, label, props, provenance, ontology_version, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                rows,
            )
        self._invalidate()
        return ids

    def get_node(self, node_id: str) -> dict | None:
        row = self._fetch_one("SELECT * FROM nodes WHERE id=?", (node_id,))
        if row is None:
            return None
        return self._node_row_to_dict(row)

    def require_node(self, node_id: str) -> dict:
        node = self.get_node(node_id)
        if node is None:
            raise NodeNotFound(f"节点 '{node_id}' 不存在")
        return node

    def query_nodes(
        self,
        *,
        layer: int | None = None,
        ntype: str | None = None,
        label_like: str | None = None,
        prop_filters: dict | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM nodes WHERE 1=1"
        params: list[Any] = []
        if layer is not None:
            sql += " AND layer=?"
            params.append(layer)
        if ntype is not None:
            sql += " AND ntype=?"
            params.append(ntype)
        if label_like:
            sql += " AND label LIKE ?"
            params.append(f"%{label_like}%")
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        rows = [self._node_row_to_dict(r) for r in self._fetch_all(sql, params)]
        if prop_filters:
            rows = [r for r in rows if all(r["props"].get(k) == v for k, v in prop_filters.items())]
        return rows

    def find_by_prop(self, key: str, value: Any, *, ntype: str | None = None, limit: int = 50) -> list[dict]:
        """按属性查节点。SQLite 的 json_extract 让这一步走索引外扫描但不解析全表到 Python。"""
        sql = "SELECT * FROM nodes WHERE json_extract(props, ?) = ?"
        params: list[Any] = [f"$.{key}", value]
        if ntype:
            sql += " AND ntype=?"
            params.append(ntype)
        sql += " LIMIT ?"
        params.append(limit)
        try:
            return [self._node_row_to_dict(r) for r in self._fetch_all(sql, params)]
        except sqlite3.OperationalError:
            # 老版本 SQLite 无 JSON1 扩展时退化为全表扫描
            return [
                r
                for r in self.query_nodes(ntype=ntype, limit=limit * 100)
                if r["props"].get(key) == value
            ][:limit]

    def update_node_props(
        self,
        node_id: str,
        patch: dict,
        *,
        merge: bool = True,
        source: dict | None = None,
    ) -> dict:
        """改属性。**新值获胜**——这是系统自己认定的更新，不是另一个来源的主张。

        "新值获胜"和"保留先到的值"是两种不同的意图，必须分清楚：
        ``update_node_props`` 是前者（本体演化标记提案状态、导入时字段类型矫正、
        审计测试里修正上游数据），调用方明确知道要让字段变成什么；
        ``merge_from_source`` 是后者，用在"另一份文档对同一个实体给出了不同的
        说法"这个场景。把两种意图混成一个方法，就会让真实的更新变成静默失效。
        """
        node = self.require_node(node_id)
        props = {**node["props"], **patch} if merge else dict(patch)
        provenance = _merge_sources(node["provenance"], source)
        with self._write() as conn:
            conn.execute(
                "UPDATE nodes SET props=?, provenance=?, updated_at=? WHERE id=?",
                (_dumps(props), _dumps(provenance), _now(), node_id),
            )
        self._invalidate()
        return self.require_node(node_id)

    def merge_from_source(self, node_id: str, patch: dict, *, source: dict | None = None) -> dict:
        """合并**另一份来源文档**对同一实体的描述，并对字段冲突留痕。

        静默覆盖是多源数据里最难查的一类问题：企业更名、不同统筹区的字段口径
        不一致，或者一份文件冒用了别家的统一社会信用代码——最终都表现为某个字段
        悄悄变成了后写入的值，而节点上的来源信息还指着第一份文件。决策链于是
        引用着一段自己都说不清出处的属性。

        取值取保守的一侧：**保留先到的值**，把后到的值与来源记进 ``__冲突__``。
        这个系统是为审计做的，一个"没变"的字段比一个"变了但只有一个人知道"的
        字段容易解释得多；冲突表让两个值都还在，信息一点没丢。审计的漂移检测会
        把 ``__冲突__`` 的变化报出来，因此"有人试图改这个字段"本身也是可见的。
        """
        node = self.require_node(node_id)
        existing = node["props"]
        merged: dict[str, Any] = {
            k: v for k, v in existing.items() if k not in ("id", CONFLICT_KEY)
        }
        conflicts = list(existing.get(CONFLICT_KEY) or [])
        for key, new in patch.items():
            if key in ("id", CONFLICT_KEY) or new in (None, ""):
                continue
            old = existing.get(key)
            if old in (None, "") or _same_value(old, new):
                merged[key] = new
                continue
            entry = {"字段": key, "保留值": old, "另一来源的值": new, "来源": source or {}}
            # 同一份文档里同一个字段会经两条路径各合并一次（建节点的 props 一条、
            # 数据属性一条），去重才不至于让一处冲突在表里出现两遍。冲突表一旦
            # 开始成倍膨胀，读者就会开始怀疑它，然后忽略它。
            if entry not in conflicts and len(conflicts) < MAX_CONFLICTS:
                conflicts.append(entry)
        if conflicts:
            merged[CONFLICT_KEY] = conflicts
        merged["id"] = node_id
        return self.update_node_props(node_id, merged, merge=False, source=source)

    def delete_node(self, node_id: str) -> int:
        with self._write() as conn:
            cur = conn.execute("DELETE FROM nodes WHERE id=?", (node_id,))
        self._invalidate()
        return cur.rowcount

    # ------------------------------------------------------------------
    # 写入：边
    # ------------------------------------------------------------------

    def add_edge(
        self,
        src: str,
        dst: str,
        etype: str,
        *,
        layer: int | None = None,
        props: dict | None = None,
        evidence: dict | None = None,
        confidence: float | None = None,
        evidence_class: str | None = None,
        edge_id: str | None = None,
        validate: bool = True,
    ) -> str:
        src_node = self.require_node(src)
        dst_node = self.require_node(dst)

        if validate:
            self._validate_edge(src_node, dst_node, etype)

        cls = evidence_class or DEFAULT_EVIDENCE_CLASS.get(etype, EVIDENCE_DIRECT)
        conf = DEFAULT_EDGE_CONFIDENCE.get(etype, 1.0) if confidence is None else confidence
        if not 0.0 <= conf <= 1.0:
            raise ValidationError(f"边置信度必须落在 [0,1]，收到 {conf}")

        cross = is_cross_layer(src_node["layer"], dst_node["layer"])
        resolved_layer = layer if layer is not None else (dst_node["layer"] if cross else src_node["layer"])

        eid = edge_id or f"e:{src}|{etype}|{dst}"
        try:
            with self._write() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO edges(id, src, dst, etype, layer, cross_layer, props, evidence, "
                    "confidence, evidence_class, ontology_version, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        eid,
                        src,
                        dst,
                        etype,
                        resolved_layer,
                        int(cross),
                        _dumps(props or {}),
                        _dumps(evidence or {}),
                        float(conf),
                        cls,
                        self.ontology.version,
                        _now(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise GraphError(f"边写入失败 {src} -[{etype}]-> {dst}: {exc}") from exc
        self._invalidate()
        return eid

    def add_edges_bulk(self, records: Sequence[dict]) -> list[str]:
        """批量写边，单事务。导入大图谱时比逐条 add_edge 快一个数量级。

        **必须做和 ``add_edge`` 一样的结构校验**。这里一度跳过了校验，理由是
        "批量路径要快"，但那是错的：单条写入会拒绝的非法跨层边，走批量路径就
        静默进了图。症状出现在很远的地方——``integrity_report`` 报出一批
        illegal_cross_layer_edges，而那时已经没人知道是哪一次导入写进去的。
        两条写入路径对同一条边的接受与否必须一致，否则"结构不变量"就只是
        建议而不是约束。校验所需的节点在上面的存在性检查里已经取到，成本只是
        几次字典查找和父类链遍历。
        """
        now = _now()
        rows = []
        ids = []
        known: dict[str, dict] = {}
        for rec in records:
            src, dst = rec["src"], rec["dst"]
            for nid in (src, dst):
                if nid not in known:
                    node = self.get_node(nid)
                    if node is None:
                        raise NodeNotFound(f"边引用了不存在的节点 '{nid}'")
                    known[nid] = node
            etype = rec["etype"]
            src_node = known[src]
            dst_node = known[dst]
            self._validate_edge(src_node, dst_node, etype)
            cross = is_cross_layer(src_node["layer"], dst_node["layer"])
            eid = rec.get("edge_id") or f"e:{src}|{etype}|{dst}"
            ids.append(eid)
            rows.append(
                (
                    eid,
                    src,
                    dst,
                    etype,
                    rec.get("layer", dst_node["layer"] if cross else src_node["layer"]),
                    int(cross),
                    _dumps(rec.get("props") or {}),
                    _dumps(rec.get("evidence") or {}),
                    float(rec.get("confidence", DEFAULT_EDGE_CONFIDENCE.get(etype, 1.0))),
                    rec.get("evidence_class") or DEFAULT_EVIDENCE_CLASS.get(etype, EVIDENCE_DIRECT),
                    self.ontology.version,
                    now,
                )
            )
        with self._write() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO edges(id, src, dst, etype, layer, cross_layer, props, evidence, "
                "confidence, evidence_class, ontology_version, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        self._invalidate()
        return ids

    def _validate_edge(self, src_node: dict, dst_node: dict, etype: str) -> None:
        prop = self.ontology.properties.get(etype)
        if prop is None:
            raise ValidationError(f"边类型 '{etype}' 未在本体中定义为属性")

        if not self.ontology.is_subclass_of(src_node["ntype"], prop.domain):
            raise ValidationError(
                f"边 '{etype}' 的 domain 是 '{prop.domain}'，但源节点是 '{src_node['ntype']}'",
                detail={"src": src_node["id"], "domain": prop.domain},
            )
        if prop.kind == "object" and not self.ontology.is_subclass_of(dst_node["ntype"], prop.range):
            raise ValidationError(
                f"边 '{etype}' 的 range 是 '{prop.range}'，但目标节点是 '{dst_node['ntype']}'",
                detail={"dst": dst_node["id"], "range": prop.range},
            )

        src_layer, dst_layer = src_node["layer"], dst_node["layer"]
        if is_cross_layer(src_layer, dst_layer):
            if cross_layer_rule_for(src_layer, dst_layer, etype) is None:
                raise ValidationError(
                    f"跨层边 {LAYER_NAMES.get(src_layer, src_layer)} → "
                    f"{LAYER_NAMES.get(dst_layer, dst_layer)} 不允许使用边类型 '{etype}'，"
                    "请检查 graph.schema.CROSS_LAYER_RULES",
                    detail={"src_layer": src_layer, "dst_layer": dst_layer, "etype": etype},
                )

    def get_edge(self, edge_id: str) -> dict | None:
        row = self._fetch_one("SELECT * FROM edges WHERE id=?", (edge_id,))
        return self._edge_row_to_dict(row) if row else None

    def find_edges(self, *, src: str | None = None, dst: str | None = None, etype: str | None = None) -> list[dict]:
        sql = "SELECT * FROM edges WHERE 1=1"
        params: list[Any] = []
        for col, val in (("src", src), ("dst", dst), ("etype", etype)):
            if val is not None:
                sql += f" AND {col}=?"
                params.append(val)
        return [self._edge_row_to_dict(r) for r in self._fetch_all(sql, params)]

    def delete_edge(self, edge_id: str) -> int:
        with self._write() as conn:
            cur = conn.execute("DELETE FROM edges WHERE id=?", (edge_id,))
        self._invalidate()
        return cur.rowcount

    # ------------------------------------------------------------------
    # 邻接遍历
    # ------------------------------------------------------------------

    def neighbors(
        self,
        node_id: str,
        *,
        direction: str = "out",
        etypes: Iterable[str] | None = None,
        layers: Iterable[int] | None = None,
        min_confidence: float = 0.0,
    ) -> list[dict]:
        self._ensure_adjacency()
        allow_types = set(etypes) if etypes is not None else None
        allow_layers = set(layers) if layers is not None else None

        results: list[dict] = []
        pools: list[list[dict]] = []
        if direction in ("out", "both"):
            pools.append(self._adjacency.get(node_id, []))
        if direction in ("in", "both"):
            pools.append(self._reverse_adjacency.get(node_id, []))

        for pool in pools:
            for rec in pool:
                if allow_types is not None and rec["etype"] not in allow_types:
                    continue
                if allow_layers is not None and rec["layer"] not in allow_layers:
                    continue
                if rec["confidence"] < min_confidence:
                    continue
                results.append(rec)
        return results

    def neighbors_within_hops(
        self, node_id: str, hops: int, *, etypes: Iterable[str] | None = None, direction: str = "out"
    ) -> dict[str, int]:
        """用递归 CTE 在数据库内做多跳可达性计算，只返回距离不展开路径。

        适合"3 跳内有哪些关联方"这类只需要节点集合的场景——比在 Python 里
        构造完整路径便宜得多。
        """
        if hops < 1:
            return {}
        type_clause = ""
        params: list[Any] = [node_id]
        if etypes is not None:
            types = list(etypes)
            if not types:
                return {}
            type_clause = f" AND etype IN ({','.join('?' * len(types))})"
            params.extend(types)
        params.append(hops)

        if direction == "out":
            anchor, join_col, next_col = "src", "src", "dst"
        elif direction == "in":
            anchor, join_col, next_col = "dst", "dst", "src"
        else:
            raise ValidationError("neighbors_within_hops 的 direction 只支持 out / in")

        sql = f"""
        WITH RECURSIVE reach(node, depth) AS (
            SELECT {next_col}, 1 FROM edges WHERE {anchor} = ? {type_clause}
            UNION
            SELECT e.{next_col}, r.depth + 1
            FROM edges e JOIN reach r ON e.{join_col} = r.node
            WHERE r.depth < ? {type_clause.replace('etype', 'e.etype') if type_clause else ''}
        )
        SELECT node, MIN(depth) AS d FROM reach WHERE node != ? GROUP BY node
        """
        params.append(node_id)
        try:
            rows = self._fetch_all(sql, params)
        except sqlite3.OperationalError as exc:
            raise GraphError(f"多跳可达查询失败: {exc}") from exc
        return {r["node"]: r["d"] for r in rows}

    def cross_layer_edges(self) -> list[dict]:
        return [
            self._edge_row_to_dict(r)
            for r in self._fetch_all("SELECT * FROM edges WHERE cross_layer=1")
        ]

    # ------------------------------------------------------------------
    # 统计 / 完整性
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        node_total = self._fetch_one("SELECT COUNT(*) c FROM nodes")["c"]
        edge_total = self._fetch_one("SELECT COUNT(*) c FROM edges")["c"]
        by_layer = {
            r["layer"]: {"nodes": r["n"], "edges": r["e"]}
            for r in self._fetch_all(
                """
                SELECT l.layer AS layer,
                       (SELECT COUNT(*) FROM nodes WHERE layer=l.layer) AS n,
                       (SELECT COUNT(*) FROM edges WHERE layer=l.layer) AS e
                FROM (SELECT DISTINCT layer FROM nodes UNION SELECT DISTINCT layer FROM edges) l
                """
            )
        }
        by_ntype = {
            r["ntype"]: r["c"]
            for r in self._fetch_all(
                "SELECT ntype, COUNT(*) c FROM nodes GROUP BY ntype ORDER BY c DESC"
            )
        }
        by_etype = {
            r["etype"]: r["c"]
            for r in self._fetch_all(
                "SELECT etype, COUNT(*) c FROM edges GROUP BY etype ORDER BY c DESC"
            )
        }
        by_class = {
            r["evidence_class"]: r["c"]
            for r in self._fetch_all(
                "SELECT evidence_class, COUNT(*) c FROM edges GROUP BY evidence_class"
            )
        }
        return {
            "nodes": node_total,
            "edges": edge_total,
            "by_layer": {LAYER_NAMES.get(k, str(k)): v for k, v in sorted(by_layer.items())},
            "by_node_type": by_ntype,
            "by_edge_type": by_etype,
            "by_evidence_class": by_class,
            "cross_layer_edges": len(self.cross_layer_edges()),
            "ontology_version": self.ontology.version,
        }

    def integrity_report(self) -> dict:
        """结构完整性体检。暴力测试和 CI 都靠它兜底。"""
        problems: list[dict] = []

        orphans = self._fetch_one(
            "SELECT COUNT(*) c FROM edges e WHERE NOT EXISTS (SELECT 1 FROM nodes n WHERE n.id=e.src) "
            "OR NOT EXISTS (SELECT 1 FROM nodes n WHERE n.id=e.dst)"
        )["c"]
        if orphans:
            problems.append({"type": "orphan_edges", "count": orphans})

        dangling_parent = self._fetch_one(
            "SELECT COUNT(*) c FROM edges e JOIN nodes d ON d.id=e.dst "
            "WHERE e.cross_layer=0 AND e.layer != d.layer"
        )["c"]
        if dangling_parent:
            problems.append({"type": "same_layer_edge_across_layers", "count": dangling_parent})

        bad_conf = self._fetch_one(
            "SELECT COUNT(*) c FROM edges WHERE confidence < 0 OR confidence > 1"
        )["c"]
        if bad_conf:
            problems.append({"type": "confidence_out_of_range", "count": bad_conf})

        illegal_cross = 0
        for edge in self.cross_layer_edges():
            src = self.get_node(edge["src"])
            dst = self.get_node(edge["dst"])
            if src is None or dst is None:
                illegal_cross += 1
                continue
            if cross_layer_rule_for(src["layer"], dst["layer"], edge["etype"]) is None:
                illegal_cross += 1
        if illegal_cross:
            problems.append({"type": "illegal_cross_layer_edges", "count": illegal_cross})

        untyped = 0
        for row in self._fetch_all("SELECT ntype FROM nodes GROUP BY ntype"):
            if self.ontology.get_class(row["ntype"]) is None or self.ontology.resolve_alias(row["ntype"]) is None:
                untyped += 1
        if untyped:
            problems.append({"type": "node_types_not_in_ontology", "count": untyped})

        return {
            "healthy": not problems,
            "problems": problems,
            "structure_check": self._fetch_one("PRAGMA integrity_check")[0],
            "foreign_key_check": len(self._fetch_all("PRAGMA foreign_key_check")),
        }

    # ------------------------------------------------------------------
    # 本体版本
    # ------------------------------------------------------------------

    def _persist_ontology_version(self, version: str, *, rationale: str = "", actor: str = "system") -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ontology_versions(version, created_at, rationale, actor, snapshot, history) "
                "VALUES(?,?,?,?,?,?)",
                (
                    version,
                    _now(),
                    rationale,
                    actor,
                    self.ontology.to_json(),
                    _dumps(self.ontology.history),
                ),
            )

    def record_ontology_version(self, version: str, *, rationale: str = "", actor: str = "arbiter") -> None:
        self._persist_ontology_version(version, rationale=rationale, actor=actor)

    def ontology_version_history(self) -> list[dict]:
        return [
            {
                "version": r["version"],
                "created_at": r["created_at"],
                "rationale": r["rationale"],
                "actor": r["actor"],
            }
            for r in self._fetch_all(
                "SELECT version, created_at, rationale, actor FROM ontology_versions ORDER BY created_at DESC"
            )
        ]

    # ------------------------------------------------------------------
    # LLM 边反馈（置信度动态校准）
    # ------------------------------------------------------------------

    def record_edge_feedback(self, etype: str, *, success: bool) -> None:
        col = "successes" if success else "failures"
        with self._write() as conn:
            conn.execute(
                f"INSERT INTO llm_edge_feedback(etype, {col}) VALUES(?, 1) "
                f"ON CONFLICT(etype) DO UPDATE SET {col} = {col} + 1",
                (etype,),
            )

    def edge_feedback(self, etype: str) -> tuple[int, int]:
        row = self._fetch_one(
            "SELECT successes, failures FROM llm_edge_feedback WHERE etype=?", (etype,)
        )
        return (row["successes"], row["failures"]) if row else (0, 0)

    def all_edge_feedback(self) -> dict[str, tuple[int, int]]:
        return {
            r["etype"]: (r["successes"], r["failures"])
            for r in self._fetch_all("SELECT etype, successes, failures FROM llm_edge_feedback")
        }

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------

    def to_cypher(self, *, include_runtime: bool = True) -> str:
        """导出为 Neo4j Cypher，供需要图数据库演示时一次性灌库。"""
        lines: list[str] = [
            "// GovFin 三层图导出 — 由 GraphStore.to_cypher() 生成",
            f"// ontology_version: {self.ontology.version}",
            "CREATE CONSTRAINT govfin_node_id IF NOT EXISTS FOR (n:GovFin) REQUIRE n.id IS UNIQUE;",
            "CREATE CONSTRAINT govfin_edge_id IF NOT EXISTS FOR ()-[r:GOVFIN]-() REQUIRE r.id IS UNIQUE;",
            "",
        ]
        prop_names = {p.name for p in self.ontology.properties.values()}
        for node in self.query_nodes():
            if not include_runtime and node["layer"] == 3:
                continue
            props = {k: v for k, v in node["props"].items() if k != "id"}
            props["id"] = node["id"]
            props["_ntype"] = node["ntype"]
            props["_layer"] = node["layer"]
            props["_label"] = node["label"]
            lines.append(f"MERGE (n:GovFin:{_cypher_ident(node['ntype'])} {{id: {_cypher_val(node['id'])}}})")
            lines.append(f"  SET n += {_cypher_map(props)};")

        for edge in self.find_edges():
            if not include_runtime and edge["layer"] == 3:
                continue
            props = {
                "id": edge["id"],
                "etype": edge["etype"],
                "confidence": edge["confidence"],
                "evidence_class": edge["evidence_class"],
                "cross_layer": edge["cross_layer"],
            }
            lines.append(
                f"MATCH (a:GovFin {{id: {_cypher_val(edge['src'])}}}), (b:GovFin {{id: {_cypher_val(edge['dst'])}}})"
            )
            lines.append(
                f"  MERGE (a)-[r:{_cypher_ident(edge['etype'])} {{id: {_cypher_val(edge['id'])}}}]->(b)"
            )
            lines.append(f"  SET r += {_cypher_map(props)};")
        return "\n".join(lines)

    def snapshot_edges(self) -> list[dict]:
        return [self._edge_row_to_dict(r) for r in self._fetch_all("SELECT * FROM edges")]

    def clear(self, *, layer: int | None = None) -> None:
        with self._write() as conn:
            if layer is None:
                conn.execute("DELETE FROM edges")
                conn.execute("DELETE FROM nodes")
            else:
                conn.execute(
                    "DELETE FROM edges WHERE src IN (SELECT id FROM nodes WHERE layer=?) "
                    "OR dst IN (SELECT id FROM nodes WHERE layer=?)",
                    (layer, layer),
                )
                conn.execute("DELETE FROM nodes WHERE layer=?", (layer,))
        self._invalidate()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _make_id(self, ntype: str, props: dict) -> str:
        natural = (
            props.get("统一社会信用代码")
            or props.get("记录编号")
            or props.get("条款编号")
            or props.get("决策编号")
            or props.get("提案编号")
            or props.get("姓名")
            or props.get("名称")
            or props.get("产品名称")
        )
        if natural:
            safe = str(natural).strip().replace("|", "/")[:120]
            return f"{ntype}:{safe}"
        return f"{ntype}:{uuid.uuid4().hex[:16]}"

    def _node_row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["props"] = _loads(d["props"])
        d["provenance"] = _loads(d["provenance"])
        return d

    def _edge_row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["props"] = _loads(d["props"])
        d["evidence"] = _loads(d["evidence"])
        d["cross_layer"] = bool(d["cross_layer"])
        return d

    def iter_nodes(self, batch_size: int = 1000) -> Iterator[list[dict]]:
        offset = 0
        while True:
            rows = [
                self._node_row_to_dict(r)
                for r in self._fetch_all(
                    "SELECT * FROM nodes LIMIT ? OFFSET ?", (batch_size, offset)
                )
            ]
            if not rows:
                return
            yield rows
            offset += batch_size


def _cypher_ident(name: str) -> str:
    """把任意中文类型名转成合法的 Cypher 标签/关系类型。"""
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"T_{cleaned}"
    return cleaned


def _cypher_val(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _cypher_map(props: dict) -> str:
    parts = [f"{_cypher_ident(k)}: {_cypher_val(v)}" for k, v in props.items()]
    return "{" + ", ".join(parts) + "}"
