"""规则内容哈希与版本比较。

规则身份以“规范化 JSON 的 SHA-256”为准，因此规则更新（即使沿用旧版本号）
必然产生新的 content_hash；判定包只引用 content_hash，旧判定永不被改写。
"""
from __future__ import annotations

import copy
import json
from datetime import timezone
from typing import Any, Iterable

from .models import ResourceSet, RuleSet


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _iso(dt):
    return None if dt is None else dt.astimezone(timezone.utc).isoformat()


def rule_content(rule_set: RuleSet) -> dict:
    """决定时限行为的内容（去掉 note 等不影响判定的字段）。

    适用范围（生效区间、基质/方法/容器/保存条件）与 rule_id 都是规则身份的
    一部分：同样限值但适用条件不同的规则项哈希必然不同。
    """
    return {
        "name": rule_set.name,
        "items": [
            {
                "item": r.item,
                "rule_id": r.rule_id,
                "effective_from": _iso(r.effective_from),
                "effective_to": _iso(r.effective_to),
                "matrices": sorted(r.matrices),
                "methods": sorted(r.methods),
                "containers": sorted(r.containers),
                "storage_conditions": sorted(r.storage_conditions),
                "min_temp_c": r.min_temp_c,
                "max_temp_c": r.max_temp_c,
                "pretreatment_minutes": r.pretreatment_minutes,
                "analysis_minutes": r.analysis_minutes,
                "required_preservation": sorted(r.required_preservation),
                "continuous_basis": r.continuous_basis.value,
            }
            for r in sorted(rule_set.items, key=lambda x: (x.item, x.rule_id))
        ],
    }


def hash_rules(rule_set: RuleSet) -> str:
    import hashlib

    payload = canonical_json(rule_content(rule_set)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _index_rules(rule_set: RuleSet) -> dict[tuple[str, str], dict]:
    return {(r.item, r.rule_id): r.model_dump(mode="json")
            for r in rule_set.items}


def diff_rule_sets(old: RuleSet, new: RuleSet) -> list[dict]:
    """逐规则项比较两套规则，返回可读的差异列表。"""
    changes: list[dict] = []
    old_idx = _index_rules(old)
    new_idx = _index_rules(new)
    fields = (
        "effective_from",
        "effective_to",
        "matrices",
        "methods",
        "containers",
        "storage_conditions",
        "min_temp_c",
        "max_temp_c",
        "pretreatment_minutes",
        "analysis_minutes",
        "required_preservation",
        "continuous_basis",
    )
    for key in sorted(set(old_idx) | set(new_idx)):
        item, rule_id = key
        if key not in old_idx:
            changes.append({"item": item, "rule_id": rule_id,
                            "kind": "item_added", "new": new_idx[key]})
            continue
        if key not in new_idx:
            changes.append({"item": item, "rule_id": rule_id,
                            "kind": "item_removed", "old": old_idx[key]})
            continue
        for f in fields:
            if old_idx[key].get(f) != new_idx[key].get(f):
                changes.append(
                    {
                        "item": item,
                        "rule_id": rule_id,
                        "kind": "field_changed",
                        "field": f,
                        "old": old_idx[key].get(f),
                        "new": new_idx[key].get(f),
                    }
                )
    return changes


def _clock_view(c: dict) -> tuple:
    """从判定包 dict 中抽取可比较的时钟状态视图。"""
    mr = c.get("matched_rule")
    return (
        c["sample_id"],
        c["item"],
        c["status"],
        c.get("conclusive"),
        c["conforming"],
        c["remaining_minutes"],
        None if c["next_action_deadline"] is None else c["next_action_deadline"],
        c["next_action"],
        None if c["latest_operation_at"] is None else c["latest_operation_at"],
        tuple((p["phase"], p["status"], p["remaining_minutes"]) for p in c["phases"]),
        None if mr is None else (mr["content_hash"], mr["rule_id"]),
    )


def diff_judgments(old_pkg: dict, new_pkg: dict) -> list[dict]:
    """比较两个判定包（dict），给出每个样品—项目时钟的状态变化。"""
    old_map = {
        (c["sample_id"], c["item"]): c for c in old_pkg.get("clocks", [])
    }
    new_map = {
        (c["sample_id"], c["item"]): c for c in new_pkg.get("clocks", [])
    }
    changes: list[dict] = []
    keys = sorted(set(old_map) | set(new_map))
    for k in keys:
        sid, item = k
        if k not in old_map:
            changes.append(
                {"sample_id": sid, "item": item, "kind": "clock_added",
                 "after": _clock_view(new_map[k])[2:]}
            )
            continue
        if k not in new_map:
            changes.append(
                {"sample_id": sid, "item": item, "kind": "clock_removed",
                 "before": _clock_view(old_map[k])[2:]}
            )
            continue
        oc, nc = old_map[k], new_map[k]
        for field, old_v, new_v in (
            ("status", oc["status"], nc["status"]),
            ("conforming", oc["conforming"], nc["conforming"]),
            ("remaining_minutes", oc["remaining_minutes"], nc["remaining_minutes"]),
            ("next_action_deadline", oc["next_action_deadline"],
             nc["next_action_deadline"]),
            ("next_action", oc["next_action"], nc["next_action"]),
            ("latest_operation_at", oc["latest_operation_at"],
             nc["latest_operation_at"]),
        ):
            if old_v != new_v:
                changes.append(
                    {
                        "sample_id": sid,
                        "item": item,
                        "kind": "field_changed",
                        "field": field,
                        "before": str(old_v),
                        "after": str(new_v),
                    }
                )
        om = oc.get("matched_rule")
        nm = nc.get("matched_rule")
        om_id = None if om is None else (om["content_hash"], om["rule_id"])
        nm_id = None if nm is None else (nm["content_hash"], nm["rule_id"])
        if om_id != nm_id:
            changes.append(
                {
                    "sample_id": sid,
                    "item": item,
                    "kind": "rule_reselected",
                    "field": "matched_rule",
                    "before": None if om is None else f"{om['version']}#{om['rule_id']}",
                    "after": None if nm is None else f"{nm['version']}#{nm['rule_id']}",
                }
            )

    def vkey(v: dict) -> tuple:
        return (v["code"], v["sample_id"], tuple(sorted(v["items"])), v["message"])

    old_v = {vkey(v) for v in old_pkg.get("violations", [])}
    new_v = {vkey(v) for v in new_pkg.get("violations", [])}
    for v in sorted(new_v - old_v):
        changes.append(
            {"sample_id": v[1], "item": None, "kind": "violation_added",
             "field": v[0], "before": None, "after": v[3]}
        )
    for v in sorted(old_v - new_v):
        changes.append(
            {"sample_id": v[1], "item": None, "kind": "violation_cleared",
             "field": v[0], "before": v[3], "after": None}
        )
    return changes


def copy_rule_set(rule_set: RuleSet) -> RuleSet:
    return RuleSet.model_validate(copy.deepcopy(rule_set.model_dump()))


# ---------------------------------------------------------------- 资源 ----

def resource_content(resource_set: ResourceSet) -> dict:
    """决定排程行为的资源内容（note 等不影响排程的字段不参与哈希）。"""
    def win(w) -> dict:
        return {"start": _iso(w.start), "end": _iso(w.end)}

    return {
        "name": resource_set.name,
        "resources": [
            {
                "resource_id": r.resource_id,
                "kind": r.kind.value,
                "methods": sorted(r.methods),
                "capacity": r.capacity,
                "task_minutes": r.task_minutes,
                "switch_minutes": r.switch_minutes,
                "windows": [win(w) for w in sorted(
                    r.windows, key=lambda x: (x.start, x.end))],
                "downtime": [win(w) for w in sorted(
                    r.downtime, key=lambda x: (x.start, x.end))],
            }
            for r in sorted(resource_set.resources,
                            key=lambda x: x.resource_id)
        ],
    }


def hash_resources(resource_set: ResourceSet) -> str:
    import hashlib

    payload = canonical_json(resource_content(resource_set)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------- 排程 ----

def _sched_task_brief(t: dict) -> dict:
    return {
        "resource_id": t["resource_id"],
        "batch_id": t["batch_id"],
        "start": t["start"],
        "end": t["end"],
        "on_time": t["on_time"],
        "frozen": t.get("frozen", False),
    }


def diff_schedules(old: dict, new: dict) -> list[dict]:
    """比较两个排程版本（dict），给出任务/冲突/汇总的差异。"""
    changes: list[dict] = []
    old_map = {(t["sample_id"], t["item"], t["phase"]): t
               for t in old.get("tasks", [])}
    new_map = {(t["sample_id"], t["item"], t["phase"]): t
               for t in new.get("tasks", [])}
    for key in sorted(set(old_map) | set(new_map)):
        sid, item, phase = key
        if key not in old_map:
            changes.append({
                "sample_id": sid, "item": item, "phase": phase,
                "kind": "task_added", "after": _sched_task_brief(new_map[key]),
            })
            continue
        if key not in new_map:
            changes.append({
                "sample_id": sid, "item": item, "phase": phase,
                "kind": "task_removed", "before": _sched_task_brief(old_map[key]),
            })
            continue
        oc, nc = old_map[key], new_map[key]
        for f in ("resource_id", "batch_id", "start", "end",
                  "slack_minutes", "on_time", "frozen"):
            if oc.get(f) != nc.get(f):
                changes.append({
                    "sample_id": sid, "item": item, "phase": phase,
                    "kind": "field_changed", "field": f,
                    "before": None if oc.get(f) is None else str(oc.get(f)),
                    "after": None if nc.get(f) is None else str(nc.get(f)),
                })

    old_c = {(c["sample_id"], c["item"], c["phase"]): c
             for c in old.get("conflicts", [])}
    new_c = {(c["sample_id"], c["item"], c["phase"]): c
             for c in new.get("conflicts", [])}
    for key in sorted(set(old_c) | set(new_c)):
        sid, item, phase = key
        if key not in old_c:
            changes.append({
                "sample_id": sid, "item": item, "phase": phase,
                "kind": "conflict_added",
                "after": new_c[key]["message"],
            })
        elif key not in new_c:
            changes.append({
                "sample_id": sid, "item": item, "phase": phase,
                "kind": "conflict_cleared",
                "before": old_c[key]["message"],
            })

    for f in ("total_tasks", "scheduled", "on_time",
              "method_switches", "conflicts"):
        ov = old.get("summary", {}).get(f)
        nv = new.get("summary", {}).get(f)
        if ov != nv:
            changes.append({
                "sample_id": None, "item": None, "phase": None,
                "kind": "summary_changed", "field": f,
                "before": None if ov is None else str(ov),
                "after": None if nv is None else str(nv),
            })
    return changes
