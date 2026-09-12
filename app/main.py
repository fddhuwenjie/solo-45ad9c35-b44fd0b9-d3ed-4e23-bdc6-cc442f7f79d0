"""FastAPI 入口：环境检测样品时限判定 + 期限驱动的实验排程。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import db, schedule_service, service
from .models import (
    JudgmentRequest,
    JudgmentResult,
    ResourceSet,
    RuleSet,
    ScheduleRequest,
    ScheduleResult,
    SupplementEvent,
)
from .versioning import diff_judgments, diff_rule_sets, diff_schedules, hash_rules


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.get_conn()
    yield


app = FastAPI(
    title="环境检测样品时限判定 API",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "为每个样品—项目建立独立时钟：连续样基准、合样最早组成、分样共享历史、"
        "前处理不清零分析期限；规则内容哈希版本化，旧判定不可改写。"
        "期限驱动的实验排程：从已冻结判定包读取待办样品—项目，叠加已签发占用，"
        "按截止时刻判断能否纳入计划；签发冻结占用，补录/新判定只重排草稿。"
    ),
)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------- 判定 ----

@app.post("/api/v1/judgments/trial", response_model=JudgmentResult,
          tags=["judgments"])
def trial(req: JudgmentRequest) -> JudgmentResult:
    """试算：完整推导与违规检查，但不写库（package_id 以 trial- 开头）。"""
    return service.run_judgment(req, trial=True, version_no=0)


@app.post("/api/v1/judgments", response_model=JudgmentResult,
          tags=["judgments"], status_code=201)
def receive(req: JudgmentRequest) -> JudgmentResult:
    """正式接收：固化规则（如需）与判定包。同 idempotency_key 重复提交幂等。"""
    return service.run_judgment(req, trial=False, version_no=1)


@app.post("/api/v1/samples/{sample_id}/supplement",
          response_model=JudgmentResult, tags=["judgments"], status_code=201)
def supplement(sample_id: str, ev: SupplementEvent) -> JudgmentResult:
    """补录事件：基于该样品最近判定追加生成新版本，changes 列出状态变化。"""
    return service.supplement_judgment(sample_id, ev)


@app.get("/api/v1/judgments/{package_id}", tags=["judgments"])
def get_judgment(package_id: str):
    """取指定版本的完整 JSON 判定包。"""
    pkg = db.get_package(package_id)
    if not pkg:
        raise HTTPException(404, f"判定包不存在: {package_id}")
    return JSONResponse(pkg)


@app.get("/api/v1/samples/{sample_id}/latest", tags=["judgments"])
def latest_for_sample(sample_id: str):
    """样品当前最新判定包。"""
    pkg = db.latest_sample_package(sample_id)
    if not pkg:
        raise HTTPException(404, f"样品无判定记录: {sample_id}")
    return JSONResponse(pkg)


@app.get("/api/v1/samples/{sample_id}/versions", tags=["judgments"])
def versions_for_sample(sample_id: str) -> dict:
    rows = db.list_sample_versions(sample_id)
    if not rows:
        raise HTTPException(404, f"样品无判定记录: {sample_id}")
    return {"sample_id": sample_id, "count": len(rows), "versions": rows}


@app.get("/api/v1/judgments/{package_id}/diff/{other_package_id}",
         tags=["judgments"])
def diff_packages(package_id: str, other_package_id: str) -> dict:
    """比较两个判定包（任意版本），返回逐时钟字段变化与违规增减。

    若任一侧为样品级补录包，则比较视图限定在该样品（包内其它样品不参与），
    从而 A 的补录不会在 B 的比较基线中产生变化。
    """
    import copy

    a = db.get_package(package_id)
    b = db.get_package(other_package_id)
    if not a or not b:
        raise HTTPException(
            404,
            f"判定包缺失: "
            f"{package_id if not a else other_package_id}",
        )

    scopes = {x.get("sample_id") for x in (a, b) if x.get("sample_id")}
    scope_sample = next(iter(scopes)) if len(scopes) == 1 else None

    def scoped(pkg: dict) -> dict:
        if scope_sample is None:
            return pkg
        p = copy.deepcopy(pkg)
        p["clocks"] = [c for c in p.get("clocks", [])
                       if c["sample_id"] == scope_sample]
        p["violations"] = [v for v in p.get("violations", [])
                           if v["sample_id"] == scope_sample]
        return p

    a_s, b_s = scoped(a), scoped(b)
    changes = diff_judgments(a_s, b_s)
    return {
        "from": {"package_id": package_id, "version_no": a["version_no"],
                 "sample_id": a.get("sample_id"), "rule": a["rule"]},
        "to": {"package_id": other_package_id, "version_no": b["version_no"],
               "sample_id": b.get("sample_id"), "rule": b["rule"]},
        "scope_sample": scope_sample,
        "rule_changed": a["rule"]["content_hash"] != b["rule"]["content_hash"],
        "change_count": len(changes),
        "changes": changes,
    }


# ---------------------------------------------------------------- 规则 ----

@app.get("/api/v1/rules", tags=["rules"])
def list_rules() -> dict:
    rows = db.list_rule_sets()
    return {"count": len(rows), "rules": rows}


@app.get("/api/v1/rules/{version}", tags=["rules"])
def get_rule(version: str):
    found = db.find_rule_by_version(version)
    if not found:
        raise HTTPException(404, f"规则版本不存在: {version}")
    h, rules = found
    return {"content_hash": h, "rule_set": rules.model_dump(mode="json")}


@app.post("/api/v1/rules", tags=["rules"], status_code=201)
def register_rules(rule_set: RuleSet, dry_run: bool = False) -> dict:
    """登记规则版本。

    * ``dry_run=true``：只预检适用范围冲突（不写库，返回 200 与冲突清单）；
    * 与已登记规则在同一项目的重叠生效区间内可同时命中 -> 409 并列明细。
    """
    result = service.register_rule_set(rule_set, dry_run=dry_run)
    return result


@app.post("/api/v1/rules/impact-preview", tags=["rules"])
def rules_impact_preview(rule_set: RuleSet) -> dict:
    """影响预览：把携带的规则集并入候选池，用全部已保存的正式请求试算，
    列出会改选（或失去）规则的样品—项目；不登记规则、不改写旧判定。"""
    return service.impact_preview(rule_set)


@app.post("/api/v1/rules/diff", tags=["rules"])
def diff_rules(body: dict) -> dict:
    """比较请求中携带的两个规则版本（old_version/new_version 或两套 rule_set）。"""
    def _load(spec):
        if isinstance(spec, dict):
            return RuleSet.model_validate(spec)
        if not spec:
            raise HTTPException(
                422, "需提供 old/new（规则集）或 old_version/new_version（已登记版本）"
            )
        found = db.find_rule_by_version(spec)
        if not found:
            raise HTTPException(404, f"规则版本不存在: {spec}")
        return found[1]

    if "old_version" in body or "new_version" in body:
        old = _load(body.get("old_version"))
        new = _load(body.get("new_version"))
    else:
        old = _load(body.get("old"))
        new = _load(body.get("new"))
    changes = diff_rule_sets(old, new)
    return {
        "old": {"version": old.version, "content_hash": hash_rules(old)},
        "new": {"version": new.version, "content_hash": hash_rules(new)},
        "change_count": len(changes),
        "changes": changes,
    }


# ---------------------------------------------------------------- 资源 ----

@app.post("/api/v1/resources", tags=["resources"], status_code=201)
def register_resources(resource_set: ResourceSet, dry_run: bool = False) -> dict:
    """登记资源配置版本（前处理工位/仪器、可用时段、停机窗、方法切换时间、
    单批容量、任务时长、适用方法）。

    * ``dry_run=true``：只校验并返回内容哈希，不写库；
    * 同一版本号绑定不同内容 -> 409；完全一致 -> 幂等。
    """
    return schedule_service.register_resource_set(resource_set, dry_run=dry_run)


@app.get("/api/v1/resources", tags=["resources"])
def list_resources() -> dict:
    rows = db.list_resource_sets()
    return {"count": len(rows), "resources": rows}


@app.get("/api/v1/resources/{version}", tags=["resources"])
def get_resource(version: str):
    found = db.find_resource_by_version(version)
    if not found:
        raise HTTPException(404, f"资源版本不存在: {version}")
    h, rs = found
    return {"content_hash": h, "resource_set": rs.model_dump(mode="json")}


# ---------------------------------------------------------------- 排程 ----

@app.post("/api/v1/schedules/trial", response_model=ScheduleResult,
          tags=["scheduling"])
def schedule_trial(req: ScheduleRequest) -> ScheduleResult:
    """试排：从已冻结判定包读取待处理样品—项目，叠加已签发占用，不写库。

    响应列出每项任务的工位、批次、起止时刻、余量与规则哈希；无法准时纳入
    计划的任务进入 conflicts，并指出最早冲突的资源、区间与受影响时钟。
    相同输入得到稳定结果（content_hash 可校验）。
    """
    return schedule_service.run_schedule(req, trial=True)


@app.post("/api/v1/schedules", response_model=ScheduleResult,
          tags=["scheduling"], status_code=201)
def schedule_issue(req: ScheduleRequest) -> ScheduleResult:
    """签发：与试排同一计算，结果固化为新版本，其占用对后续排程冻结。

    补录事件或新判定只重排未冻结的草稿部分；同 idempotency_key 幂等。
    """
    return schedule_service.run_schedule(req, trial=False)


@app.get("/api/v1/schedules", tags=["scheduling"])
def list_schedules() -> dict:
    rows = db.list_schedules()
    return {"count": len(rows), "schedules": rows}


@app.get("/api/v1/schedules/{schedule_id}", tags=["scheduling"])
def get_schedule(schedule_id: str):
    """取指定签发版本的完整 JSON 排程。"""
    payload = db.get_schedule(schedule_id)
    if not payload:
        raise HTTPException(404, f"排程不存在: {schedule_id}")
    return JSONResponse(payload)


@app.get("/api/v1/schedules/{schedule_id}/diff/{other_schedule_id}",
         tags=["scheduling"])
def diff_schedule_versions(schedule_id: str, other_schedule_id: str) -> dict:
    """比较两个排程版本：任务增删与工位/批次/起止/余量变化、冲突增减。"""
    a = db.get_schedule(schedule_id)
    b = db.get_schedule(other_schedule_id)
    if not a or not b:
        raise HTTPException(
            404,
            f"排程缺失: {schedule_id if not a else other_schedule_id}",
        )
    changes = diff_schedules(a, b)
    return {
        "from": {"schedule_id": schedule_id, "version_no": a["version_no"],
                 "content_hash": a["content_hash"], "status": a["status"]},
        "to": {"schedule_id": other_schedule_id, "version_no": b["version_no"],
               "content_hash": b["content_hash"], "status": b["status"]},
        "change_count": len(changes),
        "changes": changes,
    }


@app.get("/api/v1/schedules/{schedule_id}/workorder", tags=["scheduling"])
def schedule_workorder(schedule_id: str) -> dict:
    """JSON 工作单：按资源分组、批次按开始时刻排序的可执行视图。"""
    payload = db.get_schedule(schedule_id)
    if not payload:
        raise HTTPException(404, f"排程不存在: {schedule_id}")
    return schedule_service.work_order(payload)
