"""期限驱动实验排程：引擎语义 + FastAPI 端到端。每个测试用独立临时数据库。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DEADLINE_DB", db_path)
    import importlib

    import app.db as db_mod
    importlib.reload(db_mod)
    import app.service as service_mod
    importlib.reload(service_mod)
    import app.schedule_service as schedule_service_mod
    importlib.reload(schedule_service_mod)
    import app.main as main_mod
    importlib.reload(main_mod)
    from fastapi.testclient import TestClient

    with TestClient(main_mod.app) as c:
        yield c


T0 = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)


def dt(minutes: float = 0) -> datetime:
    return T0 + timedelta(minutes=minutes)


def izo(value) -> str:
    if isinstance(value, (int, float)):
        value = dt(value)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- 辅助 ----

def sched_rules():
    from app.models import ItemRule, RuleSet
    return RuleSet(version="SR-1", items=[
        ItemRule(item="cod", pretreatment_minutes=120, analysis_minutes=1440),
        ItemRule(item="oil", pretreatment_minutes=None, analysis_minutes=600),
        ItemRule(item="nh3n", pretreatment_minutes=240, analysis_minutes=1440),
        ItemRule(item="pa", pretreatment_minutes=None, analysis_minutes=600),
        ItemRule(item="pb", pretreatment_minutes=None, analysis_minutes=600),
    ])


def mk_sample(sid, items, methods, at=0):
    from app.models import Sample, SampleKind
    return Sample(id=sid, kind=SampleKind.GRAB, items=items,
                  item_methods=methods,
                  sampling_start=dt(at), sampling_end=dt(at))


def receive(client, samples, eval_min=30, ik=None):
    from app.models import JudgmentRequest
    req = JudgmentRequest(eval_time=dt(eval_min), rule_set=sched_rules(),
                          samples=samples, idempotency_key=ik)
    r = client.post("/api/v1/judgments", json=req.model_dump(mode="json"))
    assert r.status_code == 201, r.text
    return r.json()


def W(a, b):
    from app.models import TimeWindow
    return TimeWindow(start=dt(a), end=dt(b))


def default_resources(version="LAB-1"):
    from app.models import ResourceConfig, ResourceSet
    return ResourceSet(version=version, resources=[
        ResourceConfig(resource_id="PT-1", kind="pretreatment", capacity=4,
                       task_minutes=30, switch_minutes=10, windows=[W(0, 2880)]),
        ResourceConfig(resource_id="IC-1", kind="instrument", methods=["hj828"],
                       capacity=2, task_minutes=45, switch_minutes=20,
                       windows=[W(0, 2880)]),
        ResourceConfig(resource_id="IC-2", kind="instrument", methods=["hj535"],
                       capacity=2, task_minutes=60, switch_minutes=15,
                       windows=[W(0, 2880)]),
    ])


def post_resources(client, rset=None):
    rset = rset or default_resources()
    r = client.post("/api/v1/resources", json=rset.model_dump(mode="json"))
    assert r.status_code == 201, r.text
    return r.json()


def trial(client, schedule_min=30, **kw):
    from app.models import ScheduleRequest
    req = ScheduleRequest(schedule_time=dt(schedule_min), **kw)
    return client.post("/api/v1/schedules/trial", json=req.model_dump(mode="json"))


def issue(client, schedule_min=30, **kw):
    from app.models import ScheduleRequest
    req = ScheduleRequest(schedule_time=dt(schedule_min), **kw)
    return client.post("/api/v1/schedules", json=req.model_dump(mode="json"))


def task_map(body):
    return {(t["sample_id"], t["item"], t["phase"]): t for t in body["tasks"]}


# ============================================================== 基本排程 ----

def test_trial_basic_places_pretreatment_before_analysis(client):
    pkg = receive(client, [mk_sample("b1", ["cod", "oil"],
                                     {"cod": "hj828", "oil": "hj535"})], ik="b1")
    post_resources(client)
    r = trial(client, 30)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "feasible"
    assert body["conflicts"] == []
    assert body["earliest_conflict"] is None
    assert body["trial"] is True
    assert body["schedule_id"].startswith("trial-")
    assert body["packages"] == [pkg["package_id"]]

    tasks = task_map(body)
    pre = tasks[("b1", "cod", "pretreatment")]
    ana = tasks[("b1", "cod", "analysis")]
    oil = tasks[("b1", "oil", "analysis")]
    # 前处理工位 [30,60)，分析在其后
    assert pre["resource_id"] == "PT-1"
    assert pre["start"] == izo(30) and pre["end"] == izo(60)
    assert ana["resource_id"] == "IC-1"
    assert ana["start"] == izo(60) and ana["end"] == izo(105)
    # 余量 = 截止 - 结束
    assert pre["slack_minutes"] == 60          # 120 - 60
    assert ana["slack_minutes"] == 1440 - 105
    # oil 无预处理：分析截止 600，IC-2 [30,90)
    assert oil["resource_id"] == "IC-2"
    assert oil["start"] == izo(30) and oil["end"] == izo(90)
    assert oil["slack_minutes"] == 510
    # 每项都带规则哈希（与判定包冻结的规则一致）
    rule_hash = pkg["clocks"][0]["matched_rule"]["content_hash"]
    assert ana["rule"]["content_hash"] == rule_hash
    assert ana["rule"]["rule_id"]
    assert oil["rule"]["content_hash"] == rule_hash
    # 批次视图
    assert body["summary"]["scheduled"] == 3
    assert body["summary"]["on_time"] == 3
    assert body["summary"]["method_switches"] == 0
    kinds = {b["resource_id"]: b["kind"] for b in body["batches"]}
    assert kinds["PT-1"] == "pretreatment" and kinds["IC-1"] == "instrument"


def test_trial_deterministic_same_input(client):
    receive(client, [
        mk_sample("b1", ["cod", "oil"], {"cod": "hj828", "oil": "hj535"}),
        mk_sample("b2", ["nh3n"], {"nh3n": "hj535"}),
    ], ik="det")
    post_resources(client)
    r1 = trial(client, 30).json()
    r2 = trial(client, 30).json()
    assert r1["content_hash"] == r2["content_hash"]
    assert r1["schedule_id"] == r2["schedule_id"]
    assert r1["tasks"] == r2["tasks"]
    assert r1["batches"] == r2["batches"]
    assert r1["conflicts"] == r2["conflicts"]


def test_trial_not_persisted(client):
    receive(client, [mk_sample("b1", ["cod"], {"cod": "hj828"})], ik="np")
    post_resources(client)
    r = trial(client, 30)
    assert r.status_code == 200
    assert client.get("/api/v1/schedules").json()["schedules"] == []
    assert client.get(
        f"/api/v1/schedules/{r.json()['schedule_id']}").status_code == 404


# ============================================================== 批与切换 ----

def test_batching_capacity_and_method_switch(client):
    """同方法并入同批（受单批容量限制）；方法不同产生切换时间。"""
    from app.models import ResourceConfig, ResourceSet
    rs = ResourceSet(version="LAB-SW", resources=[
        ResourceConfig(resource_id="PT-1", kind="pretreatment", capacity=4,
                       task_minutes=30, switch_minutes=10, windows=[W(0, 2880)]),
        ResourceConfig(resource_id="IC-X", kind="instrument",
                       methods=["m1", "m2"], capacity=1, task_minutes=30,
                       switch_minutes=20, windows=[W(0, 2880)]),
    ])
    receive(client, [
        mk_sample("s1", ["pa"], {"pa": "m1"}),
        mk_sample("s2", ["pa"], {"pa": "m1"}),
        mk_sample("s3", ["pb"], {"pb": "m2"}),
    ], ik="sw")
    post_resources(client, rs)
    body = trial(client, 30).json()
    assert body["status"] == "feasible"
    batches = body["batches"]
    assert len(batches) == 3
    # 同方法相邻：m1, m1, m2 -> 仅 1 次切换
    assert [b["method"] for b in batches] == ["m1", "m1", "m2"]
    assert body["summary"]["method_switches"] == 1
    assert batches[0]["switch_before_minutes"] == 0
    assert batches[1]["switch_before_minutes"] == 0
    assert batches[2]["switch_before_minutes"] == 20
    # 容量 1 -> 每个 m1 任务各占一批；m2 批次在切换 20 分钟后开始
    assert batches[2]["start"] == izo(110)   # 90 + 20 切换
    assert batches[2]["end"] == izo(140)


def test_batch_capacity_groups_same_method(client):
    """容量 2 的仪器：两个同方法任务并入一批，第三个开新批。"""
    receive(client, [
        mk_sample("s1", ["cod"], {"cod": "hj828"}),
        mk_sample("s2", ["cod"], {"cod": "hj828"}),
        mk_sample("s3", ["cod"], {"cod": "hj828"}),
    ], ik="cap")
    post_resources(client)
    body = trial(client, 30).json()
    assert body["status"] == "feasible"
    tasks = task_map(body)
    # 前处理工位容量 4：三个前处理任务同批
    pre_batches = {tasks[(s, "cod", "pretreatment")]["batch_id"]
                   for s in ("s1", "s2", "s3")}
    assert len(pre_batches) == 1
    # 仪器容量 2：s1/s2 同批，s3 新批
    a1 = tasks[("s1", "cod", "analysis")]["batch_id"]
    a2 = tasks[("s2", "cod", "analysis")]["batch_id"]
    a3 = tasks[("s3", "cod", "analysis")]["batch_id"]
    assert a1 == a2 and a3 != a1
    ic1_batches = [b for b in body["batches"] if b["resource_id"] == "IC-1"]
    assert len(ic1_batches) == 2
    assert sorted(len(b["task_keys"]) for b in ic1_batches) == [1, 2]


# ============================================================== 无解冲突 ----

def test_infeasible_reports_earliest_conflict(client):
    """停机窗挤压空闲区间：任务最早可行结束晚于截止 -> 指出资源与区间。"""
    from app.models import ResourceConfig, ResourceSet
    rs = ResourceSet(version="LAB-DT", resources=[
        ResourceConfig(resource_id="IC-2", kind="instrument", methods=["hj535"],
                       capacity=2, task_minutes=60, switch_minutes=15,
                       windows=[W(0, 2880)], downtime=[W(0, 541)]),
    ])
    receive(client, [mk_sample("b1", ["oil"], {"oil": "hj535"})], ik="dt")
    post_resources(client, rs)
    body = trial(client, 30).json()
    assert body["status"] == "infeasible"
    assert body["tasks"] == []
    assert len(body["conflicts"]) == 1
    cf = body["conflicts"][0]
    assert cf["reason"] == "deadline_miss"
    assert cf["resource_id"] == "IC-2"
    # 空闲区间 [541, 2880)：最早 [541, 601)，晚于截止 600 共 1 分钟
    assert cf["interval"] == {"start": izo(541), "end": izo(601)}
    assert cf["minutes_late"] == 1
    assert cf["deadline"] == izo(600)
    assert [(a["sample_id"], a["item"], a["phase"])
            for a in cf["affected_clocks"]] == [("b1", "oil", "analysis")]
    assert body["earliest_conflict"] == cf


def test_pretreatment_failure_blocks_analysis(client):
    """前处理无法准时 -> 同一时钟的分析连带受阻（affected_clocks 含两者）。"""
    from app.models import ResourceConfig, ResourceSet
    rs = ResourceSet(version="LAB-PT", resources=[
        ResourceConfig(resource_id="PT-1", kind="pretreatment", capacity=4,
                       task_minutes=30, switch_minutes=10,
                       windows=[W(0, 2880)], downtime=[W(0, 200)]),
        ResourceConfig(resource_id="IC-1", kind="instrument", methods=["hj828"],
                       capacity=2, task_minutes=45, switch_minutes=20,
                       windows=[W(0, 2880)]),
    ])
    receive(client, [mk_sample("b1", ["cod"], {"cod": "hj828"})], ik="pt")
    post_resources(client, rs)
    body = trial(client, 30).json()
    assert body["status"] == "infeasible"
    assert len(body["conflicts"]) == 1
    cf = body["conflicts"][0]
    assert cf["phase"] == "pretreatment"
    assert cf["resource_id"] == "PT-1"
    assert cf["interval"] == {"start": izo(200), "end": izo(230)}
    assert cf["minutes_late"] == 110  # 截止 120
    affected = {(a["sample_id"], a["item"], a["phase"])
                for a in cf["affected_clocks"]}
    assert affected == {("b1", "cod", "pretreatment"),
                        ("b1", "cod", "analysis")}
    # 分析未被尝试排程
    assert body["tasks"] == []


def test_no_compatible_resource(client):
    """仪器 methods 不含任务方法 -> no_compatible_resource。"""
    receive(client, [mk_sample("b1", ["oil"], {"oil": "ghost"})], ik="nc")
    post_resources(client)
    body = trial(client, 30).json()
    assert body["status"] == "infeasible"
    cf = body["conflicts"][0]
    assert cf["reason"] == "no_compatible_resource"
    assert cf["resource_id"] is None
    assert cf["interval"] is None


def test_overdue_clock_cannot_be_on_time(client):
    """判定时刻已超期的时钟：任何排程都晚于截止 -> deadline_miss。"""
    receive(client, [mk_sample("b1", ["oil"], {"oil": "hj535"})],
            eval_min=700, ik="ov")
    post_resources(client)
    body = trial(client, 700).json()
    assert body["status"] == "infeasible"
    cf = body["conflicts"][0]
    assert cf["reason"] == "deadline_miss"
    assert cf["interval"] == {"start": izo(700), "end": izo(760)}
    assert cf["minutes_late"] == 160


# ============================================================== 签发/冻结 ----

def test_issue_freezes_occupations_and_redrafts_only_new(client):
    pkg1 = receive(client, [mk_sample("b1", ["cod"], {"cod": "hj828"})], ik="k1")
    post_resources(client)
    # 签发 v1
    r1 = issue(client, 30, idempotency_key="sch-1")
    assert r1.status_code == 201, r1.text
    v1 = r1.json()
    assert v1["version_no"] == 1
    assert v1["trial"] is False
    assert v1["frozen_from"] is None
    # 幂等重放
    r1b = issue(client, 30, idempotency_key="sch-1")
    assert r1b.json()["schedule_id"] == v1["schedule_id"]
    assert client.get("/api/v1/schedules").json()["count"] == 1

    v1_tasks = task_map(v1)
    v1_pre = v1_tasks[("b1", "cod", "pretreatment")]
    assert v1_pre["start"] == izo(30) and v1_pre["end"] == izo(60)

    # 新判定 b2 到来；补录 b1（只加温度点）-> 都只重排草稿
    receive(client, [mk_sample("b2", ["cod"], {"cod": "hj828"})], ik="k2")
    supp = {"eval_time": dt(45).isoformat(),
            "temperature": [{"time": dt(45).isoformat(), "temp_c": 3.0}]}
    assert client.post("/api/v1/samples/b1/supplement", json=supp).status_code == 201

    body = trial(client, 45).json()
    assert body["frozen_from"]["schedule_id"] == v1["schedule_id"]
    assert body["frozen_from"]["version_no"] == 1
    tasks = task_map(body)
    # b1 的任务冻结不变（即使 b1 刚补录过）
    for key in (("b1", "cod", "pretreatment"), ("b1", "cod", "analysis")):
        t = tasks[key]
        assert t["frozen"] is True
        assert t["start"] == v1_tasks[key]["start"]
        assert t["end"] == v1_tasks[key]["end"]
        assert t["batch_id"] == v1_tasks[key]["batch_id"]
    # b2 的任务是新排的草稿，绕开冻结占用（冻结批次不可并入）
    b2_pre = tasks[("b2", "cod", "pretreatment")]
    assert b2_pre["frozen"] is False
    assert b2_pre["batch_id"] != v1_pre["batch_id"]
    assert b2_pre["start"] == izo(60) and b2_pre["end"] == izo(90)
    b2_ana = tasks[("b2", "cod", "analysis")]
    assert b2_ana["start"] == izo(105)  # IC-1 上 b1 分析 [60,105) 之后
    assert body["summary"]["frozen_tasks"] == 2
    assert body["summary"]["scheduled"] == 2

    # 签发 v2 并比较版本
    v2 = issue(client, 45, idempotency_key="sch-2").json()
    assert v2["version_no"] == 2
    d = client.get(
        f"/api/v1/schedules/{v1['schedule_id']}/diff/{v2['schedule_id']}"
    ).json()
    assert d["from"]["version_no"] == 1 and d["to"]["version_no"] == 2
    added = {(c["sample_id"], c["item"], c["phase"])
             for c in d["changes"] if c["kind"] == "task_added"}
    assert added == {("b2", "cod", "pretreatment"), ("b2", "cod", "analysis")}
    # b1 的冻结任务不产生任何变化
    assert not [c for c in d["changes"] if c["sample_id"] == "b1"]

    # 工作单：按资源分组、批次有序、含规则哈希
    wo = client.get(f"/api/v1/schedules/{v2['schedule_id']}/workorder").json()
    assert wo["schedule_id"] == v2["schedule_id"]
    res = {r["resource_id"]: r for r in wo["resources"]}
    assert set(res) == {"PT-1", "IC-1"}
    pt_batches = res["PT-1"]["batches"]
    assert [b["start"] for b in pt_batches] == [izo(30), izo(60)]
    assert pt_batches[0]["frozen"] is True
    assert pt_batches[1]["frozen"] is False
    t0 = pt_batches[0]["tasks"][0]
    assert t0["rule_hash"] and t0["rule_id"]
    assert t0["slack_minutes"] >= 0


def test_issue_with_conflicts_keeps_tasks_pending(client):
    """签发允许带冲突（infeasible）：未排入的任务留在待排池，下轮重试。"""
    from app.models import ResourceConfig, ResourceSet
    rs = ResourceSet(version="LAB-CF", resources=[
        ResourceConfig(resource_id="IC-2", kind="instrument", methods=["hj535"],
                       capacity=2, task_minutes=60, switch_minutes=15,
                       windows=[W(0, 2880)], downtime=[W(0, 541)]),
    ])
    receive(client, [mk_sample("b1", ["oil"], {"oil": "hj535"})], ik="cf")
    post_resources(client, rs)
    v1 = issue(client, 30, idempotency_key="sch-cf").json()
    assert v1["status"] == "infeasible"
    assert v1["summary"]["scheduled"] == 0
    # 停机窗缩短后重试：同一任务重新进入草稿并可排入
    rs2 = ResourceSet(version="LAB-CF2", resources=[
        ResourceConfig(resource_id="IC-2", kind="instrument", methods=["hj535"],
                       capacity=2, task_minutes=60, switch_minutes=15,
                       windows=[W(0, 2880)], downtime=[W(0, 100)]),
    ])
    post_resources(client, rs2)
    body = trial(client, 30, resource_version="LAB-CF2").json()
    assert body["status"] == "feasible"
    assert body["summary"]["scheduled"] == 1


# ============================================================== 输入解析 ----

def test_explicit_package_ids_and_validation(client):
    p1 = receive(client, [mk_sample("b1", ["cod"], {"cod": "hj828"})], ik="p1")
    p2 = receive(client, [mk_sample("b2", ["oil"], {"oil": "hj535"})], ik="p2")
    post_resources(client)
    # 只排 p1
    body = trial(client, 30, package_ids=[p1["package_id"]]).json()
    assert {t["sample_id"] for t in body["tasks"]} == {"b1"}
    assert body["packages"] == [p1["package_id"]]
    # 两个包
    body = trial(client, 30,
                 package_ids=[p2["package_id"], p1["package_id"]]).json()
    assert {t["sample_id"] for t in body["tasks"]} == {"b1", "b2"}
    # 不存在的包 -> 404
    assert trial(client, 30, package_ids=["pkg-ghost"]).status_code == 404
    # 试算包未冻结 -> 422
    from app.models import JudgmentRequest
    tr = client.post("/api/v1/judgments/trial", json=JudgmentRequest(
        eval_time=dt(30), rule_set=sched_rules(),
        samples=[mk_sample("bx", ["cod"], {"cod": "hj828"})],
    ).model_dump(mode="json"))
    tid = tr.json()["package_id"]
    assert trial(client, 30, package_ids=[tid]).status_code == 422


def test_resource_resolution_paths(client):
    receive(client, [mk_sample("b1", ["cod"], {"cod": "hj828"})], ik="rr")
    # 未登记任何资源且未携带 -> 422
    assert trial(client, 30).status_code == 422
    # 未登记版本 -> 404
    assert trial(client, 30, resource_version="ghost").status_code == 404
    # 携带资源集试排：不写库
    r = trial(client, 30, resource_set=default_resources("LAB-INLINE"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "feasible"
    assert client.get("/api/v1/resources").json()["resources"] == []
    # 签发携带资源集：登记 + 固化
    r = issue(client, 30, resource_set=default_resources("LAB-ISS"),
              idempotency_key="sch-iss")
    assert r.status_code == 201, r.text
    versions = [x["version"] for x in
                client.get("/api/v1/resources").json()["resources"]]
    assert versions == ["LAB-ISS"]
    # 缺省使用最近登记的资源集
    body = trial(client, 30).json()
    assert body["resource"]["version"] == "LAB-ISS"


def test_resource_version_label_conflict(client):
    post_resources(client, default_resources("LAB-1"))
    # 完全相同 -> 幂等
    r = client.post("/api/v1/resources",
                    json=default_resources("LAB-1").model_dump(mode="json"))
    assert r.status_code == 201 and r.json()["created"] is False
    # 同标签不同内容 -> 409
    changed = default_resources("LAB-1")
    changed.resources[0].capacity = 8
    r = client.post("/api/v1/resources", json=changed.model_dump(mode="json"))
    assert r.status_code == 409
    # 详情端点
    g = client.get("/api/v1/resources/LAB-1")
    assert g.status_code == 200
    assert g.json()["resource_set"]["resources"][0]["capacity"] == 4
    assert client.get("/api/v1/resources/ghost").status_code == 404


# ============================================================== 单元 ----

def test_extract_tasks_skips_non_conclusive_clock():
    from app.schedule_service import extract_tasks
    pkg = {
        "package_id": "pkg-x", "created_at": "2026-09-11T08:30:00+00:00",
        "clocks": [{
            "sample_id": "b1", "item": "cod", "conclusive": False,
            "status": "indeterminate", "match_status": "none",
            "matched_rule": None, "match_context": None,
            "phases": [{
                "phase": "analysis", "status": "pending",
                "deadline": "2026-09-12T08:00:00+00:00",
                "limit_minutes": 1440,
            }],
        }],
    }
    tasks, skipped = extract_tasks([pkg], set())
    assert tasks == []
    assert len(skipped) == 1
    assert skipped[0]["sample_id"] == "b1"
    # 冻结键排除
    pkg2 = {
        "package_id": "pkg-y", "created_at": "2026-09-11T08:30:00+00:00",
        "clocks": [{
            "sample_id": "b1", "item": "cod", "conclusive": True,
            "status": "ok", "match_status": "unique",
            "matched_rule": {"version": "v", "content_hash": "h",
                             "rule_id": "r", "item": "cod"},
            "match_context": {"field_sources": [
                {"field": "method", "value": "hj828"}]},
            "phases": [{
                "phase": "analysis", "status": "pending",
                "deadline": "2026-09-12T08:00:00+00:00",
                "limit_minutes": 1440,
            }],
        }],
    }
    tasks, skipped = extract_tasks([pkg2], {("b1", "cod", "analysis")})
    assert tasks == [] and skipped == []
    tasks, _ = extract_tasks([pkg2], set())
    assert len(tasks) == 1
    assert tasks[0].method == "hj828"
    assert tasks[0].rule["content_hash"] == "h"
