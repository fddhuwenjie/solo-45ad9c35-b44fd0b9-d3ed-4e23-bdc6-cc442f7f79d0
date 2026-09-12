"""端到端演示（走 HTTP）：

  .venv/bin/python scripts/demo.py

覆盖：正常、临界、超时、来源成环、正式接收与幂等、规则更新不可改写旧判定、
补录新版本与状态变化、版本比较。使用独立临时数据库。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DEADLINE_DB", os.path.join(tempfile.mkdtemp(), "demo.db"))

from fastapi.testclient import TestClient

from app.main import app

T0 = "2026-09-11T08:00:00+08:00"  # = 00:00 UTC


def at(minute: int) -> str:
    from datetime import datetime, timedelta, timezone
    t0 = datetime.fromisoformat(T0)
    return (t0 + timedelta(minutes=minute)).isoformat()


def temps(a: int, b: int, step: int = 10, temp: float = 3.0) -> list[dict]:
    return [{"time": at(m), "temp_c": temp}
            for m in range(a, b + 1, step)]


RULES_V1 = {
    "version": "HJ-2026.1",
    "name": "environmental-deadline-rules",
    "items": [
        {"item": "cod", "item_name": "化学需氧量", "min_temp_c": 0, "max_temp_c": 4,
         "pretreatment_minutes": 120, "analysis_minutes": 1440,
         "required_preservation": ["cool_4c"], "continuous_basis": "end",
         "effective_to": "2027-01-01T00:00:00+08:00"},
        {"item": "oil", "item_name": "石油类", "min_temp_c": 0, "max_temp_c": 4,
         "pretreatment_minutes": None, "analysis_minutes": 600,
         "required_preservation": ["cool_4c", "dark"], "continuous_basis": "end",
         "effective_to": "2027-01-01T00:00:00+08:00"},
        {"item": "nh3n", "item_name": "氨氮", "min_temp_c": 0, "max_temp_c": 4,
         "pretreatment_minutes": 240, "analysis_minutes": 1440,
         "required_preservation": ["add_acid"], "continuous_basis": "end",
         "effective_to": "2027-01-01T00:00:00+08:00"},
    ],
}

GRAB = {
    "id": "B-001", "kind": "grab", "items": ["cod", "oil", "nh3n"],
    "container": "glass_amber",
    "sampling_start": at(0), "sampling_end": at(0),
    "preservation": [
        {"name": "cool_4c", "time": at(2)},
        {"name": "dark", "time": at(2)},
        {"name": "add_acid", "time": at(3)},
    ],
    "temperature": temps(0, 300, 10),
    "pretreatments": [],
    "analyses": [],
    "custody_transfers": [at(20), at(90)],
}


def envelope(eval_min: int, samples, **kw) -> dict:
    body = {
        "request_id": kw.pop("request_id", f"REQ-{eval_min}"),
        "eval_time": at(eval_min),
        "critical_within_minutes": 60,
        "rule_set": RULES_V1,
        "samples": samples,
    }
    body.update(kw)
    return body


def show(title: str, body: dict) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("-" * 78)
    print(f"package_id : {body['package_id']}  (v{body['version_no']},"
          f" trial={body['trial']})")
    print(f"规则       : {body['rule']['version']} @ {body['rule']['content_hash'][:12]}")
    print("时钟:")
    for c in body["clocks"]:
        rem = "—" if c["remaining_minutes"] is None else f"{c['remaining_minutes']} 分钟"
        print(f"  {c['sample_id']:<8} {c['item']:<5} 状态={c['status']:<8} "
              f"下一步={str(c['next_action']):<13} 剩余={rem:<10} "
              f"最晚操作={c['latest_operation_at']} 来源={c['rule_source']}")
    print("优先收样批次:")
    for b in body["priority_batches"]:
        print(f"  {b['sample_id']} 项目={b['items']} {b['reason']}")
    if body["violations"]:
        print("违规/告警:")
        for v in body["violations"]:
            print(f"  [{v['code']}] {v['sample_id']} items={v['items']} :: {v['message']}")
    if body.get("changes"):
        print("相对上版本的状态变化:")
        for ch in body["changes"]:
            print(f"  {ch['sample_id']}/{ch['item']} {ch['field']}: "
                  f"{ch['before']} -> {ch['after']}")
    print("汇总:", body["summary"])


def main() -> None:
    with TestClient(app) as c:
        # 1) 正常试算（eval=100 分钟：cod 预处理剩 20 -> 临界）
        r = c.post("/api/v1/judgments/trial", json=envelope(100, [GRAB]))
        show("① 试算·临界（eval=采样后 100 分钟，cod 预处理剩 20 分钟）", r.json())

        # 2) 正常·正式接收（eval=30 分钟，全部充裕）
        r = c.post("/api/v1/judgments", json=envelope(30, [GRAB],
                                                      idempotency_key="batch-001",
                                                      request_id="REQ-OK"))
        assert r.status_code == 201, r.text
        ok_pkg = r.json()
        show("② 正式接收·正常（eval=30 分钟）", ok_pkg)
        pid_v1 = ok_pkg["package_id"]

        # 幂等重放
        r2 = c.post("/api/v1/judgments", json=envelope(30, [GRAB],
                                                       idempotency_key="batch-001",
                                                       request_id="REQ-OK"))
        assert r2.json()["package_id"] == pid_v1
        print("\n幂等重放返回同一 package_id:", pid_v1)

        # 3) 超时（eval=700：oil 分析截止 600、cod 预处理 120 均已超；650 分交接亦逾期）
        late = json.loads(json.dumps(GRAB))
        late["id"] = "B-002"
        late["temperature"] = temps(0, 700, 10)
        late["custody_transfers"] = [at(20), at(650)]
        r = c.post("/api/v1/judgments/trial", json=envelope(700, [late]))
        show("③ 试算·超时（eval=700 分钟，且 650 分交接晚于截止时刻）", r.json())

        # 4) 来源成环 + 温度越界
        cyc_temps = temps(5, 60, 10) + [{"time": at(45), "temp_c": 9.0}]
        cyc_temps.sort(key=lambda p: p["time"])
        a = {"id": "A", "kind": "aliquot", "items": ["cod"],
             "sampling_start": at(5), "sampling_end": at(5), "parent_ids": ["B"],
             "preservation": [{"name": "cool_4c", "time": at(5)}],
             "temperature": cyc_temps}
        b = {"id": "B", "kind": "aliquot", "items": ["cod"],
             "sampling_start": at(5), "sampling_end": at(5), "parent_ids": ["A"],
             "preservation": [{"name": "cool_4c", "time": at(5)}],
             "temperature": cyc_temps}
        r = c.post("/api/v1/judgments/trial", json=envelope(60, [a, b]))
        show("④ 试算·来源成环 + 温度越界", r.json())

        # 5) 规则更新与适用范围冲突预检
        # 5a) 同适用范围、同生效区间改限值 -> 409（会导致同一时钟多同等候选）
        rules_v2_overlap = json.loads(json.dumps(RULES_V1))
        rules_v2_overlap["version"] = "HJ-2026.2"
        rules_v2_overlap["items"][0]["analysis_minutes"] = 2880
        r_overlap = c.post("/api/v1/rules", json=rules_v2_overlap)
        print("\n⑤a 同适用范围改限值登记 ->", r_overlap.status_code,
              "（", len(r_overlap.json()["detail"]["conflicts"]), "条适用范围冲突）")

        # 5b) dry_run 预检（不写库）
        dr = c.post("/api/v1/rules?dry_run=true", json=rules_v2_overlap).json()
        print("⑤b dry_run 预检: would_register =", dr["would_register"],
              "conflict_count =", dr["conflict_count"])

        # 5c) 旧版本号绑定不同内容 -> 409
        clash = json.loads(json.dumps(rules_v2_overlap))
        clash["version"] = "HJ-2026.1"
        r_clash = c.post("/api/v1/rules", json=clash)
        print("⑤c 复用旧版本号提交不同内容 ->", r_clash.status_code)

        # 5d) 合法演进：v2 错峰生效（2027-01-01 起），与在库规则不重叠
        successor = "2027-01-01T00:00:00+08:00"
        rules_v2 = json.loads(json.dumps(rules_v2_overlap))
        for it in rules_v2["items"]:
            it["effective_from"] = successor
            it["effective_to"] = None  # 新标准向后无界
        r_new = c.post("/api/v1/rules", json=rules_v2)
        print("⑤d 错峰生效新版本登记 ->", r_new.status_code, r_new.json().get("created"))

        # 6) 补录：oil 在 500 分钟完成分析，温度补齐，eval=550
        supp = {
            "eval_time": at(550),
            "analyses": [{"item": "oil", "time": at(500)}],
            "temperature": temps(300, 550, 10),
            "custody_transfers": [at(480)],
        }
        r = c.post("/api/v1/samples/B-001/supplement", json=supp)
        assert r.status_code == 201, r.text
        v2_pkg = r.json()
        show("⑥ 补录·新版本（oil 已完成分析，eval=550）", v2_pkg)

        # 7) 版本比较
        d = c.get(f"/api/v1/judgments/{pid_v1}/diff/{v2_pkg['package_id']}").json()
        print("\n⑦ 版本比较", d["from"]["package_id"], "->", d["to"]["package_id"],
              f"共 {d['change_count']} 处变化:")
        for ch in d["changes"][:8]:
            print("  ", ch)

        # 8) 版本清单与旧包仍为旧规则
        vers = c.get("/api/v1/samples/B-001/versions").json()
        print("\n⑧ B-001 版本链:", [(v["version_no"], v["package_id"])
                                  for v in vers["versions"]])
        old = c.get(f"/api/v1/judgments/{pid_v1}").json()
        print("旧包规则哈希仍为:", old["rule"]["content_hash"][:12],
              "（规则更新未改写旧判定）")

        print("\n演示完成。")


if __name__ == "__main__":
    main()
