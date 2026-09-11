"""Pydantic v2 请求/响应模型。

时间一律使用带时区的 ISO-8601；进入系统时统一归一化为 UTC（存原始字符串于
判定包中以便复现）。所有请求字段使用 snake_case。
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime) -> datetime:
    """把任意带时区时间归一化为 UTC；拒绝 naive datetime。"""
    if dt.tzinfo is None:
        raise ValueError("datetime 必须带时区信息（ISO-8601 偏移，如 +08:00 / Z）")
    return dt.astimezone(timezone.utc)


class SampleKind(str, Enum):
    GRAB = "grab"            # 瞬时样
    CONTINUOUS = "continuous"  # 连续样（有采样区间）
    ALIQUOT = "aliquot"      # 分样子样
    COMPOSITE = "composite"  # 合样


class ContinuousBasis(str, Enum):
    START = "start"
    END = "end"


# ---------------------------------------------------------------- 请求侧 ----

class PreservationAction(BaseModel):
    name: str = Field(..., description="动作名，如 add_acid / cool_4c / dark")
    time: datetime
    note: Optional[str] = None

    @field_validator("time")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return as_utc(v)


class TemperaturePoint(BaseModel):
    time: datetime
    temp_c: float = Field(..., alias="temp_c")
    note: Optional[str] = None

    model_config = {"populate_by_name": True}

    @field_validator("time")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return as_utc(v)


class PretreatmentEvent(BaseModel):
    """前处理（消解/萃取/蒸馏等）；只结束“预处理”阶段，不影响分析期限。"""
    type: str = Field(..., description="如 digestion / extraction / distillation")
    time: datetime
    items: list[str] = Field(
        default_factory=list,
        description="作用到的项目；空列表表示对该样品全部项目生效",
    )

    @field_validator("time")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return as_utc(v)


class AnalysisEvent(BaseModel):
    item: str
    time: datetime
    note: Optional[str] = None

    @field_validator("time")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return as_utc(v)


class ItemRule(BaseModel):
    item: str
    item_name: Optional[str] = None
    min_temp_c: float = -1_000_000.0
    max_temp_c: float = 1_000_000.0
    pretreatment_minutes: Optional[float] = Field(
        None, description="预处理期限（分钟），从基准起算；为空表示无此前置阶段"
    )
    analysis_minutes: float
    required_preservation: list[str] = Field(default_factory=list)
    continuous_basis: ContinuousBasis = ContinuousBasis.END
    note: Optional[str] = None


class RuleSet(BaseModel):
    """可版本化的时限规则集合。version 是调用方给的可读版本号；
    服务端另按规则内容计算 content_hash 作为不可变身份。"""
    version: str = Field(..., description="可读版本号，如 2026.1")
    name: str = "environmental-deadline-rules"
    items: list[ItemRule]
    note: Optional[str] = None

    @model_validator(mode="after")
    def _unique_items(self) -> "RuleSet":
        names = [r.item for r in self.items]
        if len(names) != len(set(names)):
            raise ValueError("规则集中 item 不得重复")
        if not self.items:
            raise ValueError("规则集至少包含一条项目规则")
        return self


class Sample(BaseModel):
    id: str
    kind: SampleKind
    items: list[str] = Field(..., description="该样品（瓶）要测的项目")
    container: Optional[str] = Field(None, description="容器，如 glass_amber / pe_bottle")
    sampling_start: datetime
    sampling_end: datetime
    parent_ids: list[str] = Field(
        default_factory=list,
        description="来源：分样为母体；合样为全部组成样；瞬时/连续样为空",
    )
    merged_at: Optional[datetime] = Field(
        None, description="合样时刻（仅 composite）：此前历史共享，此后独立"
    )
    preservation: list[PreservationAction] = Field(default_factory=list)
    temperature: list[TemperaturePoint] = Field(default_factory=list)
    pretreatments: list[PretreatmentEvent] = Field(default_factory=list)
    analyses: list[AnalysisEvent] = Field(default_factory=list)
    custody_transfers: list[datetime] = Field(
        default_factory=list, description="交接时间序列"
    )

    @field_validator("sampling_start", "sampling_end", "merged_at")
    @classmethod
    def _tz(cls, v: Optional[datetime]) -> Optional[datetime]:
        return as_utc(v) if v is not None else None

    @field_validator("custody_transfers")
    @classmethod
    def _tz_list(cls, v: list[datetime]) -> list[datetime]:
        return [as_utc(x) for x in v]

    @model_validator(mode="after")
    def _shape(self) -> "Sample":
        if self.sampling_end < self.sampling_start:
            # 时间倒置留给引擎出“完整推导”，不在 422 阶段拦截
            pass
        if self.kind == SampleKind.COMPOSITE and not self.parent_ids:
            raise ValueError("合样必须提供 parent_ids")
        if self.kind == SampleKind.ALIQUOT and not self.parent_ids:
            raise ValueError("分样必须提供 parent_ids（母体）")
        if not self.items:
            raise ValueError("样品至少包含一个分析项目")
        return self


class JudgmentRequest(BaseModel):
    request_id: Optional[str] = Field(None, description="调用方幂等/追踪号")
    idempotency_key: Optional[str] = Field(
        None, description="正式接收的幂等键；相同键重复提交返回同一判定包"
    )
    eval_time: datetime = Field(
        ..., description="判定时刻（剩余分钟按此刻计算），带时区"
    )
    critical_within_minutes: float = Field(
        60.0,
        description="剩余时间 <= 该阈值且未超时则标记 critical（默认 60 分钟）",
        ge=0,
    )
    rule_version: Optional[str] = Field(
        None, description="引用已登记的规则版本；与 rule_set 二选一"
    )
    rule_set: Optional[RuleSet] = Field(
        None, description="随请求携带的规则集（试算或登记新版本用）"
    )
    samples: list[Sample]

    @field_validator("eval_time")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return as_utc(v)

    @model_validator(mode="after")
    def _rules_and_refs(self) -> "JudgmentRequest":
        if self.rule_set is None and not self.rule_version:
            raise ValueError("必须提供 rule_set 或 rule_version 之一")
        if self.rule_set is not None and self.rule_version:
            if self.rule_set.version != self.rule_version:
                raise ValueError("rule_set.version 与 rule_version 不一致")
        ids = [s.id for s in self.samples]
        if len(ids) != len(set(ids)):
            raise ValueError("样品 id 不得重复")
        idset = set(ids)
        for s in self.samples:
            for p in s.parent_ids:
                if p not in idset:
                    # 结构缺链在引擎里产出 source_break 推导（不 422），
                    # 这样响应里能保留完整上下文；但这里仍允许通过。
                    pass
        known = {r.item for r in self.rule_set.items} if self.rule_set else None
        if known is not None:
            for s in self.samples:
                missing = [i for i in s.items if i not in known]
                if missing:
                    raise ValueError(f"样品 {s.id} 的项目在规则集中缺失: {missing}")
        return self


class SupplementEvent(BaseModel):
    """补录事件（挂在指定样品上），生成新判定版本。"""
    eval_time: datetime
    critical_within_minutes: Optional[float] = None
    preservation: list[PreservationAction] = Field(default_factory=list)
    temperature: list[TemperaturePoint] = Field(default_factory=list)
    pretreatments: list[PretreatmentEvent] = Field(default_factory=list)
    analyses: list[AnalysisEvent] = Field(default_factory=list)
    custody_transfers: list[datetime] = Field(default_factory=list)
    note: Optional[str] = None

    @field_validator("eval_time")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return as_utc(v)

    @field_validator("custody_transfers")
    @classmethod
    def _tz_list(cls, v: list[datetime]) -> list[datetime]:
        return [as_utc(x) for x in v]


# ---------------------------------------------------------------- 响应侧 ----

class PhaseStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    CRITICAL = "critical"
    OVERDUE = "overdue"
    INVALID = "invalid"


class ClockStatus(str, Enum):
    OK = "ok"
    CRITICAL = "critical"
    OVERDUE = "overdue"
    COMPLETED = "completed"
    INVALID = "invalid"


class DerivationStep(BaseModel):
    step: str
    detail: str


class PhaseInfo(BaseModel):
    phase: Literal["pretreatment", "analysis"]
    limit_minutes: Optional[float]
    deadline: Optional[datetime]
    done_at: Optional[datetime]
    status: PhaseStatus
    remaining_minutes: Optional[int] = Field(
        None, description="截止时刻 - eval_time（秒级四舍五入到分钟）；完成/无效为空"
    )
    basis_time: datetime
    rule_source: str


class ClockResult(BaseModel):
    sample_id: str
    item: str
    origin_sample_id: str = Field(..., description="基准时刻实际来源样品（合样为最早组成样）")
    basis: Literal["sampling_start", "sampling_end"]
    basis_time: datetime
    merged_at: Optional[datetime]
    status: ClockStatus
    conforming: bool
    phases: list[PhaseInfo]
    next_action_deadline: Optional[datetime]
    next_action: Optional[Literal["pretreatment", "analysis", "transfer"]]
    remaining_minutes: Optional[int]
    latest_operation_at: Optional[datetime] = Field(
        ..., description="该项目理论最晚操作时刻（分析截止）"
    )
    rule_source: str
    derivation: list[DerivationStep]


class Violation(BaseModel):
    code: Literal[
        "time_inversion",
        "source_cycle",
        "source_break",
        "temperature_excursion",
        "temperature_gap",
        "missing_preservation",
        "late_transfer",
        "item_rule_missing",
    ]
    sample_id: str
    items: list[str]
    message: str
    detail: dict
    derivation: list[DerivationStep] = Field(default_factory=list)


class PriorityBatch(BaseModel):
    sample_id: str
    items: list[str]
    due_at: datetime
    due_phase: Literal["pretreatment", "analysis", "transfer"]
    remaining_minutes: int
    reason: str


class RuleRef(BaseModel):
    version: str
    content_hash: str
    name: str
    item_count: int


class StatusChange(BaseModel):
    sample_id: str
    item: Optional[str] = None
    field: str
    before: Optional[str]
    after: Optional[str]


class JudgmentResult(BaseModel):
    package_id: str
    sample_id: Optional[str] = Field(
        None, description="样品级补录判定时的目标样品；整批接收时为空"
    )
    version_no: int
    trial: bool
    request_id: Optional[str]
    eval_time: datetime
    created_at: datetime
    rule: RuleRef
    basis_policy: dict
    summary: dict
    clocks: list[ClockResult]
    violations: list[Violation]
    priority_batches: list[PriorityBatch]
    changes_from: Optional[str] = Field(
        None, description="补录时所基于的上一版本 package_id"
    )
    changes: list[StatusChange] = Field(default_factory=list)
