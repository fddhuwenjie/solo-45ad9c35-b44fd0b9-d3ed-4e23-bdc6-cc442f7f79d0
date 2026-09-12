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
    rule_id: str = Field(
        "", description="规则项稳定标识（同一项目的不同适用规则以此区分）；为空取 item"
    )
    item_name: Optional[str] = None
    # 生效区间：半开 [effective_from, effective_to)，None 表示该侧无界
    effective_from: Optional[datetime] = Field(
        None, description="生效起始时刻（含）；按样品原始采样时刻筛选"
    )
    effective_to: Optional[datetime] = Field(
        None, description="生效结束时刻（不含）"
    )
    # 适用范围：空列表表示该维度通配（任意值均可）
    matrices: list[str] = Field(
        default_factory=list,
        description="适用样品基质，如 surface_water / groundwater / wastewater；空=通配",
    )
    methods: list[str] = Field(
        default_factory=list,
        description="适用分析方法（样品按项目提交 item_methods），如 hj828；空=通配",
    )
    containers: list[str] = Field(
        default_factory=list,
        description="适用容器，如 glass_amber / pe_bottle；空=通配",
    )
    storage_conditions: list[str] = Field(
        default_factory=list,
        description="适用保存条件，如 refrigerated_4c / dark / acidified；空=通配",
    )
    min_temp_c: float = -1_000_000.0
    max_temp_c: float = 1_000_000.0
    pretreatment_minutes: Optional[float] = Field(
        None, description="预处理期限（分钟），从基准起算；为空表示无此前置阶段"
    )
    analysis_minutes: float
    required_preservation: list[str] = Field(default_factory=list)
    continuous_basis: ContinuousBasis = ContinuousBasis.END
    note: Optional[str] = None

    @field_validator("effective_from", "effective_to")
    @classmethod
    def _tz(cls, v: Optional[datetime]) -> Optional[datetime]:
        return as_utc(v) if v is not None else None

    @model_validator(mode="after")
    def _normalize(self) -> "ItemRule":
        if not self.rule_id:
            object.__setattr__(self, "rule_id", self.item)
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to <= self.effective_from
        ):
            raise ValueError(
                f"规则项 {self.rule_id}(item={self.item}) 生效区间非法："
                "effective_to 必须晚于 effective_from（半开区间）"
            )
        return self


class RuleSet(BaseModel):
    """可版本化的时限规则集合。version 是调用方给的可读版本号；
    服务端另按规则内容计算 content_hash 作为不可变身份。"""
    version: str = Field(..., description="可读版本号，如 2026.1")
    name: str = "environmental-deadline-rules"
    items: list[ItemRule]
    note: Optional[str] = None

    @model_validator(mode="after")
    def _unique_and_non_overlapping(self) -> "RuleSet":
        if not self.items:
            raise ValueError("规则集至少包含一条项目规则")
        ids = [r.rule_id for r in self.items]
        if len(ids) != len(set(ids)):
            dup = sorted({x for x in ids if ids.count(x) > 1})
            raise ValueError(f"规则集中 rule_id 不得重复: {dup}")
        # 同一规则集内部即不得存在适用范围重叠（会导致正式接收多候选）
        from .matching import scope_conflict

        for i, r1 in enumerate(self.items):
            for r2 in self.items[i + 1:]:
                c = scope_conflict(r1, r2)
                if c is not None:
                    raise ValueError(
                        f"规则集内部适用范围冲突：rule_id={r1.rule_id} 与 "
                        f"{r2.rule_id}（item={r1.item}）在生效区间与适用条件上"
                        "同时命中，无法保证唯一匹配"
                    )
        return self


class Sample(BaseModel):
    id: str
    kind: SampleKind
    items: list[str] = Field(..., description="该样品（瓶）要测的项目")
    container: Optional[str] = Field(None, description="容器，如 glass_amber / pe_bottle")
    matrix: Optional[str] = Field(
        None, description="样品基质，如 surface_water / groundwater / wastewater"
    )
    storage_condition: Optional[str] = Field(
        None, description="实际保存条件，如 refrigerated_4c / dark / acidified"
    )
    item_methods: dict[str, str] = Field(
        default_factory=dict,
        description="按项目提交的分析方法，键为项目，值如 hj828 / hj535；可只覆盖部分项目",
    )
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
        unknown = [k for k in self.item_methods if k not in self.items]
        if unknown:
            raise ValueError(f"样品 {self.id} 的 item_methods 含未申报项目: {unknown}")
        return self


class CandidateSelection(BaseModel):
    """试算时强制指定某个时钟使用的规则项（对照用，不形成合规结论）。"""
    rule_id: str = Field(..., description="强制使用的规则项 rule_id")
    version: Optional[str] = Field(
        None, description="规则集可读版本；提供时必须命中且与 content_hash 一致"
    )
    content_hash: Optional[str] = Field(
        None, description="规则集内容哈希；提供时必须命中"
    )


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
        None,
        description="额外纳入候选池的已登记规则版本；与 rule_set 均可省略"
        "（省略时候选池为全部已登记规则）",
    )
    rule_set: Optional[RuleSet] = Field(
        None, description="随请求携带的规则集（试算对照或登记新版本用），并入候选池"
    )
    selected_candidates: Optional[dict[str, CandidateSelection]] = Field(
        None,
        description="试算专用：按样品—项目显式指定候选规则进行对照，"
        "键为 'sample_id/item'；正式接收提供该字段将被拒绝",
    )
    samples: list[Sample]

    @field_validator("eval_time")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return as_utc(v)

    @model_validator(mode="after")
    def _rules_and_refs(self) -> "JudgmentRequest":
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
                    raise ValueError(f"样品 {s.id} 的项目在携带规则集中缺失: {missing}")
        if self.selected_candidates:
            idset_pairs = {
                f"{s.id}/{i}" for s in self.samples for i in s.items
            }
            bad = [k for k in self.selected_candidates if k not in idset_pairs]
            if bad:
                raise ValueError(
                    f"selected_candidates 只能指定本请求内的样品—项目时钟: {bad}"
                )
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
    INDETERMINATE = "indeterminate"  # 无唯一适用规则/保存动作不足：不得形成合规结论
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


class FieldSource(BaseModel):
    field: str
    value: Optional[str]
    source_sample_id: str = Field(..., description="取值实际来自的样品（沿来源链可能是祖先）")
    source_field: str
    note: Optional[str] = None


class UnmetCondition(BaseModel):
    dimension: Literal[
        "effective_time", "matrix", "method", "container", "storage_condition"
    ]
    required: list
    actual: Optional[str] = None
    field_sources: list[FieldSource] = Field(
        default_factory=list,
        description="参与该维度判定的实际字段及来源样品（含沿来源链回退的取值）",
    )


class ClockRuleRef(BaseModel):
    """冻结到单个时钟的规则项身份。"""
    version: str
    content_hash: str
    rule_id: str
    item: str


class CandidateRuleView(BaseModel):
    version: str
    content_hash: str
    rule_id: str
    item: str
    item_name: Optional[str] = None
    applies: bool = Field(..., description="是否满足全部适用条件")
    unmet: list[UnmetCondition] = Field(default_factory=list)
    effective_from: Optional[datetime] = None
    effective_to: Optional[datetime] = None
    matrices: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    containers: list[str] = Field(default_factory=list)
    storage_conditions: list[str] = Field(default_factory=list)
    analysis_minutes: Optional[float] = None
    pretreatment_minutes: Optional[float] = None


class MatchContext(BaseModel):
    """规则筛选使用的实际条件；field_sources 逐字段说明取值来源。"""
    basis_time: datetime
    field_sources: list[FieldSource] = Field(default_factory=list)


class ClockResult(BaseModel):
    sample_id: str
    item: str
    origin_sample_id: str = Field(..., description="基准时刻实际来源样品（合样为最早组成样）")
    basis: Literal["sampling_start", "sampling_end"]
    basis_time: datetime
    merged_at: Optional[datetime]
    status: ClockStatus
    conforming: bool
    conclusive: bool = Field(
        ..., description="是否允许形成合规结论：规则不唯一/保存动作不足/结构失效时为 false"
    )
    phases: list[PhaseInfo]
    next_action_deadline: Optional[datetime]
    next_action: Optional[Literal["pretreatment", "analysis", "transfer"]]
    remaining_minutes: Optional[int]
    latest_operation_at: Optional[datetime] = Field(
        None, description="该项目理论最晚操作时刻（分析截止）；无适用规则时为空"
    )
    rule_source: str
    matched_rule: Optional[ClockRuleRef] = Field(
        None, description="唯一匹配并冻结到本时钟的规则项（无匹配/多候选时为空）"
    )
    match_status: Literal["unique", "none", "ambiguous", "forced", "forced_missing"] = Field(
        "unique",
        description="unique=唯一匹配；none=无候选；ambiguous=多同等候选；"
        "forced=试算强制；forced_missing=试算强制的规则不在候选池",
    )
    candidates: list[CandidateRuleView] = Field(
        default_factory=list, description="适用筛选所见候选规则（含未满足条件与字段来源）"
    )
    match_context: Optional[MatchContext] = Field(
        None, description="本次筛选使用的实际条件与各字段来源"
    )
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
        "rule_applicability",
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
    rule: RuleRef = Field(
        ..., description="代表性规则集（包内时钟冻结规则的首个，兼容旧客户端）"
    )
    rules: list[RuleRef] = Field(
        default_factory=list,
        description="本判定包各时钟实际冻结的全部规则集（按 content_hash 去重）",
    )
    selection_policy: dict = Field(
        default_factory=dict,
        description="规则适用性匹配策略说明（基准时刻、维度、半开区间、强制候选等）",
    )
    basis_policy: dict
    summary: dict
    clocks: list[ClockResult]
    violations: list[Violation]
    priority_batches: list[PriorityBatch]
    changes_from: Optional[str] = Field(
        None, description="补录时所基于的上一版本 package_id"
    )
    changes: list[StatusChange] = Field(default_factory=list)


# ================================================================ 排程 ----

class ResourceKind(str, Enum):
    PRETREATMENT = "pretreatment"  # 前处理工位
    INSTRUMENT = "instrument"      # 分析仪器


class TimeWindow(BaseModel):
    """半开时段 [start, end)：可用时段或停机窗。"""
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return as_utc(v)

    @model_validator(mode="after")
    def _order(self) -> "TimeWindow":
        if self.end <= self.start:
            raise ValueError("时段 end 必须晚于 start（半开区间）")
        return self


class ResourceConfig(BaseModel):
    """一个前处理工位或仪器的排程配置。"""
    resource_id: str
    kind: ResourceKind
    methods: list[str] = Field(
        default_factory=list,
        description="适用分析方法（对应样品 item_methods）；空列表=通配",
    )
    capacity: int = Field(1, ge=1, description="单批容量：同批最多任务数")
    task_minutes: float = Field(
        ..., gt=0, description="单批任务时长（分钟，整批占用该资源）"
    )
    switch_minutes: float = Field(
        0, ge=0, description="相邻批次方法不同时所需的方法切换时间（分钟）"
    )
    windows: list[TimeWindow] = Field(..., description="可用时段")
    downtime: list[TimeWindow] = Field(default_factory=list, description="停机窗")
    note: Optional[str] = None

    @model_validator(mode="after")
    def _shape(self) -> "ResourceConfig":
        if not self.resource_id:
            raise ValueError("resource_id 不能为空")
        if not self.windows:
            raise ValueError(f"资源 {self.resource_id} 至少需要一个可用时段")
        return self


class ResourceSet(BaseModel):
    """可版本化的资源配置集合。version 是可读版本号；
    服务端按资源内容计算 content_hash 作为不可变身份。"""
    version: str = Field(..., description="可读版本号，如 LAB-2026.1")
    name: str = "lab-resource-profile"
    resources: list[ResourceConfig]
    note: Optional[str] = None

    @model_validator(mode="after")
    def _unique(self) -> "ResourceSet":
        if not self.resources:
            raise ValueError("资源集至少包含一个资源")
        ids = [r.resource_id for r in self.resources]
        if len(ids) != len(set(ids)):
            dup = sorted({x for x in ids if ids.count(x) > 1})
            raise ValueError(f"资源集中 resource_id 不得重复: {dup}")
        return self


class ScheduleRequest(BaseModel):
    request_id: Optional[str] = Field(None, description="调用方追踪号")
    idempotency_key: Optional[str] = Field(
        None, description="签发幂等键；相同键重复签发返回同一排程版本"
    )
    schedule_time: datetime = Field(
        ..., description="排程基准时刻：任务不得安排在此刻之前，带时区"
    )
    package_ids: Optional[list[str]] = Field(
        None,
        description="参与排程的已冻结判定包；缺省=全部样品的最新正式判定",
    )
    resource_version: Optional[str] = Field(
        None, description="已登记资源版本；与 resource_set 均可省略"
        "（省略时使用最近登记的资源集）",
    )
    resource_set: Optional[ResourceSet] = Field(
        None, description="随请求携带的资源集（签发时登记，试排不写库）"
    )

    @field_validator("schedule_time")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return as_utc(v)

    @model_validator(mode="after")
    def _check(self) -> "ScheduleRequest":
        if self.resource_set is not None and self.resource_version:
            if self.resource_set.version != self.resource_version:
                raise ValueError("resource_set.version 与 resource_version 不一致")
        if self.package_ids is not None:
            if len(self.package_ids) != len(set(self.package_ids)):
                raise ValueError("package_ids 不得重复")
        return self


class ResourceRef(BaseModel):
    version: str
    content_hash: str
    name: str
    resource_count: int


class ScheduleTaskView(BaseModel):
    sample_id: str
    item: str
    phase: Literal["pretreatment", "analysis"]
    resource_id: str = Field(..., description="承担该任务的工位/仪器")
    batch_id: str
    method: Optional[str]
    start: datetime
    end: datetime
    deadline: datetime
    slack_minutes: int = Field(..., description="余量：截止时刻 - 批次结束（分钟）")
    on_time: bool
    frozen: bool = Field(False, description="来自已签发版本的冻结占用，不参与重排")
    rule: ClockRuleRef = Field(..., description="判定时冻结的规则身份（含规则哈希）")


class ScheduleBatchView(BaseModel):
    batch_id: str
    resource_id: str
    kind: ResourceKind
    method: Optional[str]
    start: datetime
    end: datetime
    capacity: int
    task_keys: list[str] = Field(
        default_factory=list, description="批内任务键：sample_id/item/phase"
    )
    frozen: bool
    switch_before_minutes: float = Field(
        0, description="与前一批次方法不同所需的切换时间（已计入间隔）"
    )


class ConflictInterval(BaseModel):
    start: datetime
    end: datetime


class AffectedClock(BaseModel):
    sample_id: str
    item: str
    phase: Literal["pretreatment", "analysis"]
    deadline: datetime


class ScheduleConflict(BaseModel):
    sample_id: str
    item: str
    phase: Literal["pretreatment", "analysis"]
    deadline: datetime
    reason: Literal["deadline_miss", "no_compatible_resource", "window_unavailable"]
    resource_id: Optional[str] = Field(None, description="最早可承载该任务的资源")
    interval: Optional[ConflictInterval] = Field(
        None, description="该任务最早可占用的区间（结束仍晚于截止）"
    )
    minutes_late: Optional[int] = None
    affected_clocks: list[AffectedClock] = Field(
        default_factory=list,
        description="受本冲突波及的时钟（如前处理失败连带分析被阻断）",
    )
    message: str


class FrozenSource(BaseModel):
    schedule_id: str
    version_no: int


class ScheduleResult(BaseModel):
    schedule_id: str
    version_no: int
    trial: bool
    request_id: Optional[str]
    schedule_time: datetime
    created_at: datetime
    resource: ResourceRef
    content_hash: str = Field(
        ..., description="排程内容哈希（不含 id/版本/生成时刻）：相同输入得到稳定结果"
    )
    status: Literal["feasible", "infeasible"] = Field(
        ..., description="feasible=全部待排任务均可准时纳入计划；infeasible=存在无解冲突"
    )
    frozen_from: Optional[FrozenSource] = Field(
        None, description="本次试排叠加的已签发占用来源"
    )
    summary: dict
    packages: list[str] = Field(
        default_factory=list, description="参与排程的判定包 package_id"
    )
    tasks: list[ScheduleTaskView]
    batches: list[ScheduleBatchView]
    conflicts: list[ScheduleConflict]
    earliest_conflict: Optional[ScheduleConflict] = Field(
        None, description="最早冲突（按区间开始时刻排序）"
    )
    skipped_clocks: list[dict] = Field(
        default_factory=list,
        description="有待办阶段但未形成合规结论、无法排程的时钟",
    )
