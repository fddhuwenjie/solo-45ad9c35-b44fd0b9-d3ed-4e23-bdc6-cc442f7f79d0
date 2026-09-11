"""pytest：引擎语义 + FastAPI 端到端。每个测试用独立临时数据库。"""
from __future__ import annotations

import os
import tempfile
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
    import app.main as main_mod
    importlib.reload(main_mod)
    from fastapi.testclient import TestClient

    with TestClient(main_mod.app) as c:
        yield c


T0 = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)


def dt(minutes: float = 0) -> datetime:
    return T0 + timedelta(minutes=minutes)


def izo(value) -> str:
    """归一化为 FastAPI JSON 输出形式（UTC -> ...Z）。"""
    if isinstance(value, (int, float)):
        value = dt(value)
    s = value.astimezone(timezone.utc).isoformat()
    return s.replace("+00:00", "Z")


def ruleset(version="2026.1"):
    from app.models import ItemRule, RuleSet
    return RuleSet(version=version, items=[
        ItemRule(item="cod", item_name="COD", min_temp_c=0, max_temp_c=4,
                 pretreatment_minutes=120, analysis_minutes=1440,
                 required_preservation=["cool_4c"]),
        ItemRule(item="oil", item_name="石油类", min_temp_c=0, max_temp_c=4,
                 pretreatment_minutes=None, analysis_minutes=600,
                 required_preservation=["cool_4c", "dark"]),
        ItemRule(item="nh3n", item_name="氨氮", min_temp_c=0, max_temp_c=4,
                 pretreatment_minutes=240, analysis_minutes=1440,
                 required_preservation=["add_acid"]),
    ])


def temps(start=0, end=60, step=10, temp=3.0, extra=None):
    from app.models import TemperaturePoint
    pts = [TemperaturePoint(time=dt(m), temp_c=temp)
           for m in range(start, end + 1, step)]
    if extra:
        pts += extra
    pts.sort(key=lambda p: p.time)
    return pts


def make_sample(**over):
    from app.models import PreservationAction, Sample, SampleKind
    base = dict(
        id="b1", kind=SampleKind.GRAB, items=["cod", "oil"],
        container="glass", sampling_start=dt(0), sampling_end=dt(0),
        preservation=[PreservationAction(name="cool_4c", time=dt(0)),
                      PreservationAction(name="dark", time=dt(0))],
        temperature=temps(0, 30, 10),
        custody_transfers=[dt(15)],
    )
    base.update(over)
    return Sample(**base)


def request(samples, eval_min=30, **over):
    from app.models import JudgmentRequest
    base = dict(eval_time=dt(eval_min), rule_set=ruleset(), samples=samples,
                critical_within_minutes=60)
    base.update(over)
    return JudgmentRequest(**base)


# ============================================================== 正常 ----

def test_normal_grab(client):
    s = make_sample()
    r = client.post("/api/v1/judgments/trial", json=request([s]).model_dump(mode="json"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["package_id"].startswith("trial-")
    by = {c["item"]: c for c in body["clocks"]}
    assert by["cod"]["status"] == "ok"
    # 预处理截止 120，eval 30 -> 90 分钟
    assert by["cod"]["remaining_minutes"] == 90
    assert by["cod"]["next_action"] == "pretreatment"
    assert by["cod"]["latest_operation_at"] == izo(1440)
    # oil 无预处理：分析截止 600，eval 30 -> 570
    assert by["oil"]["remaining_minutes"] == 570
    assert by["oil"]["next_action"] == "analysis"
    assert by["cod"]["conforming"] and by["oil"]["conforming"]
    assert body["violations"] == []
    # 优先批次按截止排序：cod(预处理120) 早于 oil(分析600)
    assert body["priority_batches"][0]["sample_id"] == "b1"
    assert body["summary"]["conforming"] == 2


def test_continuous_uses_end_by_default(client):
    from app.models import Sample, SampleKind, JudgmentRequest
    # 连续采样 08:00-10:00，cod 预处理 120，eval 10:30 -> 剩 90
    s = Sample(id="c1", kind=SampleKind.CONTINUOUS, items=["cod"],
               sampling_start=dt(0), sampling_end=dt(120),
               preservation=[{"name": "cool_4c", "time": dt(120)}],
               temperature=temps(120, 150, 10))
    req = JudgmentRequest(eval_time=dt(150), rule_set=ruleset(), samples=[s])
    r = client.post("/api/v1/judgments/trial", json=req.model_dump(mode="json"))
    body = r.json()
    c = body["clocks"][0]
    assert c["basis"] == "sampling_end"
    assert c["basis_time"] == izo(120)
    assert c["remaining_minutes"] == 90


def test_continuous_rule_basis_start(client):
    """规则指定 continuous_basis=start 时按采样开始起算。"""
    from app.models import ItemRule, RuleSet, Sample, SampleKind, JudgmentRequest
    rs = RuleSet(version="x", items=[ItemRule(
        item="x", max_temp_c=4, pretreatment_minutes=60, analysis_minutes=600,
        continuous_basis="start")])
    s = Sample(id="c1", kind=SampleKind.CONTINUOUS, items=["x"],
               sampling_start=dt(0), sampling_end=dt(120),
               temperature=temps(0, 30, 10))
    req = JudgmentRequest(eval_time=dt(30), rule_set=rs, samples=[s])
    r = client.post("/api/v1/judgments/trial", json=req.model_dump(mode="json"))
    c = r.json()["clocks"][0]
    assert c["basis"] == "sampling_start"
    assert c["remaining_minutes"] == 30  # 60 - 30


# ============================================================== 临界/超时 ----

def test_critical_within_threshold(client):
    # cod 预处理 120，eval 70 -> 剩 50 <= 60 临界
    s = make_sample(temperature=temps(0, 70, 10))
    r = client.post("/api/v1/judgments/trial",
                    json=request([s], eval_min=70).model_dump(mode="json"))
    c = {x["item"]: x for x in r.json()["clocks"]}["cod"]
    assert c["status"] == "critical"
    assert c["remaining_minutes"] == 50
    phases = {p["phase"]: p for p in c["phases"]}
    assert phases["pretreatment"]["status"] == "critical"


def test_overdue(client):
    # cod 预处理期限 120，eval 130 未做前处理 -> 超时；分析期限仍独立
    s = make_sample(temperature=temps(0, 130, 10))
    r = client.post("/api/v1/judgments/trial",
                    json=request([s], eval_min=130).model_dump(mode="json"))
    c = {x["item"]: x for x in r.json()["clocks"]}["cod"]
    assert c["status"] == "overdue"
    phases = {p["phase"]: p for p in c["phases"]}
    assert phases["pretreatment"]["status"] == "overdue"
    assert phases["analysis"]["status"] == "pending"  # 分析未被清零
    assert not c["conforming"]


def test_pretreatment_done_does_not_reset_analysis(client):
    """前处理在期限内完成只结束预处理阶段，分析期限仍从基准起算。"""
    from app.models import PretreatmentEvent
    s = make_sample(
        pretreatments=[PretreatmentEvent(type="digestion", time=dt(100))],
        temperature=temps(0, 200, 10),
    )
    r = client.post("/api/v1/judgments/trial",
                    json=request([s], eval_min=200).model_dump(mode="json"))
    c = {x["item"]: x for x in r.json()["clocks"]}["cod"]
    phases = {p["phase"]: p for p in c["phases"]}
    assert phases["pretreatment"]["status"] == "completed"
    assert phases["pretreatment"]["done_at"] == izo(100)
    # 分析截止仍是基准+1440，剩余 1240
    assert phases["analysis"]["deadline"] == izo(1440)
    assert phases["analysis"]["remaining_minutes"] == 1240
    assert c["next_action"] == "analysis"


def test_late_pretreatment_completion_flags_overdue(client):
    from app.models import PretreatmentEvent
    s = make_sample(
        pretreatments=[PretreatmentEvent(type="digestion", time=dt(125))],
        temperature=temps(0, 130, 10),
    )
    r = client.post("/api/v1/judgments/trial",
                    json=request([s], eval_min=130).model_dump(mode="json"))
    c = {x["item"]: x for x in r.json()["clocks"]}["cod"]
    phases = {p["phase"]: p for p in c["phases"]}
    assert phases["pretreatment"]["status"] == "overdue"  # 125 > 120


# ============================================================== 分样/合样 ----

def test_aliquot_shares_basis_and_history(client):
    from app.models import Sample, SampleKind
    mother = make_sample(id="M")
    child = Sample(
        id="A", kind=SampleKind.ALIQUOT, items=["cod", "oil"],
        sampling_start=dt(10), sampling_end=dt(10), parent_ids=["M"],
        temperature=temps(10, 40, 10),
    )
    r = client.post(
        "/api/v1/judgments/trial",
        json=request([mother, child], eval_min=40).model_dump(mode="json"))
    body = r.json()
    ca = {x["item"]: x for x in body["clocks"] if x["sample_id"] == "A"}
    assert ca["cod"]["origin_sample_id"] == "M"
    assert ca["cod"]["basis_time"] == izo(0)
    # 母体已做 cool_4c + dark，子样继承 -> 无防腐缺失
    assert body["violations"] == []
    assert ca["cod"]["remaining_minutes"] == 80


def test_composite_uses_earliest_component(client):
    from app.models import Sample, SampleKind
    # 组成样 X(08:00) 与 Y(09:00)，合样于 10:00
    x = Sample(id="X", kind=SampleKind.GRAB, items=["cod"],
               sampling_start=dt(0), sampling_end=dt(0),
               preservation=[{"name": "cool_4c", "time": dt(0)}],
               temperature=temps(0, 120, 15))
    y = Sample(id="Y", kind=SampleKind.GRAB, items=["cod"],
               sampling_start=dt(60), sampling_end=dt(60),
               preservation=[{"name": "cool_4c", "time": dt(60)}],
               temperature=temps(60, 120, 15))
    z = Sample(id="Z", kind=SampleKind.COMPOSITE, items=["cod"],
               sampling_start=dt(120), sampling_end=dt(120),
               parent_ids=["X", "Y"], merged_at=dt(120),
               temperature=temps(120, 150, 10))
    r = client.post(
        "/api/v1/judgments/trial",
        json=request([x, y, z], eval_min=150).model_dump(mode="json"))
    body = r.json()
    cz = {x["item"]: x for x in body["clocks"] if x["sample_id"] == "Z"}["cod"]
    assert cz["origin_sample_id"] == "X"          # 最早组成样
    assert cz["basis_time"] == izo(0)
    assert cz["merged_at"] == izo(120)
    # 预处理截止 = 08:00 + 120 = 10:00；eval 10:30 -> 已超时
    assert cz["status"] == "overdue"
    # 历史收集存在合样墙说明
    assert any("合样墙" in stp["detail"] for stp in cz["derivation"])


def test_source_cycle_detected(client):
    from app.models import Sample, SampleKind
    a = Sample(id="A", kind=SampleKind.ALIQUOT, items=["cod"],
               sampling_start=dt(0), sampling_end=dt(0), parent_ids=["B"],
               temperature=temps(0, 10, 10))
    b = Sample(id="B", kind=SampleKind.ALIQUOT, items=["cod"],
               sampling_start=dt(0), sampling_end=dt(0), parent_ids=["A"],
               temperature=temps(0, 10, 10))
    r = client.post(
        "/api/v1/judgments/trial",
        json=request([a, b], eval_min=10).model_dump(mode="json"))
    body = r.json()
    codes = {v["code"] for v in body["violations"]}
    assert "source_cycle" in codes
    assert all(c["status"] == "invalid" for c in body["clocks"])


def test_source_break_detected(client):
    from app.models import Sample, SampleKind
    a = Sample(id="A", kind=SampleKind.ALIQUOT, items=["cod"],
               sampling_start=dt(0), sampling_end=dt(0), parent_ids=["GHOST"],
               temperature=temps(0, 10, 10))
    r = client.post(
        "/api/v1/judgments/trial",
        json=request([a], eval_min=10).model_dump(mode="json"))
    body = r.json()
    assert any(v["code"] == "source_break" for v in body["violations"])
    assert body["clocks"][0]["status"] == "invalid"


# ============================================================== 违规 ----

def test_temperature_excursion(client):
    from app.models import TemperaturePoint
    bad = temps(0, 30, 10) + [TemperaturePoint(time=dt(20), temp_c=8.5)]
    s = make_sample(temperature=bad)
    r = client.post("/api/v1/judgments/trial",
                    json=request([s]).model_dump(mode="json"))
    codes = {v["code"] for v in r.json()["violations"]}
    assert "temperature_excursion" in codes


def test_temperature_gap(client):
    # 20 -> 60 间隔 40 分钟 > 15 容差
    from app.models import TemperaturePoint
    pts = [TemperaturePoint(time=dt(0), temp_c=3.0),
           TemperaturePoint(time=dt(20), temp_c=3.0),
           TemperaturePoint(time=dt(60), temp_c=3.0)]
    s = make_sample(temperature=pts)
    r = client.post("/api/v1/judgments/trial",
                    json=request([s], eval_min=60).model_dump(mode="json"))
    v = next(v for v in r.json()["violations"] if v["code"] == "temperature_gap")
    assert v["items"] == ["cod", "oil"]
    intervals = []
    if "intervals" in v["detail"]:
        intervals = v["detail"]["intervals"]
    else:
        for occ in v["detail"]["occurrences"]:
            intervals.extend(occ.get("intervals", []))
    assert any(i["minutes"] == 40 for i in intervals)


def test_missing_preservation(client):
    s = make_sample(preservation=[])  # 无 cool_4c / dark
    r = client.post("/api/v1/judgments/trial",
                    json=request([s]).model_dump(mode="json"))
    codes = {v["code"]: v["code"] for v in r.json()["violations"]}
    assert "missing_preservation" in codes


def test_time_inversion_interval(client):
    s = make_sample(sampling_start=dt(10), sampling_end=dt(0))
    r = client.post("/api/v1/judgments/trial",
                    json=request([s]).model_dump(mode="json"))
    body = r.json()
    assert any(v["code"] == "time_inversion" for v in body["violations"])
    assert all(c["status"] == "invalid" for c in body["clocks"])


def test_eval_before_sampling_is_inversion(client):
    s = make_sample()
    r = client.post("/api/v1/judgments/trial",
                    json=request([s], eval_min=-10).model_dump(mode="json"))
    body = r.json()
    assert any(v["code"] == "time_inversion" for v in body["violations"])
    assert all(c["status"] == "invalid" for c in body["clocks"])


def test_late_transfer(client):
    s = make_sample(custody_transfers=[dt(700)])  # oil 分析截止 600
    r = client.post("/api/v1/judgments/trial",
                    json=request([s], eval_min=720,
                                 critical_within_minutes=60)
                    .model_dump(mode="json"))
    body = r.json()
    v = next(v for v in body["violations"] if v["code"] == "late_transfer")
    # 700 > 600(oil analysis) 与 120(cod pretreatment)
    assert set(v["items"]) == {"cod", "oil"}


# ============================================================== 版本化 ----

def test_receive_idempotent_and_immutable(client):
    s = make_sample()
    payload = request([s], eval_min=30)
    payload.idempotency_key = "K-1"
    j = payload.model_dump(mode="json")
    r1 = client.post("/api/v1/judgments", json=j)
    assert r1.status_code == 201, r1.text
    pid1 = r1.json()["package_id"]
    r2 = client.post("/api/v1/judgments", json=j)
    assert r2.status_code == 201
    assert r2.json()["package_id"] == pid1  # 幂等
    # 判定包可取回
    g = client.get(f"/api/v1/judgments/{pid1}")
    assert g.status_code == 200
    assert g.json()["package_id"] == pid1
    assert g.json()["trial"] is False


def test_rule_update_cannot_rewrite_old_version(client):
    s = make_sample()
    req1 = request([s], eval_min=30, rule_set=ruleset("v1"))
    r1 = client.post("/api/v1/judgments", json=req1.model_dump(mode="json"))
    pid1 = r1.json()["package_id"]
    hash1 = r1.json()["rule"]["content_hash"]

    from app.models import ItemRule, RuleSet
    new_items = [it.model_copy(update={"analysis_minutes": 9999})
                 for it in ruleset("v1").items]
    clash = RuleSet(version="v1", items=new_items)
    # 相同可读版本号但内容不同 -> 409
    req_clash = request([s], eval_min=30, rule_set=clash)
    rc = client.post("/api/v1/judgments", json=req_clash.model_dump(mode="json"))
    assert rc.status_code == 409

    # 用新版本号提交：旧包 hash 不变
    ok = RuleSet(version="v2", items=new_items)
    req2 = request([s], eval_min=30, rule_set=ok)
    r2 = client.post("/api/v1/judgments", json=req2.model_dump(mode="json"))
    assert r2.json()["rule"]["content_hash"] != hash1
    old = client.get(f"/api/v1/judgments/{pid1}").json()
    assert old["rule"]["content_hash"] == hash1


def test_supplement_creates_version_with_changes(client):
    from app.models import AnalysisEvent, TemperaturePoint
    s = make_sample(temperature=temps(0, 30, 10))
    req1 = request([s], eval_min=30)
    r1 = client.post("/api/v1/judgments", json=req1.model_dump(mode="json"))
    pid1 = r1.json()["package_id"]

    # 补录：oil 在 500 分钟完成分析，温度继续记录，eval 推进到 510
    supp = {
        "eval_time": dt(510).isoformat(),
        "analyses": [{"item": "oil", "time": dt(500).isoformat()}],
        "temperature": [{"time": dt(m).isoformat(), "temp_c": 3.0}
                        for m in range(40, 511, 10)],
    }
    r2 = client.post("/api/v1/samples/b1/supplement", json=supp)
    assert r2.status_code == 201, r2.text
    body = r2.json()
    assert body["version_no"] == 2
    assert body["changes_from"] == pid1
    fields = {(c["item"], c["field"]) for c in body["changes"]}
    assert ("oil", "status") in fields
    oil_new = {c["item"]: c for c in body["clocks"] if c["sample_id"] == "b1"}["oil"]
    assert oil_new["status"] == "completed"

    # 版本列表与旧包仍可取
    vers = client.get("/api/v1/samples/b1/versions").json()
    assert [v["version_no"] for v in vers["versions"]] == [2, 1]
    old = client.get(f"/api/v1/judgments/{pid1}").json()
    old_oil = {c["item"]: c for c in old["clocks"] if c["sample_id"] == "b1"}["oil"]
    assert old_oil["status"] != "completed"  # 旧判定未被改写

    # 版本比较端点
    d = client.get(f"/api/v1/judgments/{pid1}/diff/{body['package_id']}").json()
    assert d["change_count"] >= 1


def test_supplement_unknown_sample_404(client):
    r = client.post("/api/v1/samples/NOPE/supplement",
                    json={"eval_time": dt(10).isoformat()})
    assert r.status_code == 404


# ============================================================== 校验 ----

def test_trial_does_not_persist_rules(client):
    s = make_sample()
    payload = request([s]).model_dump(mode="json")
    r = client.post("/api/v1/judgments/trial", json=payload)
    assert r.status_code == 200
    # 试算中的 rule_set 不应被登记
    rules = client.get("/api/v1/rules").json()["rules"]
    assert rules == []
    # 试算包不可取回
    assert client.get(
        f"/api/v1/judgments/{r.json()['package_id']}"
    ).status_code == 404


def test_trial_with_unregistered_rule_version_404(client):
    s = make_sample()
    payload = request([s]).model_dump(mode="json")
    payload.pop("rule_set")
    payload["rule_version"] = "ghost-9"
    r = client.post("/api/v1/judgments/trial", json=payload)
    assert r.status_code == 404


def test_naive_datetime_rejected(client):
    s = make_sample()
    payload = request([s]).model_dump(mode="json")
    payload["eval_time"] = "2026-09-11T08:30:00"  # 无时区
    r = client.post("/api/v1/judgments/trial", json=payload)
    assert r.status_code == 422


def test_unknown_item_rejected(client):
    # 绕过 Python 侧模型校验，直接构造 JSON 让服务端返回 422
    payload = request([make_sample()]).model_dump(mode="json")
    payload["samples"][0]["items"].append("phantom")
    r = client.post("/api/v1/judgments/trial", json=payload)
    assert r.status_code == 422


def test_missing_rules_rejected(client):
    s = make_sample()
    payload = request([s]).model_dump(mode="json")
    payload.pop("rule_set")
    r = client.post("/api/v1/judgments/trial", json=payload)
    assert r.status_code == 422


def test_duplicate_rule_item_rejected(client):
    bad = {
        "version": "dup",
        "items": [
            {"item": "cod", "analysis_minutes": 10},
            {"item": "cod", "analysis_minutes": 20},
        ],
    }
    r = client.post("/api/v1/rules", json=bad)
    assert r.status_code == 422


def test_rule_registration_and_diff(client):
    r1 = client.post("/api/v1/rules", json=ruleset("v1").model_dump(mode="json"))
    assert r1.status_code == 201 and r1.json()["created"] is True
    r2 = client.post("/api/v1/rules", json=ruleset("v1").model_dump(mode="json"))
    assert r2.json()["created"] is False  # 内容去重

    from app.models import ItemRule, RuleSet
    v2 = RuleSet(version="v2", items=[
        it.model_copy(update={"analysis_minutes": it.analysis_minutes * 2})
        if it.item == "cod" else it for it in ruleset().items
    ])
    client.post("/api/v1/rules", json=v2.model_dump(mode="json"))
    d = client.post("/api/v1/rules/diff",
                    json={"old_version": "v1", "new_version": "v2"})
    assert d.status_code == 200
    ch = [c for c in d.json()["changes"] if c["item"] == "cod"]
    assert any(c["field"] == "analysis_minutes" for c in ch)


def test_priority_batches_ordered_by_due(client):
    from app.models import Sample, SampleKind
    # A 瓶 oil（截止 600），B 瓶 cod（预处理截止 120）-> B 先收
    a = make_sample(id="A", items=["oil"])
    b = Sample(id="B", kind=SampleKind.GRAB, items=["cod"],
               sampling_start=dt(0), sampling_end=dt(0),
               preservation=[{"name": "cool_4c", "time": dt(0)}],
               temperature=temps(0, 30, 10), custody_transfers=[dt(10)])
    r = client.post(
        "/api/v1/judgments/trial",
        json=request([a, b], eval_min=30).model_dump(mode="json"))
    batches = r.json()["priority_batches"]
    assert [b["sample_id"] for b in batches][:2] == ["B", "A"]
