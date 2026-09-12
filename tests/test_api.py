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


def test_future_analysis_event_not_marked_completed(client):
    """eval=30、分析事件在 500（未来）：不得据此结束分析阶段，且报 time_inversion。"""
    from app.models import AnalysisEvent
    s = make_sample(analyses=[AnalysisEvent(item="oil", time=dt(500))],
                    temperature=temps(0, 30, 10))
    r = client.post("/api/v1/judgments/trial",
                    json=request([s], eval_min=30).model_dump(mode="json"))
    body = r.json()
    clocks = {c["item"]: c for c in body["clocks"]}
    oil = clocks["oil"]
    assert oil["status"] != "completed"
    ana_phase = {p["phase"]: p for p in oil["phases"]}["analysis"]
    assert ana_phase["status"] in ("pending", "critical")
    assert ana_phase["done_at"] is None
    # cod 时钟不受该未来分析事件影响
    assert clocks["cod"]["status"] == "ok"

    inv = [v for v in body["violations"] if v["code"] == "time_inversion"]
    assert inv, "应返回 time_inversion 违规"
    hit = [v for v in inv if "oil" in v["items"]]
    assert hit and all(v["sample_id"] == "b1" for v in hit)
    detail = hit[0]["detail"]
    occ = detail["occurrences"] if "occurrences" in detail else [detail]
    assert any(o.get("event_type") == "analysis" for o in occ)
    # 受影响时钟推导链必须说明该未来事件被忽略
    assert any(st["step"] == "ignore_future_events" for st in oil["derivation"])
    assert not oil["conforming"]


def test_future_pretreatment_event_does_not_end_phase(client):
    """未来的前处理事件不能把预处理阶段标为 completed。"""
    from app.models import PretreatmentEvent
    # cod 预处理期限 120；事件在 100（期限内）但 eval=30，属于未来事件
    s = make_sample(pretreatments=[PretreatmentEvent(type="digestion", time=dt(100))],
                    temperature=temps(0, 30, 10))
    r = client.post("/api/v1/judgments/trial",
                    json=request([s], eval_min=30).model_dump(mode="json"))
    cod = {c["item"]: c for c in r.json()["clocks"]}["cod"]
    pre = {p["phase"]: p for p in cod["phases"]}["pretreatment"]
    assert pre["status"] != "completed"
    assert pre["done_at"] is None
    assert any(v["code"] == "time_inversion" and "cod" in v["items"]
               for v in r.json()["violations"])


def test_future_transfer_ignored_and_flagged(client):
    """未来交接既不算已交接，也不据此判 late_transfer，只报 time_inversion。"""
    s = make_sample(custody_transfers=[dt(15), dt(700)],
                    temperature=temps(0, 30, 10))
    r = client.post("/api/v1/judgments/trial",
                    json=request([s], eval_min=30).model_dump(mode="json"))
    codes = {v["code"] for v in r.json()["violations"]}
    assert "time_inversion" in codes
    assert "late_transfer" not in codes  # 未来交接不参与晚交接判定


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
    from app.models import ItemRule, RuleSet
    boundary = dt(100000)
    # v1 规则带生效上界（模拟被替代的旧标准）
    v1_items = [
        it.model_copy(update={"effective_to": boundary})
        for it in ruleset("v1").items
    ]
    v1_set = RuleSet(version="v1", items=v1_items)
    s = make_sample()
    req1 = request([s], eval_min=30, rule_set=v1_set)
    r1 = client.post("/api/v1/judgments", json=req1.model_dump(mode="json"))
    pid1 = r1.json()["package_id"]
    hash1 = r1.json()["rule"]["content_hash"]

    new_items = [it.model_copy(update={"analysis_minutes": 9999})
                 for it in v1_set.items]
    clash = RuleSet(version="v1", items=new_items)
    # 相同可读版本号但内容不同：直接登记 -> 409
    rc = client.post("/api/v1/rules", json=clash.model_dump(mode="json"))
    assert rc.status_code == 409
    # 走正式接收携带该冲突集：被版本标签冲突或匹配守门拒绝（均为 4xx，不落库）
    req_clash = request([s], eval_min=30, rule_set=clash)
    rx = client.post("/api/v1/judgments", json=req_clash.model_dump(mode="json"))
    assert rx.status_code in (409, 422)

    # 同生效区间、同适用范围但改限值 -> 适用性冲突预检 409（会出现多同等候选）
    overlap = RuleSet(version="vX", items=new_items)
    ro = client.post("/api/v1/rules", json=overlap.model_dump(mode="json"))
    assert ro.status_code == 409

    # 合法演进：v2 从 boundary 时刻接续（区间不重叠），旧包 hash 不变
    future_items = [
        it.model_copy(update={
            "analysis_minutes": 9999,
            "effective_to": None,
            "effective_from": boundary,
        })
        for it in v1_set.items
    ]
    ok = RuleSet(version="v2", items=future_items)
    r2 = client.post("/api/v1/rules", json=ok.model_dump(mode="json"))
    assert r2.status_code == 201, r2.text
    assert r2.json()["content_hash"] != hash1
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


def test_supplement_version_isolated_per_sample(client):
    """A、B 同批生成 v1；仅补录 A：B 的版本列表/latest/比较基线都不得出现 A 的 v2。"""
    a = make_sample(id="SA")
    b = make_sample(id="SB")
    payload = request([a, b], eval_min=30, idempotency_key="batch-AB")
    r1 = client.post("/api/v1/judgments", json=payload.model_dump(mode="json"))
    assert r1.status_code == 201, r1.text
    batch_pkg = r1.json()["package_id"]

    # 补录只针对 A：oil 在 500 分钟完成分析，温度补齐，eval=510
    supp = {
        "eval_time": dt(510).isoformat(),
        "analyses": [{"item": "oil", "time": dt(500).isoformat()}],
        "temperature": [{"time": dt(m).isoformat(), "temp_c": 3.0}
                        for m in range(40, 511, 10)],
    }
    r2 = client.post("/api/v1/samples/SA/supplement", json=supp)
    assert r2.status_code == 201, r2.text
    a_v2 = r2.json()
    assert a_v2["sample_id"] == "SA"
    assert a_v2["version_no"] == 2
    assert a_v2["changes_from"] == batch_pkg
    # changes 只涉及 A，不波及 B
    assert all(ch["sample_id"] == "SA" for ch in a_v2["changes"])

    # A 的版本视图：v2（样品级）+ v1（批次级）
    va = client.get("/api/v1/samples/SA/versions").json()["versions"]
    assert [v["version_no"] for v in va] == [2, 1]
    assert va[0]["package_id"] == a_v2["package_id"]

    # B 的版本视图：只有 v1 批次包，绝不出现 A 的 v2
    vb = client.get("/api/v1/samples/SB/versions").json()["versions"]
    assert [v["version_no"] for v in vb] == [1]
    assert vb[0]["package_id"] == batch_pkg
    assert a_v2["package_id"] not in [v["package_id"] for v in vb]

    # B 的 latest 必须仍是批次包，且包内 sample_id 为空（不能返回 sample_id=SA）
    latest_b = client.get("/api/v1/samples/SB/latest").json()
    assert latest_b["package_id"] == batch_pkg
    assert latest_b["sample_id"] is None
    # A 的 latest 是其样品级 v2
    latest_a = client.get("/api/v1/samples/SA/latest").json()
    assert latest_a["package_id"] == a_v2["package_id"]
    assert latest_a["sample_id"] == "SA"

    # 比较基线：A 的 v2 基于批次包；用 B 视角 diff 批次包与 A 的 v2
    # 时，样品级包对 B 不可见 —— 直接对两包做版本比较端点（跨包）仍可用，
    # 但 B 的 latest 永不指向它。
    d = client.get(
        f"/api/v1/judgments/{batch_pkg}/diff/{a_v2['package_id']}"
    ).json()
    changed_samples = {c["sample_id"] for c in d["changes"]}
    assert "SB" not in changed_samples
    assert "SA" in changed_samples


def test_b_supplement_does_not_touch_a(client):
    """反向：只补录 B，A 的视图保持不变。"""
    a = make_sample(id="PA")
    b = make_sample(id="PB")
    payload = request([a, b], eval_min=30, idempotency_key="batch-P")
    batch_pkg = client.post("/api/v1/judgments",
                            json=payload.model_dump(mode="json")).json()["package_id"]
    supp = {"eval_time": dt(40).isoformat(),
            "temperature": [{"time": dt(40).isoformat(), "temp_c": 3.0}]}
    rb = client.post("/api/v1/samples/PB/supplement", json=supp)
    assert rb.status_code == 201
    # A 仍是批次 v1
    va = client.get("/api/v1/samples/PA/versions").json()["versions"]
    assert [v["version_no"] for v in va] == [1]
    assert client.get("/api/v1/samples/PA/latest").json()["package_id"] == batch_pkg


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
    # 不再强制提供 rule_set/rule_version：候选池为空时不报错（422），
    # 而是正常试算出无匹配时钟（indeterminate，不形成结论）
    s = make_sample()
    payload = request([s]).model_dump(mode="json")
    payload.pop("rule_set")
    r = client.post("/api/v1/judgments/trial", json=payload)
    assert r.status_code == 200
    cl = r.json()["clocks"]
    assert all(c["match_status"] == "none" for c in cl)
    assert all(c["status"] == "indeterminate" for c in cl)
    assert any(v["code"] == "rule_applicability"
               for v in r.json()["violations"])
    # 但正式接收无候选 -> 422（不得固化无结论判定）
    assert client.post("/api/v1/judgments", json=payload).status_code == 422


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
    from app.models import ItemRule, RuleSet
    # v1 规则带生效上界，v2 从该时刻接续（避免适用范围重叠预检）
    boundary = dt(100000)
    v1_ruleset = RuleSet(version="v1", items=[
        it.model_copy(update={"effective_to": boundary})
        for it in ruleset().items
    ])
    r1 = client.post("/api/v1/rules", json=v1_ruleset.model_dump(mode="json"))
    assert r1.status_code == 201 and r1.json()["created"] is True
    r2 = client.post("/api/v1/rules", json=v1_ruleset.model_dump(mode="json"))
    assert r2.json()["created"] is False  # 内容去重

    # 新版本规则从 boundary 生效，避免与 v1 在同一适用范围内重叠
    v2 = RuleSet(version="v2", items=[
        it.model_copy(update={
            "analysis_minutes": it.analysis_minutes * 2,
            "effective_from": boundary,
        })
        if it.item == "cod"
        else it.model_copy(update={"effective_from": boundary})
        for it in ruleset().items
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


# ====================================================== 规则适用性匹配 ----

from app.models import ItemRule, RuleSet as _RS, Sample as _S, SampleKind as _SK


def _scoped_rule(rule_id, item="cod", *, matrices=None, methods=None,
                 containers=None, storage=None, analysis=1440, pre=120,
                 eff_from=None, eff_to=None, preservation=None):
    return ItemRule(
        item=item, rule_id=rule_id,
        effective_from=eff_from, effective_to=eff_to,
        matrices=matrices or [], methods=methods or [],
        containers=containers or [], storage_conditions=storage or [],
        min_temp_c=0, max_temp_c=4,
        pretreatment_minutes=pre, analysis_minutes=analysis,
        required_preservation=preservation or ["cool_4c"],
    )


def _scoped_sample(sid="b1", item="cod", *, matrix=None, method=None,
                   container=None, storage=None):
    from app.models import PreservationAction
    return _S(
        id=sid, kind=_SK.GRAB, items=[item],
        container=container, matrix=matrix, storage_condition=storage,
        item_methods={item: method} if method else {},
        sampling_start=dt(0), sampling_end=dt(0),
        preservation=[PreservationAction(name="cool_4c", time=dt(0))],
        temperature=temps(0, 30, 10), custody_transfers=[dt(10)],
    )


def _trial(client, samples, rule_set, eval_min=30, **kw):
    from app.models import JudgmentRequest
    req = JudgmentRequest(
        eval_time=dt(eval_min), rule_set=rule_set, samples=samples,
        critical_within_minutes=60, **kw)
    return client.post("/api/v1/judgments/trial",
                       json=req.model_dump(mode="json"))


def test_applicability_matrix_method_selects_unique(client):
    surf = _RS(version="r1", items=[
        _scoped_rule("cod-surface", matrices=["surface_water"],
                     methods=["hj828"], analysis=1000)])
    waste = _RS(version="r2", items=[
        _scoped_rule("cod-waste", matrices=["wastewater"],
                     methods=["hj828"], analysis=500)])
    client.post("/api/v1/rules", json=surf.model_dump(mode="json"))
    client.post("/api/v1/rules", json=waste.model_dump(mode="json"))
    s = _scoped_sample(matrix="wastewater", method="hj828")
    from app.models import JudgmentRequest
    req = JudgmentRequest(eval_time=dt(30), samples=[s])
    r = client.post("/api/v1/judgments/trial", json=req.model_dump(mode="json"))
    cl = r.json()["clocks"][0]
    assert cl["match_status"] == "unique"
    assert cl["matched_rule"]["rule_id"] == "cod-waste"
    assert cl["status"] in ("ok", "critical")
    assert cl["conclusive"] is True
    # 未中选规则仍在候选列表中，并标明未满足的 matrix 维度及字段来源
    cand = {c["rule_id"]: c for c in cl["candidates"]}
    assert cand["cod-surface"]["applies"] is False
    unmet = cand["cod-surface"]["unmet"][0]
    assert unmet["dimension"] == "matrix"
    assert unmet["field_sources"][0]["field"] == "matrix"
    assert unmet["field_sources"][0]["value"] == "wastewater"


def test_applicability_no_match_is_indeterminate(client):
    surf = _RS(version="r1", items=[
        _scoped_rule("cod-surface", matrices=["surface_water"])])
    s = _scoped_sample(matrix="wastewater", method="hj828")
    r = _trial(client, [s], surf)
    cl = r.json()["clocks"][0]
    assert cl["status"] == "indeterminate"
    assert cl["match_status"] == "none"
    assert cl["conclusive"] is False and cl["conforming"] is False
    assert cl["latest_operation_at"] is None  # 无匹配不产出截止时刻
    assert cl["candidates"][0]["unmet"][0]["dimension"] == "matrix"
    assert any(v["code"] == "rule_applicability"
               for v in r.json()["violations"])
    # 正式接收被守门拒绝
    from app.models import JudgmentRequest
    req = JudgmentRequest(eval_time=dt(30), rule_set=surf, samples=[s])
    rc = client.post("/api/v1/judgments", json=req.model_dump(mode="json"))
    assert rc.status_code == 422
    assert any(c["item"] == "cod" for c in rc.json()["detail"]["blocked_clocks"])


def test_applicability_ambiguous_candidates(client):
    # 规则集内部重叠 -> 422（端点模型校验）
    rb = client.post("/api/v1/rules", json={
        "version": "bad", "items": [
            _scoped_rule("cod-a", analysis=1000).model_dump(mode="json"),
            _scoped_rule("cod-b", analysis=500).model_dump(mode="json"),
        ]})
    assert rb.status_code == 422

    # 跨规则集重叠：登记被预检 409 拦截，库内保证唯一；但试算携带未登记集
    # 与已登记集在同一条件下同时命中 -> 歧义时钟
    a = _RS(version="a", items=[_scoped_rule("cod-a", analysis=1000)])
    assert client.post(
        "/api/v1/rules", json=a.model_dump(mode="json")).status_code == 201
    b = _RS(version="b", items=[_scoped_rule("cod-b", analysis=500)])
    s = _scoped_sample(matrix="surface_water", method="hj828")
    from app.models import JudgmentRequest
    req = JudgmentRequest(eval_time=dt(30), rule_set=b, samples=[s])
    r = client.post("/api/v1/judgments/trial", json=req.model_dump(mode="json"))
    cl = r.json()["clocks"][0]
    assert cl["match_status"] == "ambiguous"
    assert cl["status"] == "indeterminate"
    assert len([c for c in cl["candidates"] if c["applies"]]) == 2
    assert cl["conclusive"] is False
    # 该歧义集不得登记
    assert client.post(
        "/api/v1/rules", json=b.model_dump(mode="json")).status_code == 409


def test_effective_interval_boundary_half_open(client):
    boundary = dt(60)
    old = _RS(version="old", items=[
        _scoped_rule("cod-old", eff_to=boundary, analysis=1000)])
    new = _RS(version="new", items=[
        _scoped_rule("cod-new", eff_from=boundary, analysis=500)])
    client.post("/api/v1/rules", json=old.model_dump(mode="json"))
    client.post("/api/v1/rules", json=new.model_dump(mode="json"))
    from app.models import JudgmentRequest
    s = _scoped_sample()
    # 采样在边界点（=effective_from new，含；=effective_to old，不含）-> 命中 new
    s_boundary = _scoped_sample()
    req = JudgmentRequest(
        eval_time=dt(90), samples=[s_boundary],
        rule_version=None)
    # 直接把采样时刻设到 boundary
    s_boundary.sampling_start = boundary
    s_boundary.sampling_end = boundary
    r = client.post("/api/v1/judgments/trial", json=req.model_dump(mode="json"))
    cl = r.json()["clocks"][0]
    assert cl["matched_rule"]["rule_id"] == "cod-new"
    assert cl["basis_time"] == boundary.isoformat().replace("+00:00", "Z")


def test_registration_conflict_precheck(client):
    a = _RS(version="a", items=[_scoped_rule("cod-a", analysis=1000)])
    client.post("/api/v1/rules", json=a.model_dump(mode="json"))
    # 干跑预检
    b = _RS(version="b", items=[_scoped_rule("cod-b", analysis=500)])
    dr = client.post("/api/v1/rules?dry_run=true",
                     json=b.model_dump(mode="json")).json()
    assert dr["would_register"] is False and dr["conflict_count"] >= 1
    assert dr["conflicts"][0]["item"] == "cod"
    # 正式登记被 409
    assert client.post("/api/v1/rules", json=b.model_dump(mode="json")).status_code == 409
    # 错峰生效：用独立项目验证“接续区间无冲突、可登记”（避开已存在的 cod 规则）
    boundary = dt(1000)
    old = client.post("/api/v1/rules", json=_RS(version="a2", items=[
        _scoped_rule("tn-old", item="tn", analysis=1000, eff_to=boundary)
    ]).model_dump(mode="json"))
    assert old.status_code == 201
    c = _RS(version="c", items=[
        _scoped_rule("tn-new", item="tn", analysis=500, eff_from=boundary)])
    ok = client.post("/api/v1/rules", json=c.model_dump(mode="json"))
    assert ok.status_code == 201


def test_trial_forced_candidate_comparison(client):
    surf = _RS(version="r1", items=[
        _scoped_rule("cod-surface", matrices=["surface_water"], analysis=1000)])
    waste = _RS(version="r2", items=[
        _scoped_rule("cod-waste", matrices=["wastewater"], analysis=500)])
    client.post("/api/v1/rules", json=surf.model_dump(mode="json"))
    client.post("/api/v1/rules", json=waste.model_dump(mode="json"))
    s = _scoped_sample(matrix="wastewater", method="hj828")
    # 不强制：唯一命中 cod-waste
    r0 = _trial(client, [s], None) if False else None
    from app.models import JudgmentRequest
    req = JudgmentRequest(eval_time=dt(30), samples=[s],
                          selected_candidates={
                              "b1/cod": {"rule_id": "cod-surface"}})
    r = client.post("/api/v1/judgments/trial", json=req.model_dump(mode="json"))
    cl = r.json()["clocks"][0]
    assert cl["match_status"] == "forced"
    assert cl["matched_rule"]["rule_id"] == "cod-surface"
    assert cl["conclusive"] is False  # 对照不得形成合规结论
    # 强制不存在的规则项 -> 422
    req_bad = JudgmentRequest(eval_time=dt(30), samples=[s],
                              selected_candidates={"b1/cod": {"rule_id": "ghost"}})
    assert client.post("/api/v1/judgments/trial",
                       json=req_bad.model_dump(mode="json")).status_code == 422


def test_insufficient_preservation_is_indeterminate(client):
    rs = _RS(version="r1", items=[
        _scoped_rule("cod-acid", matrices=["wastewater"],
                     preservation=["cool_4c", "add_acid"])])
    # 样品只做了 cool_4c，缺 add_acid
    s = _scoped_sample(matrix="wastewater", method="hj828")
    r = _trial(client, [s], rs)
    cl = r.json()["clocks"][0]
    assert cl["match_status"] == "unique"  # 规则唯一，但保存动作不足
    assert cl["status"] == "indeterminate"
    assert cl["conclusive"] is False
    v = next(v for v in r.json()["violations"]
             if v["code"] == "missing_preservation")
    assert "add_acid" in v["message"]
    # 截止时刻仍照算（供操作参考），但不形成合规结论，正式接收拒绝
    assert cl["latest_operation_at"] is not None
    from app.models import JudgmentRequest
    req = JudgmentRequest(eval_time=dt(30), rule_set=rs, samples=[s])
    assert client.post("/api/v1/judgments",
                       json=req.model_dump(mode="json")).status_code == 422


def test_impact_preview_lists_reselected(client):
    surf = _RS(version="r1", items=[
        _scoped_rule("cod-surface", matrices=["surface_water"], analysis=1000)])
    client.post("/api/v1/rules", json=surf.model_dump(mode="json"))
    s = _scoped_sample(matrix="surface_water", method="hj828")
    from app.models import JudgmentRequest
    req = JudgmentRequest(eval_time=dt(30), samples=[s],
                          idempotency_key="ik-impact")
    r1 = client.post("/api/v1/judgments", json=req.model_dump(mode="json"))
    assert r1.status_code == 201
    pid = r1.json()["package_id"]

    # 新规则：同基质同方法、生效重叠、限值更紧 -> 影响预览应报告该时钟改选/歧义
    tighter = _RS(version="r2", items=[
        _scoped_rule("cod-surface-v2", matrices=["surface_water"],
                     methods=["hj828"], analysis=300)])
    ip = client.post("/api/v1/rules/impact-preview",
                     json=tighter.model_dump(mode="json")).json()
    assert ip["stored_packages_scanned"] >= 1
    hit = [x for x in ip["reselected"] if x["sample_id"] == "b1"
           and x["item"] == "cod"]
    assert hit and hit[0]["before"]["rule_id"] == "cod-surface"
    assert hit[0]["after_match_status"] == "ambiguous"
    # 原判定未被改写
    old = client.get(f"/api/v1/judgments/{pid}").json()
    assert old["clocks"][0]["matched_rule"]["rule_id"] == "cod-surface"
    # 新规则未被登记
    assert all(r["version"] != "r2"
               for r in client.get("/api/v1/rules").json()["rules"])


def test_field_source_traces_to_origin_sample(client):
    """分样未提交基质/方法时，字段来源沿来源链回退到母体。"""
    from app.models import Sample, SampleKind
    rs = _RS(version="r1", items=[
        _scoped_rule("cod-gw", matrices=["groundwater"], analysis=700)])
    client.post("/api/v1/rules", json=rs.model_dump(mode="json"))
    mother = _scoped_sample(sid="M", matrix="groundwater", method="hj828")
    child = Sample(
        id="A", kind=SampleKind.ALIQUOT, items=["cod"],
        sampling_start=dt(10), sampling_end=dt(10), parent_ids=["M"],
        temperature=temps(10, 30, 10))
    from app.models import JudgmentRequest
    req = JudgmentRequest(eval_time=dt(30), samples=[mother, child])
    r = client.post("/api/v1/judgments/trial", json=req.model_dump(mode="json"))
    clocks = {c["sample_id"]: c for c in r.json()["clocks"]}
    ca = clocks["A"]
    assert ca["matched_rule"]["rule_id"] == "cod-gw"
    src = {f["field"]: f for f in ca["match_context"]["field_sources"]}
    assert src["matrix"]["source_sample_id"] == "M"
    assert src["matrix"]["value"] == "groundwater"
    assert src["method"]["source_sample_id"] == "M"
    assert src["basis_time"]["source_sample_id"] == "M"  # 基准也来自母体
