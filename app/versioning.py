"""规则内容哈希与版本比较。

规则身份以“规范化 JSON 的 SHA-256”为准，因此规则更新（即使沿用旧版本号）
必然产生新的 content_hash；判定包只引用 content_hash，旧判定永不被改写。
"""
from __future__ import annotations

import copy
import json
from typing import Any, Iterable

from .models import RuleSet


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def rule_content(rule_set: RuleSet) -> dict:
    """决定时限行为的内容（去掉 note 等不影响判定的字段）。"""
    return {
        "name": rule_set.name,
        "items": [
            {
                "item": r.item,
                "min_temp_c": r.min_temp_c,
                "max_temp_c": r.max_temp_c,
                "pretreatment_minutes": r.pretreatment_minutes,
                "analysis_minutes": r.analysis_minutes,
                "required_preservation": sorted(r.required_preservation),
                "continuous_basis": r.continuous_basis.value,
            }
            for r in sorted(rule_set.items, key=lambda x: x.item)
        ],
    }


def hash_rules(rule_set: RuleSet) -> str:
    import hashlib

    payload = canonical_json(rule_content(rule_set)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _index_rules(rule_set: RuleSet) -> dict[str, dict]:
    return {r.item: r.model_dump() for r in rule_set.items}


def diff_rule_sets(old: RuleSet, new: RuleSet) -> list[dict]:
    """逐项目比较两套规则，返回可读的差异列表。"""
    changes: list[dict] = []
    old_idx = _index_rules(old)
    new_idx = _index_rules(new)
    fields = (
        "min_temp_c",
        "max_temp_c",
        "pretreatment_minutes",
        "analysis_minutes",
        "required_preservation",
        "continuous_basis",
    )
    for item in sorted(set(old_idx) | set(new_idx)):
        if item not in old_idx:
            changes.append({"item": item, "kind": "item_added", "new": new_idx[item]})
            continue
        if item not in new_idx:
            changes.append({"item": item, "kind": "item_removed", "old": old_idx[item]})
            continue
        for f in fields:
            if old_idx[item].get(f) != new_idx[item].get(f):
                changes.append(
                    {
                        "item": item,
                        "kind": "field_changed",
                        "field": f,
                        "old": old_idx[item].get(f),
                        "new": new_idx[item].get(f),
                    }
                )
    return changes


def _clock_view(c: dict) -> tuple:
    """从判定包 dict 中抽取可比较的时钟状态视图。"""
    return (
        c["sample_id"],
        c["item"],
        c["status"],
        c["conforming"],
        c["remaining_minutes"],
        None if c["next_action_deadline"] is None else c["next_action_deadline"],
        c["next_action"],
        None if c["latest_operation_at"] is None else c["latest_operation_at"],
        tuple((p["phase"], p["status"], p["remaining_minutes"]) for p in c["phases"]),
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
