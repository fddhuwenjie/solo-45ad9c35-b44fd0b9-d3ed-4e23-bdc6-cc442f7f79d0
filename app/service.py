"""编排：规则解析 -> 引擎判定 -> 持久化/版本管理。"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import HTTPException

from . import db
from .engine import evaluate
from .models import (
    JudgmentRequest,
    JudgmentResult,
    RuleSet,
    StatusChange,
    SupplementEvent,
    Sample,
    utc_now,
)
from .versioning import canonical_json, diff_judgments, hash_rules


def resolve_rules(request: JudgmentRequest, *, persist: bool) -> tuple[RuleSet, str]:
    """返回 (规则集, content_hash)。

    * 仅 rule_version：从库中取，找不到则 404（只读）。
    * 携带 rule_set：
      - persist=True（正式接收）：登记；版本标签被其它内容占用 -> 409，
        或相同内容绑定了不同版本标签 -> 409；
      - persist=False（试算）：只按内容计算哈希，不写库。
    """
    if request.rule_set is not None and request.rule_version:
        h = hash_rules(request.rule_set)
        found = db.find_rule_by_version(request.rule_version)
        if found and found[0] != h:
            raise HTTPException(
                409,
                f"规则版本 {request.rule_version} 已登记为不同内容 "
                f"({found[0][:12]})；规则更新请使用新版本号，旧判定不可改写",
            )
        if persist:
            db.register_rule_set(request.rule_set)
        return request.rule_set, h

    if request.rule_set is not None:
        if persist:
            h, _ = db.register_rule_set(request.rule_set)
        else:
            h = hash_rules(request.rule_set)
        return request.rule_set, h

    found = db.find_rule_by_version(request.rule_version)  # type: ignore[arg-type]
    if not found:
        raise HTTPException(404, f"规则版本未登记: {request.rule_version}")
    return found[1], found[0]


def _package_id(trial: bool, sample_id: Optional[str]) -> str:
    raw = uuid.uuid4().hex
    if trial:
        return f"trial-{raw}"
    if sample_id:
        return f"pkg-s-{raw}"
    return f"pkg-{raw}"


def _build_result(
    request: JudgmentRequest,
    *,
    trial: bool,
    version_no: int,
    sample_id: Optional[str] = None,
    changes_from: Optional[str] = None,
    changes: Optional[list[StatusChange]] = None,
) -> tuple[JudgmentResult, str]:
    rule_set, content_hash = resolve_rules(request, persist=not trial)
    result = evaluate(
        request=request,
        rule_set=rule_set,
        content_hash=content_hash,
        package_id=_package_id(trial, sample_id),
        version_no=version_no,
        trial=trial,
        created_at=utc_now(),
        changes_from=changes_from,
        changes=changes,
        sample_id=sample_id,
    )
    return result, canonical_json(request.model_dump(mode="json"))


def run_judgment(
    request: JudgmentRequest,
    *,
    trial: bool,
    version_no: int = 1,
    sample_id: Optional[str] = None,
    changes_from: Optional[str] = None,
    changes: Optional[list[StatusChange]] = None,
) -> JudgmentResult:
    result, req_json = _build_result(
        request, trial=trial, version_no=version_no, sample_id=sample_id,
        changes_from=changes_from, changes=changes,
    )
    if not trial:
        result = db.save_judgment(
            result, trial=False,
            idempotency_key=request.idempotency_key, request_json=req_json,
        )
    return result


# ---------------------------------------------------------------- 补录 ----

def _append_events(target: Sample, ev: SupplementEvent) -> None:
    target.preservation = list(target.preservation) + list(ev.preservation)
    target.temperature = list(target.temperature) + list(ev.temperature)
    target.pretreatments = list(target.pretreatments) + list(ev.pretreatments)
    target.analyses = list(target.analyses) + list(ev.analyses)
    target.custody_transfers = list(target.custody_transfers) + list(
        ev.custody_transfers
    )


def _to_status_changes(raw_changes: list[dict], sample_id: str) -> list[StatusChange]:
    out: list[StatusChange] = []
    for c in raw_changes:
        field = c.get("field") or c.get("kind") or "clock"
        before = c.get("before")
        after = c.get("after")
        if c.get("kind") == "clock_added":
            before, after = None, "clock_present"
        elif c.get("kind") == "clock_removed":
            before, after = "clock_present", None
        out.append(StatusChange(
            sample_id=c.get("sample_id") or sample_id,
            item=c.get("item"),  # type: ignore[arg-type]
            field=field,
            before=None if before is None else str(before),
            after=None if after is None else str(after),
        ))
    return out


def supplement_judgment(
    sample_id: str, ev: SupplementEvent
) -> JudgmentResult:
    """在该样品最近一次判定基础上追加补录事件，生成新版本并列出状态变化。"""
    latest_pkg = db.latest_sample_package(sample_id)
    if not latest_pkg:
        raise HTTPException(404, f"样品 {sample_id} 没有可补录的历史判定")
    stored_req = db.get_stored_request(latest_pkg["package_id"])
    if not stored_req:
        raise HTTPException(
            409, f"样品 {sample_id} 的历史判定缺少请求快照，无法补录"
        )

    request = JudgmentRequest.model_validate(stored_req)
    target = next((s for s in request.samples if s.id == sample_id), None)
    if target is None:
        raise HTTPException(
            404, f"样品 {sample_id} 不在判定包 {latest_pkg['package_id']} 中"
        )
    _append_events(target, ev)
    request.eval_time = ev.eval_time
    if ev.critical_within_minutes is not None:
        request.critical_within_minutes = ev.critical_within_minutes
    request.idempotency_key = None  # 补录是追加的新版本
    request.request_id = (
        f"{request.request_id or latest_pkg['package_id']}:supplement"
    )

    # 先在试算模式下计算新结果（不落库），与上一版本比较状态变化
    new_version_no = db.next_sample_version(sample_id)
    preview, _ = _build_result(
        request, trial=True,
        version_no=new_version_no,
        sample_id=sample_id, changes_from=latest_pkg["package_id"],
    )
    raw_changes = diff_judgments(
        latest_pkg, preview.model_dump(mode="json")
    )
    changes = _to_status_changes(raw_changes, sample_id)

    # 正式持久化：生成正式 package_id（非 trial- 前缀），时间戳沿用 preview
    rule_set, content_hash = resolve_rules(request, persist=True)
    from .engine import evaluate as _evaluate
    result = _evaluate(
        request=request, rule_set=rule_set, content_hash=content_hash,
        package_id=_package_id(False, sample_id), version_no=new_version_no,
        trial=False, created_at=preview.created_at,
        changes_from=latest_pkg["package_id"], changes=changes,
        sample_id=sample_id,
    )
    result = db.save_judgment(
        result, trial=False, idempotency_key=None,
        request_json=canonical_json(request.model_dump(mode="json")),
    )
    return result
