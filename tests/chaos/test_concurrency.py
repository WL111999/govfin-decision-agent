"""并发故障注入：把"偶尔才发生一次"的那些时序缺陷逼出来。

并发缺陷和前面几类故障有个本质区别：**它们不可复现**。单线程跑一万遍都看不到，
线上在某个午后的负载高峰出现一次，日志里只留下一句驴唇不对马嘴的报错。因此
这里的做法不是"多跑几遍碰运气"，而是针对每一类时序缺陷构造一个**必然触发**的
场景——只要缺陷还在，测试就一定会红。

本文件盯住五类：

1. **脏读**。内存库所有线程共用一条连接（``:memory:`` 无法跨连接共享），读者
   会站在写者未提交的事务里读，看见批量导入的中间状态。
2. **事务泄漏**。写失败若不回滚，开着的写事务会一直挂在那条连接上，而 WAL 的
   写锁是库级的——一次被拒的重复写入，能让**整个服务**在此之后写不进任何数据。
3. **check-then-act 竞态**。先查后写中间没有锁，两个线程同时判到"不存在"，
   其中一个必然撞主键冲突。
4. **审计记录被静默覆盖**。决策编号只按秒取盐，同一秒内对同一主体的两次决策
   会拿到同一个编号，而溯源写入是按编号幂等的——后一次会把前一次整个盖掉。
5. **缓存盖错代际戳**。邻接表是全量重建的，若重建期间读到了中间状态却仍然
   盖上"已是最新"的戳，那份残缺快照会一直用到下一次写入为止。

有一条边界要说清楚：``stats()`` / ``integrity_report()`` 这类跨多条语句的读，
只保证每条语句自身一致，不保证几条语句取自同一瞬间——文件库上做不到，这里
也不假装能做到。测试因此只断言语句级一致。
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from govfin.errors import GraphError
from govfin.graph.store import GraphStore
from govfin.ontology.seed import build_seed_ontology
from govfin.runtime import AgentRuntime
from govfin.tools import DomainTools

pytestmark = pytest.mark.chaos

SUBJECTS = ("甲科技有限公司", "乙贸易有限公司", "丙建材有限公司", "上海某钢材贸易有限公司")


def _run(workers) -> list:
    """并发跑一批线程并把每个线程的返回值收回来。

    用 ``join(timeout)`` 而不是无限等：死锁的表现是测试永远挂着，而挂着不产生
    任何信息——没人知道是死锁、是慢，还是测试框架的问题。超时后断言失败，
    至少能指出"卡在并发这一步"。
    """
    results: list = [None] * len(workers)
    errors: list = []

    def wrapper(index: int, fn) -> None:
        try:
            results[index] = fn()
        except BaseException as exc:  # noqa: BLE001 - 线程里的异常不测出来就会静默消失
            errors.append(exc)

    threads = [threading.Thread(target=wrapper, args=(i, fn)) for i, fn in enumerate(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    alive = [t for t in threads if t.is_alive()]
    assert not alive, f"{len(alive)} 个线程 60 秒内没有结束，并发路径上存在死锁或无界等待"
    assert not errors, f"并发执行中抛出异常: {[repr(e) for e in errors]}"
    return results


# ----------------------------------------------------------------------
# 脏读
# ----------------------------------------------------------------------


def test_shared_connection_never_exposes_uncommitted_rows():
    """批量写入进行到一半时，读者不得看见任何中间状态。

    内存库上读者与写者共用一条连接。不加互斥时，一次两万节点的导入会让读者数出
    2、4、7、17、38……2673 这样一串中间计数——每一个都是"某次导入导入到一半的
    图"。这类脏读最坏的结果不是数字难看，而是有人基于半张图作出了决策。

    断言写成"读到的计数只能是前后两个已提交状态"，而不是"没有报错"：脏读本身
    不报错，它只是给出一个错的答案。
    """
    store = GraphStore(in_memory=True, ontology=build_seed_ontology())
    before = len(store.query_nodes(ntype="企业", limit=None))

    counts: list[int] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            counts.append(len(store.query_nodes(ntype="企业", limit=None)))
            time.sleep(0.001)

    t = threading.Thread(target=reader)
    t.start()
    time.sleep(0.02)  # 让读者先采到"写之前"
    store.add_nodes_bulk(
        [
            {"ntype": "企业", "props": {"名称": f"并发导入企业{i:05d}", "统一社会信用代码": f"91ZZ{i:012d}"}}
            for i in range(20_000)
        ]
    )
    time.sleep(0.02)  # 再采一次"写之后"
    stop.set()
    t.join(timeout=10)
    after = len(store.query_nodes(ntype="企业", limit=None))

    assert counts, "读者一次都没采到，这条断言什么都证明不了"
    assert before in counts and after in counts, (
        f"读者没有同时观察到写入前后两个状态（{counts[:5]}...），采样窗口不成立"
    )
    stray = sorted({c for c in counts if c not in (before, after)})
    assert not stray, f"读者观察到了未提交的中间状态: {stray[:10]}（这是脏读，不是并发容忍度问题）"
    store.close()


# ----------------------------------------------------------------------
# 事务泄漏
# ----------------------------------------------------------------------


def test_rejected_write_does_not_leave_a_transaction_open(tmp_path):
    """被拒绝的写入必须回滚，不得把写事务留在连接上。

    这是本文件里后果最严重的一条。sqlite3 的隐式事务在第一条 DML 时就地开启，
    只有 commit 或 rollback 才结束；WAL 的写锁是**库级**的。因此一次重复的节点
    写入被拒之后，别的连接会一路等到 busy_timeout 耗尽、然后拿到
    "database is locked"——报错出现在另一个线程、另一个操作上，与真正的起因
    隔着十万八千里。

    "重复写入"在政务场景里不是异常，是日常：整表重发、多份文件引用同一家企业、
    断点重导。所以这条路径必须干净。
    """
    db = tmp_path / "leak.db"
    store = GraphStore(db_path=str(db), ontology=build_seed_ontology())
    store.add_node("企业", props={"名称": "重复上报企业", "统一社会信用代码": "91LEAK000000000001"})

    with pytest.raises(GraphError):
        store.add_node("企业", props={"名称": "重复上报企业", "统一社会信用代码": "91LEAK000000000001"})

    assert store.conn.in_transaction is False, "被拒的写入把事务留在了连接上，写锁不会释放"

    other = sqlite3.connect(str(db), timeout=1.0)
    try:
        other.execute("PRAGMA busy_timeout=1000")
        other.execute(
            "INSERT INTO nodes(id, layer, ntype, label, props, provenance, ontology_version, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("企业:另一个写入方", 1, "企业", "另一个写入方", "{}", "{}", "1", "x", "x"),
        )
        other.commit()
    except sqlite3.OperationalError as exc:  # pragma: no cover - 出问题时给出可读证据
        raise AssertionError(f"另一个连接写不进去（{exc}），说明写锁仍被泄漏的事务持有") from exc
    finally:
        other.close()
        store.close()


def test_concurrent_upsert_of_same_entity_stays_idempotent(tmp_path):
    """多线程同时 upsert 同一实体：不得抛异常，且只应有一个节点。

    "幂等"是 ``upsert_node`` 对外的承诺，而先查后写的结构会让它在并发下变成
    "重复写入时报主键冲突"。这在多 worker 部署下是常态而非巧合：两个进程同时
    开始导入同一份政务文件，第一批记录就撞在一起。
    """
    store = GraphStore(db_path=str(tmp_path / "upsert.db"), ontology=build_seed_ontology())
    props = {"名称": "并发导入同一企业", "统一社会信用代码": "91RACE000000000001"}

    results = _run([lambda: store.upsert_node("企业", props=dict(props)) for _ in range(12)])

    assert len(set(results)) == 1, f"同一自然键产生了多个节点 ID: {sorted(set(results))}"
    assert store.stats()["nodes"] == 1, "并发 upsert 写出了重复节点"
    store.close()


# ----------------------------------------------------------------------
# 决策与审计
# ----------------------------------------------------------------------


def test_concurrent_decisions_never_lose_an_audit_record(mutable_runtime: AgentRuntime):
    """并发决策一条都不能丢，每条都要能独立回放。

    审计记录"丢失"的方式很隐蔽：不是报错，而是两次决策写进了同一个编号，
    后一次把前一次覆盖掉。总数对不上、编号有重复，是唯一能看出来的地方。
    """
    tools = DomainTools(mutable_runtime)
    workers = []
    for _ in range(2):
        for subject in SUBJECTS:
            workers.append(lambda s=subject: tools.risk_decision(s))

    results = _run(workers)

    assert all(r.get("ok") for r in results), [r for r in results if not r.get("ok")]
    ids = [r["provenance"]["decision_id"] for r in results]
    assert len(set(ids)) == len(ids), f"并发决策产生了重复编号: {sorted(ids)}"

    stored = mutable_runtime.store.query_nodes(ntype="授信决策", limit=None)
    assert len(stored) == len(ids), (
        f"发起了 {len(ids)} 次决策，图里只留下 {len(stored)} 条审计记录——有记录被覆盖了"
    )

    for decision_id in ids:
        bundle = tools.evidence_bundle(decision_id=decision_id)
        assert bundle["ok"], f"决策 {decision_id} 无法回放: {bundle.get('error')}"
        assert bundle["decision_id"] == decision_id


def test_two_decisions_in_the_same_second_stay_separate(mutable_runtime: AgentRuntime, monkeypatch):
    """同一秒内的两次决策必须留下两份互不干扰的审计记录。

    决策编号的盐取自"决策时点"，精度到秒。这里把时钟钉死，让两次决策的盐**完全
    相同**——这不是人为制造的极端情况，而是把"同一秒内发生两次"这件事的概率
    放大到 100%，让缺陷必然暴露。没有这层钉死，测试就变成掷骰子。

    两次决策之间刻意改动图（删掉工商登记），让前一次的成功结论与后一次的不予
    受理形成对照：覆盖一旦发生，"前一次决策当时看到的是什么"就永久失去了答案。
    """
    monkeypatch.setattr("govfin.reasoning.decision._now", lambda: "2026-09-20T10:00:00")
    tools = DomainTools(mutable_runtime)
    store = mutable_runtime.store

    first = tools.risk_decision(SUBJECTS[0])
    assert first["ok"] and first["verdict"] != "不予受理", "首次决策应通过真实性核验"

    node = store.find_by_prop("名称", SUBJECTS[0], ntype="企业")[0]
    registrations = store.find_edges(src=node["id"], etype="拥有工商登记")
    assert registrations, "样例数据里该企业应有工商登记"
    for edge in registrations:
        store.delete_edge(edge["id"])

    second = tools.risk_decision(SUBJECTS[0])
    assert second["ok"]

    first_id = first["provenance"]["decision_id"]
    second_id = second["provenance"]["decision_id"]
    assert first_id != second_id, "同一秒内的两次决策拿到了同一个编号，前一次的审计记录会被整体覆盖"
    assert second["verdict"] == "不予受理", "登记证据已删除，第二次决策应在真实性核验处终止"

    store_first = store.get_node(f"决策:{first_id}")
    store_second = store.get_node(f"决策:{second_id}")
    assert store_first and store_second, "两次决策的记录没有同时存在"

    trail = mutable_runtime.trail
    replay_first = trail.trace(first_id)
    replay_second = trail.trace(second_id)
    assert replay_first.verdict == first["verdict"], "回放第一次决策时读到了后一次的结论"
    assert replay_second.verdict == "不予受理"
    assert replay_first.accepted_chains, "第一次决策的证据链快照丢了——冻结快照被后来的决策覆盖"


# ----------------------------------------------------------------------
# 运行时锁与缓存
# ----------------------------------------------------------------------


def test_runtime_lock_is_reentrant_and_serializes():
    """运行时锁必须同时满足两件事：可重入，且真的互斥。

    可重入：工具内部会再次取锁（``risk_decision`` 在锁内调用合成器，合成器里又
    可能进别的受锁路径），换成普通 ``Lock`` 会直接死锁。
    互斥：文档把运行时声明为"并发安全边界"，边界若漏了，读改写就会丢更新——
    这类丢失没有任何报错，只是数字少了。

    用"读 → 睡 → 写"的临界区而不是 ``counter += 1``：后者在 CPython 里靠 GIL
    也可能侥幸不丢，测不出锁有没有生效。
    """
    runtime = AgentRuntime(in_memory=True)
    counter = {"value": 0}

    def read_modify_write() -> None:
        with runtime.lock:
            with runtime.lock:  # 可重入
                current = counter["value"]
                time.sleep(0.001)
                counter["value"] = current + 1

    _run([read_modify_write] * 8)
    assert counter["value"] == 8, f"临界区发生了丢失更新（期望 8，实得 {counter['value']}），锁没有生效"
    runtime.close()


def test_concurrent_writers_leave_no_stale_adjacency_cache():
    """并发写入结束后，邻接缓存必须反映**全部**已提交的边。

    邻接表是全量重建 + 代际戳。若重建读到了未提交的中间状态、却仍然盖上"已是最
    新"的戳，那份残缺快照会一直用下去，直到下一次写入偶然把它修好——期间所有
    路径搜索都在一张不完整的图上跑，结论会漏掉风险信号，而查询本身一切正常。

    因此断言的是"缓存里的边数 == 库里的边数 == 应有的边数"，三者对上才算数。
    """
    store = GraphStore(in_memory=True, ontology=build_seed_ontology())
    writers, per_writer = 6, 60
    dim = store.add_node("风险维度", props={"记录编号": "WD-CONC", "维度名称": "并发维度", "维度权重": 1})
    indicators = [
        store.add_node("风险指标", props={"记录编号": f"ZB-CONC-{i:04d}", "指标名称": "并发指标", "指标值": 1.0})
        for i in range(writers * per_writer)
    ]

    stop = threading.Event()
    read_errors: list = []

    def reader() -> None:
        while not stop.is_set():
            try:
                store.neighbors(dim, direction="out")
                store.stats()
            except BaseException as exc:  # noqa: BLE001
                read_errors.append(exc)
                return

    watcher = threading.Thread(target=reader)
    watcher.start()

    def writer(index: int) -> int:
        sliced = indicators[index * per_writer : (index + 1) * per_writer]
        for indicator in sliced:
            store.add_edge(dim, indicator, "对应指标")
        return len(sliced)

    try:
        _run([lambda i=i: writer(i) for i in range(writers)])
    finally:
        stop.set()
        watcher.join(timeout=10)

    assert not read_errors, f"并发读抛出了异常: {read_errors[:3]}"

    expected = writers * per_writer
    assert len(store.neighbors(dim, direction="out")) == expected, "邻接缓存缺边：重建期间读到了未提交状态"
    assert len(store.all_edges()) == expected
    assert store.stats()["edges"] == expected
    assert store.integrity_report()["healthy"] is True
    store.close()
