"""规则适用性匹配（纯函数，不依赖持久层）。

规则项的适用范围由五个维度组成：

* 生效区间 ``[effective_from, effective_to)``（任一端为空表示不限）；
* 样品基质 ``matrices``、分析方法 ``methods``、容器 ``containers``、
  保存条件 ``storage_conditions``——空列表表示该维度为通配。

样品侧的实际条件打包成 ``ctx`` 字典传入；字段来源（样品 id、字段名）由调用方
（引擎）负责补充，本模块只给出“要求值 vs 实际值”的判定。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

DIMENSIONS = ("matrix", "method", "container", "storage_condition")

_DIM_FIELDS = {
    "matrix": "matrices",
    "method": "methods",
    "container": "containers",
    "storage_condition": "storage_conditions",
}


def in_interval(
    t: Optional[datetime],
    start: Optional[datetime],
    end: Optional[datetime],
) -> bool:
    """半开区间 [start, end)；端点为空表示该侧无界。"""
    if t is None:
        return False
    if start is not None and t < start:
        return False
    if end is not None and t >= end:
        return False
    return True


def intervals_overlap(
    a0: Optional[datetime], a1: Optional[datetime],
    b0: Optional[datetime], b1: Optional[datetime],
) -> bool:
    """两个半开区间是否重叠（端点相接不算重叠）；None 端表示无界。"""
    # a 整体早于 b：a 有上界且 b 有下界，且 a1 <= b0
    if a1 is not None and b0 is not None and a1 <= b0:
        return False
    # b 整体早于 a
    if b1 is not None and a0 is not None and b1 <= a0:
        return False
    return True


def dimension_overlap(a: Optional[list[str]], b: Optional[list[str]]) -> bool:
    """两个适用维度是否可能同时成立：任一为通配（空）即重叠，否则取交集。"""
    a, b = a or [], b or []
    if not a or not b:
        return True
    return bool(set(a) & set(b))


def evaluate_applicability(rule: Any, ctx: dict) -> list[dict]:
    """返回规则在给定实际条件下未满足的维度列表；空列表表示完全匹配。

    ``ctx`` 键：basis_time / matrix / method / container / storage_condition。
    每项为 ``{"dimension", "required", "actual"}``，来源描述由引擎补。
    """
    unmet: list[dict] = []
    if not in_interval(ctx.get("basis_time"), rule.effective_from, rule.effective_to):
        unmet.append({
            "dimension": "effective_time",
            "required": [rule.effective_from, rule.effective_to],
            "actual": ctx.get("basis_time"),
        })
    for dim in DIMENSIONS:
        allowed = getattr(rule, _DIM_FIELDS[dim]) or []
        actual = ctx.get(dim)
        if allowed and actual not in allowed:
            unmet.append({
                "dimension": dim,
                "required": allowed,
                "actual": actual,
            })
    return unmet


def _same_limits(r1: Any, r2: Any) -> bool:
    return (
        r1.min_temp_c == r2.min_temp_c
        and r1.max_temp_c == r2.max_temp_c
        and r1.pretreatment_minutes == r2.pretreatment_minutes
        and r1.analysis_minutes == r2.analysis_minutes
        and sorted(r1.required_preservation) == sorted(r2.required_preservation)
        and r1.continuous_basis == r2.continuous_basis
    )


def _same_scope(r1: Any, r2: Any) -> bool:
    return (
        r1.effective_from == r2.effective_from
        and r1.effective_to == r2.effective_to
        and sorted(r1.matrices) == sorted(r2.matrices)
        and sorted(r1.methods) == sorted(r2.methods)
        and sorted(r1.containers) == sorted(r2.containers)
        and sorted(r1.storage_conditions) == sorted(r2.storage_conditions)
    )


def same_rule_item(r1: Any, r2: Any) -> bool:
    """两条项目规则的判定内容与适用范围完全相同（重复发布）。"""
    return _same_limits(r1, r2) and _same_scope(r1, r2)


def scope_conflict(r1: Any, r2: Any) -> Optional[dict]:
    """同一项目的两条规则是否存在适用范围冲突。

    冲突 = 生效区间重叠，且四个适用维度的取值集合都可能同时命中
    （通配与任何具体值重叠）。内容完全相同的重复发布不算冲突。
    返回冲突维度说明；不冲突返回 None。
    """
    if r1.item != r2.item or same_rule_item(r1, r2):
        return None
    if not intervals_overlap(
        r1.effective_from, r1.effective_to, r2.effective_from, r2.effective_to
    ):
        return None
    dims: dict[str, dict] = {}
    for dim in DIMENSIONS:
        field = _DIM_FIELDS[dim]
        a, b = getattr(r1, field) or [], getattr(r2, field) or []
        if not dimension_overlap(a, b):
            return None
        overlap = sorted(set(a) & set(b)) if (a and b) else []
        dims[dim] = {"a": a, "b": b, "intersection": overlap,
                     "wildcard": not a or not b}
    return {
        "item": r1.item,
        "effective_overlap": True,
        "dimensions": dims,
    }
