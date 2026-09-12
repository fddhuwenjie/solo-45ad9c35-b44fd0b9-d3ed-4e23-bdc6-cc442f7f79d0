"""排程编排：资源配置解析 -> 已冻结判定包任务提取 -> 引擎试排 -> 签发持久化。

* 试排（trial）：从已冻结判定包读取待处理的样品—项目，叠加已签发占用，
  完整给出排程结果但不写库（schedule_id 以 trial- 开头，由内容哈希派生，
  相同输入得到稳定结果）。
* 签发（issue）：同一计算流程，结果固化为新版本；其全部占用对后续试排/
  签发冻结。补录事件或新判定只让未冻结的草稿部分重排。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

from . import db
from .models import (
    ResourceRef,
    ResourceSet,
    ScheduleConflict,
    ScheduleRequest,
    ScheduleResult,
    utc_now,
)
from .scheduler import Occupation, SchedTask, compute_schedule
from .versioning import canonical_json, hash_resources

_PHASE_ORDER = {"pretreatment": 0, "analysis": 1}
_FAR = datetime.max.replace(tzinfo=timezone.utc)


# --------------------------------------------------------- 输入解析 ----

def _resolve_resources(req: ScheduleRequest) -> tuple[str, ResourceSet, bool]:
    """返回 (content_hash, resource_set, carried)。carried=请求携带。"""
    if req.resource_set is not None:
        return hash_resources(req.resource_set), req.resource_set, True
    if req.resource_version:
        found = db.find_resource_by_version(req.resource_version)
        if not found:
            raise HTTPException(404, f"资源版本未登记: {req.resource_version}")
        return found[0], found[1], False
    latest = db.latest_resource_set()
    if latest is None:
        raise HTTPException(
            422,
            "尚未登记资源配置：请先 POST /api/v1/resources 登记，"
            "或在请求中携带 resource_set / resource_version",
        )
    return latest[0], latest[1], False


def _resolve_packages(req: ScheduleRequest) -> list[dict]:
    """显式 package_ids（必须已冻结）或缺省取全部样品的最新正式判定。"""
    if req.package_ids is not None:
        out = []
        for pid in req.package_ids:
            if pid.startswith("trial-"):
                raise HTTPException(
                    422, f"判定包 {pid} 是试算包（未冻结），不能用于排程"
                )
            pkg = db.get_package(pid)
            if not pkg:
                raise HTTPException(404, f"判定包不存在: {pid}")
            if pkg.get("trial"):
                raise HTTPException(
                    422, f"判定包 {pid} 是试算包（未冻结），不能用于排程"
                )
            out.append(pkg)
        return out
    by_id: dict[str, dict] = {}
    for sid in db.all_sample_ids():
        pkg = db.latest_sample_package(sid)
        if pkg:
            by_id[pkg["package_id"]] = pkg
    return list(by_id.values())


def extract_tasks(
    packages: list[dict], frozen_keys: set[tuple[str, str, str]]
) -> tuple[list[SchedTask], list[dict]]:
    """从已冻结判定包提取待排任务。

    同一（样品, 项目）时钟出现在多个包中时，以创建时间较晚的包为准。
    未形成合规结论（无唯一冻结规则）的时钟不产出任务，列入 skipped。
    """
    clocks: dict[tuple[str, str], dict] = {}
    for pkg in sorted(packages,
                      key=lambda p: (p["created_at"], p["package_id"])):
        for c in pkg.get("clocks", []):
            clocks[(c["sample_id"], c["item"])] = c

    tasks: list[SchedTask] = []
    skipped: list[dict] = []
    for sid, item in sorted(clocks):
        c = clocks[(sid, item)]
        pending = [
            p for p in c.get("phases", [])
            if p.get("status") in ("pending", "critical", "overdue")
            and p.get("deadline")
        ]
        if not pending:
            continue
        if not c.get("conclusive") or not c.get("matched_rule"):
            skipped.append({
                "sample_id": sid,
                "item": item,
                "status": c.get("status"),
                "match_status": c.get("match_status"),
                "reason": "时钟未形成合规结论（无唯一冻结规则），不能纳入排程",
            })
            continue
        method = None
        match_ctx = c.get("match_context") or {}
        for fs in match_ctx.get("field_sources", []):
            if fs.get("field") == "method":
                method = fs.get("value")
        rule = c["matched_rule"]
        for p in pending:
            phase = p["phase"]
            if (sid, item, phase) in frozen_keys:
                continue  # 已签发冻结：不占草稿
            tasks.append(SchedTask(
                sample_id=sid,
                item=item,
                phase=phase,
                method=method,
                deadline=datetime.fromisoformat(p["deadline"]),
                rule={
                    "version": rule["version"],
                    "content_hash": rule["content_hash"],
                    "rule_id": rule["rule_id"],
                    "item": rule["item"],
                },
            ))
    return tasks, skipped


def _frozen_overlay() -> tuple[
    Optional[dict], list[Occupation], list[dict]
]:
    """最近签发版本的占用：批次（冻结约束）+ 任务视图（随响应返回）。"""
    issued = db.latest_issued_schedule()
    if issued is None:
        return None, [], []
    frozen_from = {
        "schedule_id": issued["schedule_id"],
        "version_no": issued["version_no"],
    }
    batches = [
        Occupation(
            batch_id=b["batch_id"],
            resource_id=b["resource_id"],
            method=b["method"],
            start=datetime.fromisoformat(b["start"]),
            end=datetime.fromisoformat(b["end"]),
            frozen=True,
            task_keys=list(b["task_keys"]),
        )
        for b in issued.get("batches", [])
    ]
    task_views = [{**t, "frozen": True} for t in issued.get("tasks", [])]
    return frozen_from, batches, task_views


# --------------------------------------------------------- 主流程 ----

def _conflict_key(c: dict) -> tuple:
    interval = c.get("interval")
    start = interval["start"] if interval else None
    return (
        start is None,
        start if start is not None else _FAR,
        c.get("resource_id") or "",
        c["sample_id"],
        c["item"],
        c["phase"],
    )


def run_schedule(req: ScheduleRequest, *, trial: bool) -> ScheduleResult:
    rhash, rset, carried = _resolve_resources(req)
    packages = _resolve_packages(req)
    frozen_from, frozen_batches, frozen_task_views = _frozen_overlay()
    frozen_keys = {
        (t["sample_id"], t["item"], t["phase"]) for t in frozen_task_views
    }
    tasks, skipped = extract_tasks(packages, frozen_keys)

    computed = compute_schedule(
        tasks=tasks,
        resources=rset.resources,
        frozen_batches=frozen_batches,
        frozen_tasks=frozen_task_views,
        schedule_time=req.schedule_time,
    )

    conflicts = sorted(computed["conflicts"], key=_conflict_key)
    all_tasks = list(frozen_task_views) + computed["task_views"]
    all_tasks.sort(key=lambda t: (
        t["sample_id"], t["item"], _PHASE_ORDER[t["phase"]]))
    batch_views = computed["batch_views"]
    package_ids = sorted(p["package_id"] for p in packages)
    summary = {
        "packages": len(packages),
        "pending_tasks": len(tasks),
        "scheduled": len(computed["task_views"]),
        "unscheduled": len(conflicts),
        "on_time": len(computed["task_views"]),
        "total_tasks": len(all_tasks),
        "frozen_tasks": len(frozen_task_views),
        "new_batches": sum(1 for b in batch_views if not b["frozen"]),
        "total_batches": len(batch_views),
        "method_switches": computed["method_switches"],
        "skipped_clocks": len(skipped),
        "dropped_frozen_batches": computed["dropped_frozen"],
    }

    result = ScheduleResult(
        schedule_id="",
        version_no=0 if trial else db.next_schedule_version(),
        trial=trial,
        request_id=req.request_id,
        schedule_time=req.schedule_time,
        created_at=utc_now(),
        resource=ResourceRef(
            version=rset.version,
            content_hash=rhash,
            name=rset.name,
            resource_count=len(rset.resources),
        ),
        content_hash="",
        status="feasible" if not conflicts else "infeasible",
        frozen_from=frozen_from,
        summary=summary,
        packages=package_ids,
        tasks=all_tasks,
        batches=batch_views,
        conflicts=conflicts,
        earliest_conflict=conflicts[0] if conflicts else None,
        skipped_clocks=skipped,
    )
    # 内容哈希：相同输入得到稳定结果（不含 id/版本号/生成时刻）
    dump = result.model_dump(mode="json")
    result.content_hash = hashlib.sha256(
        canonical_json({k: dump[k] for k in (
            "resource", "schedule_time", "packages", "frozen_from",
            "tasks", "batches", "conflicts", "status",
        )}).encode("utf-8")
    ).hexdigest()
    result.schedule_id = (
        f"trial-{result.content_hash[:24]}"
        if trial else f"sch-{uuid.uuid4().hex}"
    )

    if not trial:
        if carried:
            # 签发携带的资源集先登记（版本标签冲突在此被 409 拦截）
            register_resource_set(rset, dry_run=False)
        result = db.save_schedule(
            result,
            idempotency_key=req.idempotency_key,
            request_json=canonical_json(req.model_dump(mode="json")),
        )
    return result


# --------------------------------------------------------- 资源登记 ----

def register_resource_set(resource_set: ResourceSet, *, dry_run: bool = False) -> dict:
    h = hash_resources(resource_set)
    if dry_run:
        return {
            "version": resource_set.version,
            "content_hash": h,
            "dry_run": True,
            "would_register": True,
        }
    h, created = db.register_resource_set(resource_set)
    return {
        "version": resource_set.version,
        "content_hash": h,
        "created": created,
        "note": "内容已存在" if not created else "已登记（已签发排程永不受影响）",
    }


# --------------------------------------------------------- 工作单 ----

def work_order(payload: dict) -> dict:
    """JSON 工作单：按资源分组、批次按开始时刻排序，供实验室执行。"""
    tasks_by_key = {
        f"{t['sample_id']}/{t['item']}/{t['phase']}": t
        for t in payload.get("tasks", [])
    }
    by_resource: dict[str, dict] = {}
    for b in payload.get("batches", []):
        entry = by_resource.setdefault(b["resource_id"], {
            "resource_id": b["resource_id"],
            "kind": b["kind"],
            "batches": [],
        })
        entry["batches"].append(b)

    resources = []
    for rid in sorted(by_resource):
        entry = by_resource[rid]
        batches = []
        for b in sorted(entry["batches"],
                        key=lambda x: (x["start"], x["batch_id"])):
            batch_tasks = []
            for key in b["task_keys"]:
                t = tasks_by_key.get(key)
                if t is None:
                    continue
                batch_tasks.append({
                    "sample_id": t["sample_id"],
                    "item": t["item"],
                    "phase": t["phase"],
                    "deadline": t["deadline"],
                    "slack_minutes": t["slack_minutes"],
                    "on_time": t["on_time"],
                    "rule_id": t["rule"]["rule_id"],
                    "rule_hash": t["rule"]["content_hash"],
                })
            batch_tasks.sort(key=lambda t: (
                t["sample_id"], t["item"], _PHASE_ORDER[t["phase"]]))
            batches.append({
                "batch_id": b["batch_id"],
                "method": b["method"],
                "start": b["start"],
                "end": b["end"],
                "capacity": b["capacity"],
                "switch_before_minutes": b["switch_before_minutes"],
                "frozen": b["frozen"],
                "tasks": batch_tasks,
            })
        resources.append({
            "resource_id": entry["resource_id"],
            "kind": entry["kind"],
            "batches": batches,
        })

    return {
        "schedule_id": payload["schedule_id"],
        "version_no": payload["version_no"],
        "status": payload["status"],
        "schedule_time": payload["schedule_time"],
        "generated_at": payload["created_at"],
        "resource": payload["resource"],
        "content_hash": payload["content_hash"],
        "summary": payload.get("summary", {}),
        "resources": resources,
        "unscheduled": payload.get("conflicts", []),
    }
