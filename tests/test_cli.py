"""CLI 测试：子命令可用性、退出码、自检判据。

这里不重复测各层逻辑（那些有各自的测试文件）。CLI 层的特有风险是
**退出码**和**输出可解析性**——部署脚本靠退出码判断成败，
退出码错了会让 CI 把失败当成功。因此每条断言都落在退出码或 JSON 结构上。
"""

from __future__ import annotations

import json

import pytest

from govfin.cli import EXIT_FAILED, EXIT_OK, main


def _run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip().startswith("{") else out)


def test_help_lists_all_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == EXIT_OK
    text = capsys.readouterr().out
    for cmd in ("ingest", "stats", "ask", "replay", "evolve", "doctor"):
        assert cmd in text


def test_doctor_fails_on_empty_graph(capsys):
    """空图上自检必须**失败**。

    这是本文件里最重要的一条：自检如果对空图报通过，那它就完全失去了意义——
    图空了只会让结论变空，不会抛异常，是部署里最难发现的一类故障。
    """
    code, payload = _run(capsys, "--in-memory", "doctor")
    assert code == EXIT_FAILED
    assert payload["healthy"] is False
    names = {c["name"]: c["ok"] for c in payload["checks"]}
    assert names["图谱非空"] is False
    assert names["实体图有内容"] is False


def test_doctor_passes_when_loaded(tmp_path, capsys):
    db = tmp_path / "cli.db"
    code, _ = _run(capsys, "--db", str(db), "ingest", "--data", "data")
    assert code == EXIT_OK

    code, payload = _run(capsys, "--db", str(db), "doctor")
    assert code == EXIT_OK, payload
    assert payload["healthy"] is True
    names = {c["name"]: c["ok"] for c in payload["checks"]}
    assert all(names.values())

    # 运行时图为空是正常状态，不该判为故障
    runtime_layer = next(c for c in payload["checks"] if c["name"] == "运行时图")
    assert runtime_layer["ok"] is True


def test_doctor_does_not_leave_traces(tmp_path, capsys):
    """自检跑决策探针，但不应把决策写进图。

    否则每次重启都多出一批以自检主体为对象的假决策，污染审计与漂移检测。
    """
    db = tmp_path / "cli.db"
    _run(capsys, "--db", str(db), "ingest", "--data", "data")
    before = _run(capsys, "--db", str(db), "stats")[1]["graph"]["nodes"]

    _run(capsys, "--db", str(db), "doctor")
    after = _run(capsys, "--db", str(db), "stats")[1]["graph"]["nodes"]
    assert after == before, "自检不应改变图"


def test_ask_reports_verdict_and_judgements(runtime, tmp_path, capsys):
    db = tmp_path / "cli.db"
    _run(capsys, "--db", str(db), "ingest", "--data", "data")
    code, payload = _run(capsys, "--db", str(db), "ask", "甲科技有限公司", "--brief")
    assert code == EXIT_OK
    assert payload["verdict"] == "审慎核定"
    assert payload["judgements"], "应给出触发的阈值判定"
    assert payload["judgements"][0]["clause"], "判定必须回指条款"
    assert payload["decision_id"].startswith("DEC-")


def test_ask_unknown_subject_fails(tmp_path, capsys):
    """查无此主体要返回非零退出码，否则调度脚本会把失败当成功。"""
    db = tmp_path / "cli.db"
    _run(capsys, "--db", str(db), "ingest", "--data", "data")
    code, payload = _run(capsys, "--db", str(db), "ask", "查无此企业有限公司", "--brief")
    assert code == EXIT_FAILED
    assert payload["ok"] is False


def test_replay_renders_chain(tmp_path, capsys):
    db = tmp_path / "cli.db"
    _run(capsys, "--db", str(db), "ingest", "--data", "data")
    _, asked = _run(capsys, "--db", str(db), "ask", "甲科技有限公司", "--brief")

    code, text = _run(capsys, "--db", str(db), "replay", asked["decision_id"])
    assert code == EXIT_OK
    assert asked["decision_id"] in text
    assert "依据条款原文" in text


def test_replay_unknown_decision_fails(tmp_path, capsys):
    db = tmp_path / "cli.db"
    code, _ = _run(capsys, "--db", str(db), "replay", "DEC-DOESNOTEXIST")
    assert code == EXIT_FAILED


def test_evolve_dry_run_makes_no_commit(tmp_path, capsys):
    """--dry-run 必须走完整管道但**不推进本体版本**。

    样例数据能被种子本体完全覆盖，储备池是空的，因此这里先灌入低资源域的
    UNK 观测，让管道有东西可处理——否则这条测试什么都没验证。
    """
    from govfin.evolution.unk_pool import KIND_ENTITY
    from govfin.runtime import AgentRuntime

    db = tmp_path / "cli.db"
    _run(capsys, "--db", str(db), "ingest", "--data", "data")

    seed = AgentRuntime(db_path=str(db))
    for i, text in enumerate(("经营异常名录", "经营异常名录", "异常经营名录")):
        seed.pipeline.pool.observe(
            text,
            KIND_ENTITY,
            source_document=f"doc:gs-2026-004{i}",
            context="企业被列入经营异常名录",
            hint_type="工商登记",
        )
    seed.close()

    code, payload = _run(capsys, "--db", str(db), "evolve", "--dry-run")
    assert code == EXIT_OK
    assert payload["cycle"] >= 1
    assert payload["pool_size"] > 0, "储备池里应当有刚灌入的 UNK 观测"
    assert payload["version_before"] == payload["version_after"], "dry-run 不应推进本体版本"
