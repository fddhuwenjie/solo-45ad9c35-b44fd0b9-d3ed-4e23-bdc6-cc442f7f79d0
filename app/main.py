"""FastAPI 入口：环境检测样品时限判定。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import db, service
from .models import (
    JudgmentRequest,
    JudgmentResult,
    RuleSet,
    SupplementEvent,
)
from .versioning import diff_judgments, diff_rule_sets, hash_rules


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
def register_rules(rule_set: RuleSet) -> dict:
    h, created = db.register_rule_set(rule_set)
    return {
        "version": rule_set.version,
        "content_hash": h,
        "created": created,
        "note": "内容已存在" if not created else "已登记（旧版本判定永不受影响）",
    }


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
