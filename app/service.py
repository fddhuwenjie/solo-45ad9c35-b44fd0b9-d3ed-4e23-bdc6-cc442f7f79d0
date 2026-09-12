"""编排：候选规则池 -> 引擎适用性匹配 -> 持久化/版本管理。

规则不再由调用方整包指定：候选池默认是全部已登记规则集，请求携带的 rule_set
（试算对照）或显式 rule_version 也并入池中。引擎按各时钟的原始采样时刻与实际
条件唯一匹配；正式接收只允许冻结唯一匹配的规则项。
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from fastapi import HTTPException

from . import db
from .engine import PoolEntry, evaluate
from .matching import scope_conflict
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


# --------------------------------------------------------- 候选池 ----

def build_pool(
    request: JudgmentRequest,
    *,
    extra_sets: Optional[list[tuple[str, RuleSet]]] = None,
) -> tuple[list[PoolEntry], dict[str, RuleSet]]:
    """构建候选规则池：全部已登记规则集 + 请求携带集 + 额外冻结集（补录）。

    返回 (entries, hash->RuleSet)。请求携带集若内容与已登记集相同则去重。
    已登记规则集内部的适用范围冲突在登记时拦截，这里假定库内规则两两不冲突。
    """
    registered = {h: rs for h, rs in db.all_rule_sets_payload()}
    sets_by_hash: dict[str, RuleSet] = dict(registered)

    def add(rs: RuleSet, h: str, adhoc: bool) -> None:
        sets_by_hash.setdefault(h, rs)

    if request.rule_set is not None:
        add(request.rule_set, hash_rules(request.rule_set), adhoc=True)
    if request.rule_version:
        found = db.find_rule_by_version(request.rule_version)
        if not found:
            raise HTTPException(404, f"规则版本未登记: {request.rule_version}")
        h, rs = found
        add(rs, h, adhoc=False)
    for h, rs in extra_sets or []:
        add(rs, h, adhoc=False)

    entries: list[PoolEntry] = []
    for h, rs in sets_by_hash.items():
        for r in rs.items:
            entries.append(PoolEntry(
                version=rs.version, content_hash=h, rule=r,
                adhoc=(request.rule_set is not None
                       and h == hash_rules(request.rule_set)
                       and h not in registered),
            ))
    return entries, sets_by_hash


def _validate_selections(request: JudgmentRequest,
                         entries: list[PoolEntry]) -> None:
    """试算强制候选的存在性与版本/哈希一致性预检（不满足 -> 422）。"""
    if not request.selected_candidates:
        return
    index = {(e.content_hash, e.version, e.rule.rule_id): e for e in entries}
    ids = {(e.content_hash, e.rule.rule_id): e for e in entries}
    for key, sel in request.selected_candidates.items():
        hit = None
        if sel.content_hash:
            hit = ids.get((sel.content_hash, sel.rule_id))
        else:
            hit = next(
                (e for e in entries
                 if e.rule.rule_id == sel.rule_id
                 and (sel.version is None or e.version == sel.version)),
                None,
            )
        if hit is None or (
            sel.version is not None and sel.version != hit.version
        ) or (
            sel.content_hash is not None and sel.content_hash != hit.content_hash
        ):
            raise HTTPException(
                422,
                f"selected_candidates[{key}] 指定的规则项 "
                f"rule_id={sel.rule_id} version={sel.version} "
                f"hash={sel.content_hash} 不在候选规则池中，无法对照",
            )


def assert_formal_acceptable(result: JudgmentResult, *, trial: bool) -> None:
    """正式接收守门：各时钟必须冻结到唯一匹配的规则项。

    无匹配 / 多同等候选 / 时段重叠致歧义 / 保存动作不足 / 结构失效，
    均不得作为正式判定固化（试算与补录预览不受此限制，补录在调用处单独处理）。
    """
    if trial:
        return
    blocked = [
        {
            "sample_id": c.sample_id,
            "item": c.item,
            "status": c.status.value,
            "match_status": c.match_status,
            "conclusive": c.conclusive,
            "candidates": [
                {"version": x.version, "rule_id": x.rule_id, "applies": x.applies}
                for x in c.candidates
            ],
        }
        for c in result.clocks if not c.conclusive
    ]
    if blocked:
        raise HTTPException(
            422,
            {
                "message": (
                    "存在无法唯一确定适用规则或不得形成合规结论的时钟，"
                    "正式接收被拒绝；可先 /trial 试算（支持 selected_candidates 对照）"
                ),
                "blocked_clocks": blocked,
            },
        )


def _package_id(trial: bool, sample_id: Optional[str]) -> str:
    raw = uuid.uuid4().hex
    if trial:
        return f"trial-{raw}"
    if sample_id:
        return f"pkg-s-{raw}"
    return f"pkg-{raw}"


def _evaluate(
    request: JudgmentRequest,
    *,
    trial: bool,
    version_no: int,
    sample_id: Optional[str] = None,
    changes_from: Optional[str] = None,
    changes: Optional[list[StatusChange]] = None,
    created_at=None,
    extra_sets: Optional[list[tuple[str, RuleSet]]] = None,
) -> JudgmentResult:
    entries, sets_by_hash = build_pool(request, extra_sets=extra_sets)
    _validate_selections(request, entries)
    return evaluate(
        request=request,
        pool=entries,
        rule_sets=sets_by_hash,
        package_id=_package_id(trial, sample_id),
        version_no=version_no,
        trial=trial,
        created_at=created_at or utc_now(),
        changes_from=changes_from,
        changes=changes,
        sample_id=sample_id,
    )


def _register_used_sets(request: JudgmentRequest) -> None:
    """正式接收前登记请求携带的规则集（库内冲突在此被 409 拦截）。"""
    if request.rule_set is not None:
        register_rule_set(request.rule_set, dry_run=False)


def run_judgment(
    request: JudgmentRequest,
    *,
    trial: bool,
    version_no: int = 1,
    sample_id: Optional[str] = None,
    changes_from: Optional[str] = None,
    changes: Optional[list[StatusChange]] = None,
) -> JudgmentResult:
    if not trial and request.selected_candidates:
        raise HTTPException(
            422, "正式接收不得携带 selected_candidates；强制候选只用于试算对照"
        )
    # 试算：携带集不写库，先算后守门（trial 不守门）
    result = _evaluate(
        request, trial=trial, version_no=version_no, sample_id=sample_id,
        changes_from=changes_from, changes=changes,
    )
    if not trial:
        assert_formal_acceptable(result, trial=False)
        _register_used_sets(request)
        # 登记后内容哈希不变（登记前 hash_rules 与库内一致），直接固化
        result = db.save_judgment(
            result, trial=False,
            idempotency_key=request.idempotency_key,
            request_json=canonical_json(request.model_dump(mode="json")),
        )
    return result


# ------------------------------------------------------------ 补录 ----

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


def _frozen_sets_for_package(pkg: dict) -> list[tuple[str, RuleSet]]:
    """取该判定包冻结的全部规则集内容（补录时不引入规则改选）。"""
    hashes = {c.get("matched_rule", {}).get("content_hash")
              for c in pkg.get("clocks", []) if c.get("matched_rule")}
    out: list[tuple[str, RuleSet]] = []
    for h in hashes:
        if not h:
            continue
        row = db.get_rule_set_by_hash(h)
        if row:
            out.append((h, RuleSet.model_validate(json.loads(row["payload_json"]))))
    return out


def supplement_judgment(
    sample_id: str, ev: SupplementEvent
) -> JudgmentResult:
    """在该样品最近一次判定基础上追加补录事件，生成新版本并列出状态变化。

    补录只复算事件与判定时刻，规则选择冻结在上一版本包内（连同其规则集一起
    纳入候选池），规则更新不改选、不改写限值。
    """
    latest_pkg = db.latest_sample_package(sample_id)
    if not latest_pkg:
        raise HTTPException(404, f"样品 {sample_id} 没有可补录的历史判定")
    stored_req = db.get_stored_request(latest_pkg["package_id"])
    if not stored_req:
        raise HTTPException(
            409, f"样品 {sample_id} 的历史判定缺少请求快照，无法补录"
        )

    request = JudgmentRequest.model_validate(stored_req)
    request.selected_candidates = None  # 补录是正式新版本，不允许强制候选
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

    frozen_sets = _frozen_sets_for_package(latest_pkg)
    # 补录候选池只含上一版本冻结的规则集（保证规则不改选）
    request.rule_set = None
    request.rule_version = None

    new_version_no = db.next_sample_version(sample_id)
    created = utc_now()
    preview = _evaluate(
        request, trial=True, version_no=new_version_no,
        sample_id=sample_id, changes_from=latest_pkg["package_id"],
        created_at=created, extra_sets=frozen_sets,
    )
    raw_changes = diff_judgments(
        latest_pkg, preview.model_dump(mode="json")
    )
    raw_changes = [c for c in raw_changes if c.get("sample_id") == sample_id]
    changes = _to_status_changes(raw_changes, sample_id)

    result = _evaluate(
        request, trial=False, version_no=new_version_no,
        sample_id=sample_id, changes_from=latest_pkg["package_id"],
        changes=changes, created_at=preview.created_at,
        extra_sets=frozen_sets,
    )
    # 补录允许时钟因新事件进入临界/超时，但规则必须仍唯一冻结；
    # 若冻结规则在新条件下不再唯一（理论上不应发生），拒绝固化
    assert_formal_acceptable(result, trial=False)
    result = db.save_judgment(
        result, trial=False, idempotency_key=None,
        request_json=canonical_json(request.model_dump(mode="json")),
    )
    return result


# -------------------------------------------------- 规则登记与预检 ----

def registration_conflicts(rule_set: RuleSet) -> list[dict]:
    """预检新规则集与已登记规则的适用范围冲突（仅预检，不写库）。

    规则集内部冲突已由模型校验（422）拦截；这里检查跨规则集：同一项目、生效
    区间重叠且四个适用维度可同时命中即冲突——即使仅 rule_id 不同（或限值也
    相同），也会在候选池中形成两个独立候选，必须拦截。整套规则集逐字节相同
    的重复登记按内容哈希幂等去重（在上面的哈希比较处跳过）。
    """
    new_hashes = {hash_rules(rule_set)}
    conflicts: list[dict] = []
    for _h, existing in db.all_rule_sets_payload():
        if hash_rules(existing) in new_hashes:
            continue  # 完全相同内容：幂等，无冲突
        for nr in rule_set.items:
            for or_ in existing.items:
                c = scope_conflict(nr, or_)
                if c is not None:
                    conflicts.append({
                        "item": nr.item,
                        "new_rule_id": nr.rule_id,
                        "new_version": rule_set.version,
                        "existing_rule_id": or_.rule_id,
                        "existing_version": existing.version,
                        "overlap": c["dimensions"],
                    })
    return conflicts


def register_rule_set(rule_set: RuleSet, *, dry_run: bool = False) -> dict:
    """登记规则集。dry_run=True 时只返回冲突预检结果，不写库。"""
    conflicts = registration_conflicts(rule_set)
    if dry_run:
        return {
            "version": rule_set.version,
            "content_hash": hash_rules(rule_set),
            "dry_run": True,
            "conflict_count": len(conflicts),
            "conflicts": conflicts,
            "would_register": not conflicts,
        }
    if conflicts:
        raise HTTPException(
            409,
            {
                "message": (
                    f"规则版本 {rule_set.version} 与已登记规则存在适用范围冲突，"
                    "同一项目在重叠生效区间内可能命中多条规则"
                ),
                "conflicts": conflicts,
            },
        )
    h, created = db.register_rule_set(rule_set)
    return {
        "version": rule_set.version,
        "content_hash": h,
        "created": created,
        "conflict_count": 0,
        "conflicts": [],
        "note": "内容已存在" if not created else "已登记（旧版本判定永不受影响）",
    }


# -------------------------------------------------------- 影响预览 ----

def impact_preview(rule_set: RuleSet) -> dict:
    """用已保存的正式请求在“加入候选池的新规则集”下试算，列出会改选规则的
    样品—项目。全程只读：不登记规则、不写判定、不改写任何旧判定。"""
    records = db.all_formal_requests()
    new_hash = hash_rules(rule_set)
    changes: list[dict] = []
    scanned = 0

    # 同一批次包可能对应多条 sample_versions；按 package 去重重放
    seen_packages: set[str] = set()
    for rec in records:
        pkg_id = rec["package_id"]
        if pkg_id in seen_packages:
            continue
        seen_packages.add(pkg_id)
        stored = json.loads(rec["request_json"])
        request = JudgmentRequest.model_validate(stored)
        request.selected_candidates = None
        # 新规则集作为候选池的一部分（不写库）
        entries, sets_by_hash = build_pool(request)
        entries = list(entries)
        existing_hashes = {e.content_hash for e in entries}
        for r in rule_set.items:
            if new_hash not in existing_hashes:
                entries.append(PoolEntry(
                    version=rule_set.version, content_hash=new_hash,
                    rule=r, adhoc=True,
                ))
        sets_by_hash = dict(sets_by_hash)
        sets_by_hash[new_hash] = rule_set

        old_clocks = {
            (c["sample_id"], c["item"]): c
            for c in json.loads(rec["package_json"]).get("clocks", [])
        }
        preview = evaluate(
            request=request, pool=entries, rule_sets=sets_by_hash,
            package_id=f"trial-impact-{pkg_id}", version_no=0, trial=True,
            created_at=utc_now(),
        )
        scanned += 1
        for c in preview.clocks:
            old = old_clocks.get((c.sample_id, c.item))
            if old is None:
                continue
            old_mr = old.get("matched_rule")
            old_id = None if not old_mr else (old_mr["content_hash"], old_mr["rule_id"])
            new_id = None if not c.matched_rule else (
                c.matched_rule.content_hash, c.matched_rule.rule_id)
            if old_id != new_id:
                changes.append({
                    "package_id": pkg_id,
                    "scope": rec["scope"],
                    "sample_id": c.sample_id,
                    "item": c.item,
                    "before": None if not old_mr else {
                        "version": old_mr["version"],
                        "rule_id": old_mr["rule_id"],
                        "content_hash": old_mr["content_hash"],
                    },
                    "after": None if not c.matched_rule else {
                        "version": c.matched_rule.version,
                        "rule_id": c.matched_rule.rule_id,
                        "content_hash": c.matched_rule.content_hash,
                    },
                    "after_match_status": c.match_status,
                    "after_clock_status": c.status.value,
                    "targets_new_rule": (
                        c.matched_rule is not None
                        and c.matched_rule.content_hash == new_hash
                    ),
                })

    return {
        "candidate_version": rule_set.version,
        "candidate_content_hash": new_hash,
        "stored_packages_scanned": scanned,
        "reselected_count": len(changes),
        "reselected": changes,
        "note": "仅试算影响：未登记规则，也未改写任何既有判定",
    }
