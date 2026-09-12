"""期限驱动的实验排程引擎（纯函数，不依赖持久层）。

输入：

* 待排任务：从已冻结判定包的时钟提取的待办阶段（前处理/分析，含截止时刻）；
* 资源配置：前处理工位与仪器（可用时段、停机窗、单批容量、单批任务时长、
  方法切换时间、适用方法）；
* 已签发占用：上一签发版本冻结的批次（不可改排，只占住资源时段）。

约束：

1. 前处理先于分析：同一（样品, 项目）的分析任务开始时刻不早于其前处理批次
   结束时刻（前处理已签发冻结的，按冻结批次结束时刻）；
2. 资源不重叠：同一资源上的批次（含已签发占用）两两不相交；相邻批次方法
   不同时，间隔至少 ``switch_minutes``（方法切换时间）；
3. 批内兼容：同批任务方法相同，且与资源适用方法兼容（资源 ``methods`` 为空
   表示通配；任务方法缺失时只能使用通配资源）；单批任务数不超过
   ``capacity``；
4. 时段可行：批次完整落在“可用时段减去停机窗”的空闲区间内、不早于排程
   基准时刻，且批次结束不晚于任务截止时刻（按各时钟截止时间判断能否纳入
   计划）。

目标（字典序）：

1. 先让更多项目准时完成——只接受不晚于截止时刻的排入；无法准时的任务不
   纳入计划，转入 ``conflicts``，指出最早冲突的资源、区间与受影响时钟；
2. 再减少方法切换——同等可行时优先并入既有同方法批次（零切换），新建批次
   取“净切换数最少、结束最早、资源 id 最小”的位置；
3. 相同输入得到稳定结果——所有迭代与 tie-break 确定，无随机性。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .models import ResourceConfig, ResourceKind

PRETREATMENT = "pretreatment"
ANALYSIS = "analysis"


@dataclass
class SchedTask:
    """一个待排阶段任务（某样品—项目时钟的前处理或分析）。"""
    sample_id: str
    item: str
    phase: str  # "pretreatment" | "analysis"
    method: Optional[str]
    deadline: datetime
    rule: dict  # {version, content_hash, rule_id, item}

    @property
    def key(self) -> str:
        return f"{self.sample_id}/{self.item}/{self.phase}"


@dataclass
class Occupation:
    """资源上的一个批次占用（新排或已签发冻结）。"""
    batch_id: str
    resource_id: str
    method: Optional[str]
    start: datetime
    end: datetime
    frozen: bool
    task_keys: list[str] = field(default_factory=list)


# ------------------------------------------------------------- 时段 ----

def _free_intervals(
    cfg: ResourceConfig, clip_start: datetime
) -> list[tuple[datetime, datetime]]:
    """可用时段合并、减去停机窗，再裁到 [clip_start, ∞)。"""
    wins = sorted((w.start, w.end) for w in cfg.windows)
    merged: list[tuple[datetime, datetime]] = []
    for s, e in wins:
        if merged and s <= merged[-1][1]:
            last = merged[-1]
            merged[-1] = (last[0], e if e > last[1] else last[1])
        else:
            merged.append((s, e))
    downs = sorted((d.start, d.end) for d in cfg.downtime)
    free: list[tuple[datetime, datetime]] = []
    for s, e in merged:
        cur = s
        for ds, de in downs:
            if de <= cur:
                continue
            if ds >= e:
                break
            if ds > cur:
                free.append((cur, ds if ds < e else e))
            cur = de if de > cur else cur
            if cur >= e:
                break
        if cur < e:
            free.append((cur, e))
    out: list[tuple[datetime, datetime]] = []
    for s, e in free:
        s2 = s if s > clip_start else clip_start
        if e > s2:
            out.append((s2, e))
    return out


class _ResourceState:
    def __init__(self, cfg: ResourceConfig, schedule_time: datetime):
        self.cfg = cfg
        self.free = _free_intervals(cfg, schedule_time)
        self.occs: list[Occupation] = []

    @property
    def rid(self) -> str:
        return self.cfg.resource_id

    def compatible(self, method: Optional[str]) -> bool:
        """资源 methods 为空=通配；任务方法缺失时只能落在通配资源上。"""
        return not self.cfg.methods or (
            method is not None and method in self.cfg.methods
        )

    def sorted_occs(self) -> list[Occupation]:
        return sorted(self.occs, key=lambda o: (o.start, o.batch_id))


# --------------------------------------------------------- 插入位置 ----

def _try_gap(
    g0: datetime,
    g1: datetime,
    prev_occ: Optional[Occupation],
    next_occ: Optional[Occupation],
    method: Optional[str],
    dur: timedelta,
    tsw: timedelta,
    ready: datetime,
    deadline: Optional[datetime],
) -> Optional[tuple[datetime, datetime, int]]:
    """在空隙 [g0, g1) 内放入一个批次。返回 (start, end, 净切换数) 或 None。

    净切换数 = 新批次与前后相邻批次产生的切换数 - 被消除的原相邻切换数。
    """
    start = g0 if g0 > ready else ready
    if prev_occ is not None and prev_occ.method != method:
        t = prev_occ.end + tsw
        if t > start:
            start = t
    end_limit = g1
    if next_occ is not None and next_occ.method != method:
        t = next_occ.start - tsw
        if t < end_limit:
            end_limit = t
    end = start + dur
    if end > end_limit:
        return None
    if deadline is not None and end > deadline:
        return None
    added = (1 if prev_occ is not None and prev_occ.method != method else 0) + (
        1 if next_occ is not None and next_occ.method != method else 0
    )
    removed = 1 if (
        prev_occ is not None
        and next_occ is not None
        and prev_occ.method != next_occ.method
    ) else 0
    return (start, end, added - removed)


def _find_new_slot(
    state: _ResourceState,
    method: Optional[str],
    ready: datetime,
    deadline: Optional[datetime],
) -> Optional[tuple[datetime, datetime, int]]:
    """该资源上“净切换最少、结束最早”的整批插入位置；无可行位置返回 None。

    deadline 为 None 表示不限（用于无解时报告最早可行区间）。
    """
    cfg = state.cfg
    dur = timedelta(minutes=cfg.task_minutes)
    tsw = timedelta(minutes=cfg.switch_minutes)
    occs = state.sorted_occs()
    best: Optional[tuple[datetime, datetime, int]] = None
    for fs, fe in state.free:
        cursor = fs if fs > ready else ready
        prev: Optional[Occupation] = None
        for occ in occs:
            if occ.end <= cursor:
                prev = occ  # 最后一个 end <= cursor 的占用是空隙前驱
                continue
            if occ.start >= fe:
                break
            cand = _try_gap(cursor, occ.start, prev, occ, method,
                            dur, tsw, ready, deadline)
            if cand is not None and (
                best is None or (cand[2], cand[1]) < (best[2], best[1])
            ):
                best = cand
            cursor = occ.end if occ.end > cursor else cursor
            prev = occ
            if cursor >= fe:
                break
        if cursor < fe:
            cand = _try_gap(cursor, fe, prev, None, method,
                            dur, tsw, ready, deadline)
            if cand is not None and (
                best is None or (cand[2], cand[1]) < (best[2], best[1])
            ):
                best = cand
    return best


def _best_placement(
    states: list[_ResourceState],
    task: SchedTask,
    ready: datetime,
    deadline: Optional[datetime],
):
    """跨资源选最优排入。返回 (cost, kind, state, occ|None, start, end)。

    cost 元组保证确定性：净切换数 -> 结束时刻 -> 新建劣于并入 -> 资源 id ->
    批次 id。无可行排入返回 None。
    """
    best = None
    for st in states:
        if not st.compatible(task.method):
            continue
        # 并入既有同方法批次（零切换、不新增占用；冻结批次不可并入）
        for occ in st.occs:
            if occ.frozen or occ.method != task.method:
                continue
            if len(occ.task_keys) >= st.cfg.capacity:
                continue
            if occ.start < ready:
                continue
            if deadline is not None and occ.end > deadline:
                continue
            cost = (0, occ.end, 0, st.rid, occ.batch_id)
            if best is None or cost < best[0]:
                best = (cost, "join", st, occ, occ.start, occ.end)
        # 新建批次
        slot = _find_new_slot(st, task.method, ready, deadline)
        if slot is not None:
            start, end, net = slot
            cost = (net, end, 1, st.rid, "")
            if best is None or cost < best[0]:
                best = (cost, "new", st, None, start, end)
    return best


def _earliest_slot(
    states: list[_ResourceState],
    task: SchedTask,
    ready: datetime,
) -> Optional[tuple[datetime, str, str, datetime]]:
    """忽略截止时刻的最早可行占用（用于无解时报告冲突区间）。

    返回 (end, resource_id, batch_id, start)。
    """
    best = None
    for st in states:
        if not st.compatible(task.method):
            continue
        for occ in st.occs:
            if occ.frozen or occ.method != task.method:
                continue
            if len(occ.task_keys) >= st.cfg.capacity:
                continue
            if occ.start < ready:
                continue
            cand = (occ.end, st.rid, occ.batch_id, occ.start)
            if best is None or cand < best:
                best = cand
        slot = _find_new_slot(st, task.method, ready, None)
        if slot is not None:
            start, end, _net = slot
            cand = (end, st.rid, "", start)
            if best is None or cand < best:
                best = cand
    return best


# ------------------------------------------------------------- 冲突 ----

def _affected(task: SchedTask) -> dict:
    return {
        "sample_id": task.sample_id,
        "item": task.item,
        "phase": task.phase,
        "deadline": task.deadline,
    }


def _make_conflict(
    task: SchedTask,
    states: list[_ResourceState],
    ready: datetime,
    affected: list[dict],
) -> dict:
    compatible = [st for st in states if st.compatible(task.method)]
    slot = _earliest_slot(compatible, task, ready) if compatible else None
    kind_label = "前处理工位" if task.phase == PRETREATMENT else "仪器"
    if not compatible:
        reason = "no_compatible_resource"
        resource_id, interval, minutes_late = None, None, None
        msg = (
            f"{task.sample_id}/{task.item}/{task.phase} 无兼容{kind_label}"
            f"（方法 {task.method or '未提交'}），无法纳入计划"
        )
    elif slot is None:
        reason = "window_unavailable"
        resource_id, interval, minutes_late = None, None, None
        msg = (
            f"{task.sample_id}/{task.item}/{task.phase} 的任务时长放不进任何"
            "可用时段减去停机窗后的空闲区间，无法纳入计划"
        )
    else:
        end, resource_id, _bid, start = slot
        reason = "deadline_miss"
        interval = {"start": start, "end": end}
        minutes_late = int(round((end - task.deadline).total_seconds() / 60.0))
        msg = (
            f"{task.sample_id}/{task.item}/{task.phase} 最早可于 "
            f"{start.isoformat()} 在 {resource_id} 开始、{end.isoformat()} 结束，"
            f"晚于截止 {task.deadline.isoformat()} {minutes_late} 分钟"
        )
    return {
        "sample_id": task.sample_id,
        "item": task.item,
        "phase": task.phase,
        "deadline": task.deadline,
        "reason": reason,
        "resource_id": resource_id,
        "interval": interval,
        "minutes_late": minutes_late,
        "affected_clocks": affected,
        "message": msg,
    }


# ------------------------------------------------------------- 主流程 ----

def compute_schedule(
    *,
    tasks: list[SchedTask],
    resources: list[ResourceConfig],
    frozen_batches: list[Occupation],
    frozen_tasks: list[dict],
    schedule_time: datetime,
) -> dict:
    """试排主流程。返回 task_views / batch_views / conflicts / 汇总计数。"""
    states = {cfg.resource_id: _ResourceState(cfg, schedule_time)
              for cfg in resources}
    dropped_frozen = 0
    for occ in frozen_batches:
        st = states.get(occ.resource_id)
        if st is None:
            dropped_frozen += 1  # 资源已不在当前配置中：占用不再约束
            continue
        st.occs.append(occ)

    # 已签发的前处理批次结束时刻：分析任务的前驱约束
    frozen_intervals: dict[tuple[str, str, str], tuple[datetime, datetime]] = {}
    for t in frozen_tasks:
        frozen_intervals[(t["sample_id"], t["item"], t["phase"])] = (
            datetime.fromisoformat(t["start"]),
            datetime.fromisoformat(t["end"]),
        )

    stations = sorted(
        (s for s in states.values() if s.cfg.kind == ResourceKind.PRETREATMENT),
        key=lambda s: s.rid,
    )
    instruments = sorted(
        (s for s in states.values() if s.cfg.kind == ResourceKind.INSTRUMENT),
        key=lambda s: s.rid,
    )

    # 按时钟分组；EDF（时钟最早截止）+ 确定性 tie-break
    by_clock: dict[tuple[str, str], dict[str, SchedTask]] = {}
    for t in tasks:
        by_clock.setdefault((t.sample_id, t.item), {})[t.phase] = t
    clock_order = sorted(
        by_clock.items(),
        key=lambda kv: (min(x.deadline for x in kv[1].values()),
                        kv[0][0], kv[0][1]),
    )

    batch_ids: dict[str, set[str]] = {rid: set() for rid in states}
    for rid, st in states.items():
        for o in st.occs:
            batch_ids[rid].add(o.batch_id)

    def new_batch(st: _ResourceState, method, start, end) -> Occupation:
        seq = len(st.occs) + 1
        bid = f"{st.rid}#b{seq:03d}"
        while bid in batch_ids[st.rid]:
            seq += 1
            bid = f"{st.rid}#b{seq:03d}"
        occ = Occupation(bid, st.rid, method, start, end, False, [])
        st.occs.append(occ)
        batch_ids[st.rid].add(bid)
        return occ

    def apply(best, task: SchedTask) -> Occupation:
        _cost, kind, st, occ, start, end = best
        if kind != "join":
            occ = new_batch(st, task.method, start, end)
        occ.task_keys.append(task.key)
        return occ

    placed: list[tuple[SchedTask, Occupation]] = []
    conflicts: list[dict] = []

    for (sid, item), phases in clock_order:
        pre = phases.get(PRETREATMENT)
        ana = phases.get(ANALYSIS)
        pre_end: Optional[datetime] = None
        if pre is not None:
            best = _best_placement(stations, pre, schedule_time, pre.deadline)
            if best is None:
                # 前处理无法准时 -> 同一时钟的分析连带受阻，不再尝试
                affected = [_affected(pre)] + ([_affected(ana)] if ana else [])
                conflicts.append(
                    _make_conflict(pre, stations, schedule_time, affected))
                continue
            occ = apply(best, pre)
            placed.append((pre, occ))
            pre_end = occ.end
        if ana is not None:
            ready = schedule_time
            if pre_end is not None:
                ready = pre_end  # 前处理先于分析（本次新排）
            else:
                fz = frozen_intervals.get((sid, item, PRETREATMENT))
                if fz is not None and fz[1] > ready:
                    ready = fz[1]  # 前处理已签发冻结：按冻结批次结束时刻
            best = _best_placement(instruments, ana, ready, ana.deadline)
            if best is None:
                conflicts.append(
                    _make_conflict(ana, instruments, ready, [_affected(ana)]))
                continue
            occ = apply(best, ana)
            placed.append((ana, occ))

    task_views: list[dict] = []
    for task, occ in placed:
        task_views.append({
            "sample_id": task.sample_id,
            "item": task.item,
            "phase": task.phase,
            "resource_id": occ.resource_id,
            "batch_id": occ.batch_id,
            "method": occ.method,
            "start": occ.start,
            "end": occ.end,
            "deadline": task.deadline,
            "slack_minutes": int(round(
                (task.deadline - occ.end).total_seconds() / 60.0)),
            "on_time": True,
            "frozen": False,
            "rule": task.rule,
        })

    batch_views: list[dict] = []
    total_switches = 0
    for rid in sorted(states):
        st = states[rid]
        seq = st.sorted_occs()
        for i, occ in enumerate(seq):
            switch_before = 0.0
            if i > 0 and seq[i - 1].method != occ.method:
                switch_before = st.cfg.switch_minutes
                total_switches += 1
            batch_views.append({
                "batch_id": occ.batch_id,
                "resource_id": rid,
                "kind": st.cfg.kind.value,
                "method": occ.method,
                "start": occ.start,
                "end": occ.end,
                "capacity": st.cfg.capacity,
                "task_keys": sorted(occ.task_keys),
                "frozen": occ.frozen,
                "switch_before_minutes": switch_before,
            })
    batch_views.sort(key=lambda b: (b["resource_id"], b["start"], b["batch_id"]))

    return {
        "task_views": task_views,
        "batch_views": batch_views,
        "conflicts": conflicts,
        "method_switches": total_switches,
        "dropped_frozen": dropped_frozen,
    }
