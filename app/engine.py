"""时限判定引擎。

为每个“样品—项目”建立独立时钟：

* 瞬时样基准为采样时刻；连续样默认按采样结束（规则可改为开始）。
* 分样（aliquot）继承母体基准与全部历史；合样（composite）取组成样中最早的
  基准，``merged_at`` 之前的历史共享、之后独立——分析期限绝不会被重置。
* 前处理事件只结束“预处理”阶段；“分析”期限始终从原始基准连续计算。

规则不再由调用方整包指定：引擎接收一个候选池（全部已登记规则集 + 请求携带集），
按时钟解析出的**原始采样时刻**（沿来源链）与样品实际条件（基质、该项目的分析
方法、容器、保存条件）筛选规则项。唯一匹配才交给时钟计算；无匹配/多同等候选时
时钟为 ``indeterminate``，响应携带候选、未满足条件与字段来源，不形成合规结论。

**时间不确定性传播**：采样起止、防腐、前处理、分析、交接均可提交
``{earliest, latest}`` 区间（精确时刻视为退化区间）。基准、截止与剩余分钟沿
分样/合样来源链以区间推导：整区间未超时判 compliant，整区间越界判 overdue，
跨过期限判 indeterminate；区间互不可能、合样边界无有效交集或事件次序无法
确定时，产生 ``time_uncertainty_conflict`` 违规并列出冲突来源。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from .matching import DIMENSIONS, evaluate_applicability, interval_position
from .models import (
    CandidateRuleView,
    ClockResult,
    ClockRuleRef,
    ClockStatus,
    ClockUncertainty,
    ContinuousBasis,
    DerivationStep,
    FieldSource,
    IntervalView,
    ItemRule,
    JudgmentRequest,
    JudgmentResult,
    MatchContext,
    MinutesInterval,
    PhaseInfo,
    PhaseStatus,
    PhaseUncertainty,
    PriorityBatch,
    RuleRef,
    RuleSet,
    Sample,
    StatusChange,
    TimeRange,
    TimeSpec,
    UncertaintySource,
    UnmetCondition,
    Violation,
    as_range,
)

GAP_TOLERANCE_MIN = 15.0  # 温度记录断档容差（分钟）


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def iso_range(r: Optional[TimeRange]) -> Optional[str]:
    """区间展示：精确时刻为单个 ISO 字符串，区间为 'e~l'。"""
    if r is None:
        return None
    if r.exact:
        return r.earliest.isoformat()
    return f"{r.earliest.isoformat()}~{r.latest.isoformat()}"


def _remaining_minutes(deadline: datetime, eval_time: datetime) -> int:
    return int(round((deadline - eval_time).total_seconds() / 60.0))


def _shift(r: TimeRange, minutes: float) -> TimeRange:
    delta = timedelta(minutes=minutes)
    return TimeRange(earliest=r.earliest + delta, latest=r.latest + delta)


def _mid(r: TimeRange) -> datetime:
    return r.midpoint()


def _iview(r: TimeRange) -> IntervalView:
    return IntervalView(earliest=r.earliest, latest=r.latest, exact=r.exact)


@dataclass(frozen=True)
class PoolEntry:
    """候选池中的一条项目规则。"""
    version: str
    content_hash: str
    rule: ItemRule
    adhoc: bool = False  # 请求携带（未登记）规则集


@dataclass
class HistEvent:
    """来源链上收集到的一条事件记录。

    ``wall_certain=False`` 表示该记录的时刻区间跨过合样墙，无法确定它
    是否发生在合样之前（可能不属于共享历史）。
    """
    event: Any          # PreservationAction / PretreatmentEvent / AnalysisEvent / TimeSpec
    source: str         # 记录来源样品 id
    wall_certain: bool

    def range(self) -> TimeRange:
        return as_range(self.event if isinstance(self.event, (datetime, TimeRange))
                        else self.event.time)


class _Engine:
    def __init__(self, request: JudgmentRequest, pool: list[PoolEntry]):
        self.req = request
        self.pool = pool
        self.by_item: dict[str, list[PoolEntry]] = {}
        for e in pool:
            self.by_item.setdefault(e.rule.item, []).append(e)
        self.samples: dict[str, Sample] = {s.id: s for s in request.samples}
        self.children: dict[str, list[str]] = {sid: [] for sid in self.samples}
        for s in request.samples:
            for p in s.parent_ids:
                self.children.setdefault(p, []).append(s.id)

        self.inversions: dict[str, list[dict]] = {}  # sample_id -> 原始倒置记录
        self._detect_inversions()
        self.inv_tainted = self._downward(set(self.inversions))

        # 时间区间冲突：次序无法确定 / 范围互不可能 / 合样边界无有效交集
        self.conflicts: dict[str, list[dict]] = {}
        self._detect_conflicts()
        self.conflict_tainted = self._downward(set(self.conflicts))

        self.cycle_tainted = self._cycle_nodes()
        self.break_nodes: set[str] = set()
        for s in request.samples:
            for p in s.parent_ids:
                if p not in self.samples:
                    self.break_nodes.add(s.id)
        self.break_tainted = self._downward(set(self.break_nodes))

        # 基准解析记忆：(sid, item, basis) -> (origin_id, basis_kind, time, merged_at)
        self._basis_memo: dict[tuple[str, str, str], tuple] = {}

    # -------------------------------------------------------- 图辅助 ----

    def _downward(self, seeds: set[str]) -> set[str]:
        """从种子样品向下传播到所有子代（分样/合样的后代）。"""
        out, stack = set(seeds), list(seeds)
        while stack:
            cur = stack.pop()
            for ch in self.children.get(cur, []):
                if ch not in out and ch in self.samples:
                    out.add(ch)
                    stack.append(ch)
        return out

    def _cycle_nodes(self) -> set[str]:
        """返回处于来源环上或祖先链触达环的样品集合。"""
        color: dict[str, int] = {sid: 0 for sid in self.samples}  # 0白1灰2黑
        cyclic: set[str] = set()

        def visit(u: str, path: list[str]) -> None:
            color[u] = 1
            path.append(u)
            for p in self.samples[u].parent_ids:
                if p not in self.samples:
                    continue
                if color[p] == 1:
                    idx = path.index(p)
                    cyclic.update(path[idx:])
                elif color[p] == 0:
                    visit(p, path)
            path.pop()
            color[u] = 2

        for sid in self.samples:
            if color[sid] == 0:
                visit(sid, [])
        return self._downward(cyclic)

    # ----------------------------------------------------- 时间倒置 ----

    def _add_inversion(self, sid: str, code: str, message: str, detail: dict) -> None:
        self.inversions.setdefault(sid, []).append(
            {"code": code, "message": message, "detail": detail}
        )

    def _detect_inversions(self) -> None:
        """确定的时间倒置（区间两端都满足倒置关系才判定）。

        只能圈定范围、次序无法确定的情形不在这里，而归入
        ``time_uncertainty_conflict``（见 ``_detect_conflicts``）。
        """
        for s in self.req.samples:
            start, end = as_range(s.sampling_start), as_range(s.sampling_end)
            if self.req.eval_time < start.earliest:
                self._add_inversion(
                    s.id, "eval_before_sampling",
                    f"样品 {s.id} 判定时刻早于采样开始",
                    {"eval_time": iso(self.req.eval_time),
                     "sampling_start": iso_range(start)},
                )
            if end.latest < start.earliest:
                self._add_inversion(
                    s.id,
                    "sampling_interval_reversed",
                    f"样品 {s.id} 采样结束早于采样开始",
                    {"sampling_start": iso_range(start),
                     "sampling_end": iso_range(end)},
                )
            if s.kind.value == "composite":
                wall = as_range(s.merged_at) if s.merged_at is not None else start
                if s.merged_at is not None and wall.latest < start.earliest:
                    self._add_inversion(
                        s.id, "merge_before_sampling",
                        f"样品 {s.id} 合样时刻早于其采样开始",
                        {"merged_at": iso_range(wall),
                         "sampling_start": iso_range(start)},
                    )
                if wall.latest < end.earliest:
                    self._add_inversion(
                        s.id, "merge_before_sampling_end",
                        f"样品 {s.id} 合样时刻早于采样结束",
                        {"merged_at": iso_range(wall),
                         "sampling_end": iso_range(end)},
                    )
            if s.kind.value == "aliquot":
                for pid in s.parent_ids:
                    p = self.samples.get(pid)
                    if p and start.latest < as_range(p.sampling_end).earliest:
                        self._add_inversion(
                            s.id, "split_before_parent_ready",
                            f"分样 {s.id} 分装时刻早于母体 {pid} 采样结束",
                            {"split_at": iso_range(start),
                             "parent_sampling_end": iso_range(
                                 as_range(p.sampling_end))},
                        )
            labels = [
                ("preservation", [a.time for a in s.preservation]),
                ("pretreatments", [e.time for e in s.pretreatments]),
                ("analyses", [e.time for e in s.analyses]),
                ("temperature", [t.time for t in s.temperature]),
                ("custody_transfers", list(s.custody_transfers)),
            ]
            for name, times in labels:
                ranges = [as_range(t) for t in times]
                if any(
                    ranges[i + 1].latest < ranges[i].earliest
                    for i in range(len(ranges) - 1)
                ):
                    self._add_inversion(
                        s.id, f"{name}_not_monotonic",
                        f"样品 {s.id} 的 {name} 时间序列存在倒置",
                        {"times": [iso_range(t) for t in ranges]},
                    )
                for t in ranges:
                    if t.latest < start.earliest:
                        self._add_inversion(
                            s.id, f"{name}_before_sampling",
                            f"样品 {s.id} 的 {name} 记录早于采样开始",
                            {"event_time": iso_range(t),
                             "sampling_start": iso_range(start)},
                        )

    # ------------------------------------------------- 时间区间冲突 ----

    def _add_conflict(
        self, sid: str, code: str, message: str, sources: list[dict]
    ) -> None:
        self.conflicts.setdefault(sid, []).append(
            {"code": code, "message": message, "detail": {"sources": sources}}
        )

    @staticmethod
    def _src(sid: str, field: str, r: TimeRange,
             label: Optional[str] = None) -> dict:
        return {
            "sample_id": sid, "field": field,
            "earliest": iso(r.earliest), "latest": iso(r.latest),
            "label": label,
        }

    def _detect_conflicts(self) -> None:
        """区间层面的冲突：次序无法确定 / 范围互不可能 / 合样边界无有效交集。

        与 ``_detect_inversions`` 互补：倒置是“确定反向”，冲突是“可能反向、
        无法确定”或“根本不存在一致赋值”。精确时刻下本方法不产生任何记录。
        """
        eval_time = self.req.eval_time
        for s in self.req.samples:
            start, end = as_range(s.sampling_start), as_range(s.sampling_end)

            def overlaps(a: TimeRange, b: TimeRange) -> bool:
                """两区间交叠且任一方向都不是确定次序（精确时刻下恒 False）。"""
                return (
                    a.earliest < b.latest and b.earliest < a.latest
                    and not (a.latest < b.earliest or b.latest < a.earliest)
                )

            def record_overlap(a: TimeRange, b: TimeRange) -> bool:
                """同一记录时刻（区间完全相同）不算次序冲突。"""
                if a.earliest == b.earliest and a.latest == b.latest:
                    return False
                return overlaps(a, b)

            # 采样起止区间交叠：结束可能在开始之前，次序无法确定
            if record_overlap(end, start):
                self._add_conflict(
                    s.id, "sampling_order_uncertain",
                    f"样品 {s.id} 采样起止区间交叠，采样先后次序无法确定",
                    [self._src(s.id, "sampling_start", start),
                     self._src(s.id, "sampling_end", end)],
                )
            # 判定时刻落在采样开始区间内：无法确定采样是否已开始
            if start.earliest <= eval_time < start.latest:
                self._add_conflict(
                    s.id, "eval_within_sampling_start",
                    f"样品 {s.id} 判定时刻落在采样开始区间内，"
                    "无法确定采样是否已开始",
                    [self._src(s.id, "sampling_start", start),
                     self._src(s.id, "eval_time",
                               TimeRange(earliest=eval_time, latest=eval_time))],
                )
            if s.kind.value == "composite":
                wall = as_range(s.merged_at) if s.merged_at is not None else start
                if s.merged_at is not None and record_overlap(wall, start):
                    self._add_conflict(
                        s.id, "merge_order_uncertain",
                        f"样品 {s.id} 合样时刻与采样开始区间交叠，次序无法确定",
                        [self._src(s.id, "merged_at", wall),
                         self._src(s.id, "sampling_start", start)],
                    )
                if record_overlap(wall, end):
                    self._add_conflict(
                        s.id, "merge_end_order_uncertain",
                        f"样品 {s.id} 合样时刻与采样结束区间交叠，次序无法确定",
                        [self._src(s.id, "merged_at", wall),
                         self._src(s.id, "sampling_end", end)],
                    )
                # 合样边界与组成样采样结束的有效交集：合样必须不早于各组成样
                # 采样结束；区间下界早于某组成样上界即不存在确定的有效交集
                for pid in s.parent_ids:
                    p = self.samples.get(pid)
                    if p is None:
                        continue
                    pend = as_range(p.sampling_end)
                    if wall.latest < pend.earliest:
                        self._add_conflict(
                            s.id, "merge_boundary_no_intersection",
                            f"样品 {s.id} 合样时刻区间最晚端早于组成样 {pid} "
                            "采样结束最早端，合样边界没有有效交集",
                            [self._src(s.id, "merged_at", wall),
                             self._src(pid, "sampling_end", pend)],
                        )
                    elif wall.earliest < pend.latest:
                        self._add_conflict(
                            s.id, "merge_boundary_order_uncertain",
                            f"样品 {s.id} 合样时刻与组成样 {pid} 采样结束区间"
                            "交叠，合样先后次序无法确定",
                            [self._src(s.id, "merged_at", wall),
                             self._src(pid, "sampling_end", pend)],
                        )
            if s.kind.value == "aliquot":
                for pid in s.parent_ids:
                    p = self.samples.get(pid)
                    if p is None:
                        continue
                    pend = as_range(p.sampling_end)
                    if overlaps(start, pend):
                        self._add_conflict(
                            s.id, "split_order_uncertain",
                            f"分样 {s.id} 分装时刻与母体 {pid} 采样结束区间交叠，"
                            "次序无法确定",
                            [self._src(s.id, "sampling_start", start),
                             self._src(pid, "sampling_end", pend)],
                        )
            labels = [
                ("preservation", [(a.time, a.name) for a in s.preservation]),
                ("pretreatments", [(e.time, e.type) for e in s.pretreatments]),
                ("analyses", [(e.time, e.item) for e in s.analyses]),
                ("temperature", [(t.time, None) for t in s.temperature]),
                ("custody_transfers", [(t, None) for t in s.custody_transfers]),
            ]
            for name, timed in labels:
                ranges = [(as_range(t), label) for t, label in timed]
                for i in range(len(ranges) - 1):
                    (a, la), (b, lb) = ranges[i], ranges[i + 1]
                    if overlaps(a, b):
                        self._add_conflict(
                            s.id, f"{name}_order_uncertain",
                            f"样品 {s.id} 的 {name} 相邻记录区间交叠，"
                            "先后次序无法确定",
                            [self._src(s.id, name, a, la),
                             self._src(s.id, name, b, lb)],
                        )
                for r, label in ranges:
                    if overlaps(r, start):
                        self._add_conflict(
                            s.id, f"{name}_vs_sampling_uncertain",
                            f"样品 {s.id} 的 {name} 记录与采样开始区间交叠，"
                            "事件是否发生在采样之后无法确定",
                            [self._src(s.id, name, r, label),
                             self._src(s.id, "sampling_start", start)],
                        )

    # ------------------------------------------------------- 基准 ----

    def _root_basis(
        self, s: Sample, item: str, rule: Optional[ItemRule]
    ) -> tuple[str, str, TimeRange]:
        use_start = (
            s.kind.value == "continuous"
            and rule is not None
            and rule.continuous_basis == ContinuousBasis.START
        )
        if use_start:
            return s.id, "sampling_start", as_range(s.sampling_start)
        return s.id, "sampling_end", as_range(s.sampling_end)

    def resolve_basis(
        self,
        sid: str,
        item: str,
        rule: Optional[ItemRule] = None,
        stack: Optional[list[str]] = None,
    ) -> tuple[str, str, TimeRange, Optional[TimeRange]]:
        """返回 (origin_sample_id, basis_kind, 基准区间, 合样墙区间)。

        连续样取开始还是结束取决于候选规则自身的 continuous_basis，因此基准
        解析按 (sid, item, basis) 记忆。合样基准为组成样基准的最早者：
        区间取 [各组成样最早端的最小值, 各组成样最晚端的最小值]——即使
        最早组成样本身不确定，该区间仍然成立。
        """
        stack = stack or []
        key = (
            sid, item,
            rule.continuous_basis.value if rule is not None else ContinuousBasis.END.value,
        )
        memo = self._basis_memo
        if key in memo:
            return memo[key]
        if sid in stack:
            raise _Cycle(sid)
        s = self.samples.get(sid)
        if s is None:
            raise _Break(sid)
        if any(p not in self.samples for p in s.parent_ids):
            raise _Break(sid)

        if not s.parent_ids:
            if s.kind.value in ("aliquot", "composite"):
                raise _Break(sid)
            res = (*self._root_basis(s, item, rule), None)
            memo[key] = res
            return res

        parent_bases = []
        for p in s.parent_ids:
            parent_bases.append(self.resolve_basis(p, item, rule, stack + [sid]))
        chosen = min(
            parent_bases, key=lambda b: (b[2].earliest, b[2].latest, b[0])
        )
        merged = as_range(s.merged_at) if (
            s.kind.value == "composite" and s.merged_at is not None
        ) else None
        res = (
            chosen[0], chosen[1],
            TimeRange(
                earliest=min(b[2].earliest for b in parent_bases),
                latest=min(b[2].latest for b in parent_bases),
            ),
            merged,
        )
        memo[key] = res
        return res

    # ------------------------------------------------- 共享历史收集 ----

    def collect_history(self, sid: str, item: str) -> dict:
        """沿来源链收集该时钟可见的历史。

        穿过合样边界时，只取可能不晚于该合样 ``merged_at`` 的组成样记录
        （区间与墙交叠的记录保留但标记 ``wall_certain=False``）；穿过分样
        边界不过滤（同一段共享历史）。防腐动作另外记录动作来源样品。
        """
        pres: list[HistEvent] = []
        pre: list[HistEvent] = []
        ana: list[HistEvent] = []
        xfer: list[HistEvent] = []
        temps: list = []
        pres_sources: dict[str, list[dict]] = {}
        seen: set[str] = set()
        stack: list[tuple[str, Optional[TimeRange]]] = [(sid, None)]
        walls: list[str] = []
        while stack:
            cur, wall = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            node = self.samples.get(cur)
            if node is None:
                continue

            def status(t: TimeSpec) -> tuple[bool, bool]:
                """(是否可能不晚于合样墙, 是否确定不晚于合样墙)。"""
                if wall is None:
                    return True, True
                r = as_range(t)
                return r.earliest <= wall.latest, r.latest <= wall.earliest

            for a in node.preservation:
                inc, cer = status(a.time)
                if inc:
                    pres.append(HistEvent(a, cur, cer))
                    pres_sources.setdefault(a.name, []).append(
                        {"sample_id": cur, "time": iso_range(as_range(a.time)),
                         "certain": cer}
                    )
            for t in node.temperature:
                inc, _ = status(t.time)
                if inc:
                    temps.append(t)
            for e in node.pretreatments:
                inc, cer = status(e.time)
                if inc:
                    pre.append(HistEvent(e, cur, cer))
            for e in node.analyses:
                inc, cer = status(e.time)
                if inc:
                    ana.append(HistEvent(e, cur, cer))
            for t in node.custody_transfers:
                inc, cer = status(t)
                if inc:
                    xfer.append(HistEvent(t, cur, cer))
            if wall is not None:
                walls.append(f"{cur} 的记录仅取 <= {iso_range(wall)}（合样墙）")

            for p in node.parent_ids:
                if node.kind.value == "composite":
                    w = (as_range(node.merged_at) if node.merged_at is not None
                         else as_range(node.sampling_start))
                    new_wall = w if wall is None else TimeRange(
                        earliest=min(wall.earliest, w.earliest),
                        latest=min(wall.latest, w.latest),
                    )
                else:
                    new_wall = wall
                stack.append((p, new_wall))

        pres.sort(key=lambda h: (h.range().earliest, h.range().latest))
        temps.sort(key=lambda t: t.time)
        pre.sort(key=lambda h: (h.range().earliest, h.range().latest))
        ana.sort(key=lambda h: (h.range().earliest, h.range().latest))
        xfer.sort(key=lambda h: (h.range().earliest, h.range().latest))
        return {
            "preservation": pres, "temperature": temps, "pretreatments": pre,
            "analyses": ana, "transfers": xfer, "wall_notes": walls,
            "pres_sources": pres_sources, "seen": sorted(seen),
        }

    # ----------------------------------------------- 适用条件与来源 ----

    def _resolve_condition(
        self, sid: str, item: str, field: str
    ) -> tuple[Optional[str], str, str]:
        """沿来源链解析样品实际条件（本样品优先，其次祖先）。

        返回 (值, 来源样品id, 来源字段名)；整条链都未提交时值为 None。
        """
        queue = [sid]
        visited: set[str] = set()
        while queue:
            cur = queue.pop(0)
            if cur in visited or cur not in self.samples:
                continue
            visited.add(cur)
            node = self.samples[cur]
            if field == "method":
                val = node.item_methods.get(item)
                src_field = f"item_methods.{item}"
            else:
                val = getattr(node, field)
                src_field = field
            if val is not None:
                return val, cur, src_field
            queue.extend(node.parent_ids)
        return None, sid, (f"item_methods.{item}" if field == "method" else field)

    def _condition_sources(
        self, sid: str, item: str, origin_id: str, basis_kind: str,
        basis_range: TimeRange
    ) -> dict[str, FieldSource]:
        """构建全部匹配维度的字段来源（取值可能为 None）。"""
        sources: dict[str, FieldSource] = {}
        sources["effective_time"] = FieldSource(
            field="basis_time", value=iso_range(basis_range),
            source_sample_id=origin_id,
            source_field=basis_kind,
            note="原始采样时刻（沿来源链解析；连续样按规则 continuous_basis 取端）"
            + ("" if basis_range.exact else "；区间形式提交，按范围传播"),
        )
        for dim in DIMENSIONS:
            val, src_sid, src_field = self._resolve_condition(sid, item, dim)
            sources[dim] = FieldSource(
                field=dim, value=val, source_sample_id=src_sid,
                source_field=src_field,
                note=None if val is not None else "本样品及来源链均未提交，按缺失处理",
            )
        return sources

    def _candidate_view(
        self,
        entry: PoolEntry,
        basis: Optional[tuple],
        sources: dict[str, FieldSource],
    ) -> CandidateRuleView:
        r = entry.rule
        time_match = "in"
        if basis is not None:
            basis_range: TimeRange = basis[2]
            time_match = interval_position(
                basis_range.earliest, basis_range.latest,
                r.effective_from, r.effective_to,
            )
            ctx = {
                "basis_range": (basis_range.earliest, basis_range.latest),
                "matrix": sources["matrix"].value,
                "method": sources["method"].value,
                "container": sources["container"].value,
                "storage_condition": sources["storage_condition"].value,
            }
            raw_unmet = evaluate_applicability(r, ctx)
        else:
            time_match = "out"
            raw_unmet = [{"dimension": "effective_time",
                          "required": [r.effective_from, r.effective_to],
                          "actual": None}]
        unmet = []
        for u in raw_unmet:
            dim = u["dimension"]
            src = sources.get(dim)
            if dim == "effective_time" and u.get("actual_range") is not None:
                actual = iso_range(TimeRange(
                    earliest=u["actual_range"][0], latest=u["actual_range"][1]))
            else:
                actual = (
                    iso(u["actual"]) if dim == "effective_time" else u["actual"]
                )
            unmet.append(UnmetCondition(
                dimension=dim,
                required=[iso(x) for x in u["required"]]
                if dim == "effective_time" else list(u["required"]),
                actual=actual,
                uncertain=bool(u.get("uncertain")),
                field_sources=[src] if src else [],
            ))
        certain_unmet = [u for u in raw_unmet if not u.get("uncertain")]
        return CandidateRuleView(
            version=entry.version, content_hash=entry.content_hash,
            rule_id=r.rule_id, item=r.item, item_name=r.item_name,
            applies=not unmet,
            possible=not certain_unmet,
            time_match=time_match,
            unmet=unmet,
            effective_from=r.effective_from, effective_to=r.effective_to,
            matrices=list(r.matrices), methods=list(r.methods),
            containers=list(r.containers),
            storage_conditions=list(r.storage_conditions),
            analysis_minutes=r.analysis_minutes,
            pretreatment_minutes=r.pretreatment_minutes,
        )

    def _select_rule(
        self, sid: str, item: str, forced: Optional[tuple]
    ) -> dict:
        """规则适用性匹配，返回选择结果包。

        forced = (content_hash|None, version|None, rule_id)：试算强制对照，
        只要求条目存在于候选池，不要求满足适用条件（对照本身就是看不适用规则）。
        """
        entries = list(self.by_item.get(item, []))
        # 同 (content_hash, rule_id) 去重（重复发布的同一规则项）
        dedup: dict[tuple[str, str], PoolEntry] = {}
        for e in entries:
            dedup.setdefault((e.content_hash, e.rule.rule_id), e)
        entries = sorted(dedup.values(),
                         key=lambda e: (e.version, e.rule.rule_id))

        # 先用默认（end）基准解析一次：即使没有候选命中，也要用真实原始采样
        # 时刻展示条件来源；成环/断档时才真正不可解析。
        fallback_basis: Optional[tuple] = None
        try:
            fallback_basis = self.resolve_basis(sid, item, None)
        except (_Cycle, _Break):
            fallback_basis = None

        # 逐条候选按其自身 continuous_basis 解析基准并判适用条件
        views: list[CandidateRuleView] = []
        applying: list[tuple[PoolEntry, tuple]] = []   # 整个基准区间内都适用
        possible: list[tuple[PoolEntry, tuple]] = []   # 区间内存在适用时刻
        resolved: dict[tuple, tuple] = {}  # entry key -> basis
        sources: Optional[dict[str, FieldSource]] = None
        basis_failed = fallback_basis is None
        if fallback_basis is not None:
            ob, bk, bt, _ = fallback_basis
            sources = self._condition_sources(sid, item, ob, bk, bt)
        else:
            s = self.samples[sid]
            sources = self._condition_sources(
                sid, item, sid, "sampling_end", as_range(s.sampling_start)
            )
        for e in entries:
            try:
                b = self.resolve_basis(sid, item, e.rule)
            except (_Cycle, _Break):
                b = None
            if b is not None:
                resolved[(e.content_hash, e.rule.rule_id)] = b
                view = self._candidate_view(e, b, sources)
                if view.applies:
                    applying.append((e, b))
                if view.possible:
                    possible.append((e, b))
            else:
                view = self._candidate_view(e, None, sources)
            views.append(view)

        chosen: Optional[PoolEntry] = None
        chosen_basis: Optional[tuple] = None
        status = "unique"
        if forced is not None:
            f_hash, f_version, f_rule_id = forced
            match = next(
                (e for e in entries
                 if e.rule.rule_id == f_rule_id
                 and (f_hash is None or e.content_hash == f_hash)
                 and (f_version is None or e.version == f_version)),
                None,
            )
            if match is not None:
                key = (match.content_hash, match.rule.rule_id)
                chosen = match
                chosen_basis = resolved.get(key) or fallback_basis
                status = "forced"
            else:
                status = "forced_missing"
        elif len(applying) == 1 and len(possible) == 1:
            chosen, chosen_basis = applying[0]
            status = "unique"
        elif len(possible) == 0:
            status = "none"
        else:
            # 多候选同等适用，或基准区间跨过生效边界导致适用性本身不确定
            status = "ambiguous"

        # 无唯一匹配但基准本身可解析：回退基准用于展示（不用于算截止时刻）
        display_basis = chosen_basis or fallback_basis

        views.sort(key=lambda v: (not v.applies, v.version, v.rule_id))
        return {
            "entries": entries, "views": views, "chosen": chosen,
            "chosen_basis": chosen_basis, "display_basis": display_basis,
            "status": status,
            "sources": sources, "basis_failed": basis_failed,
        }

    # ----------------------------------------------------- 单时钟 ----

    def _phase(
        self,
        phase: str,
        basis_range: TimeRange,
        limit: Optional[float],
        done_range: Optional[TimeRange],
        done_uncertain: bool,
        eval_time: datetime,
        critical_min: float,
        rule_source: str,
        invalid: bool,
    ) -> tuple[PhaseInfo, Optional[PhaseUncertainty]]:
        """构造一个阶段视图；返回 (标量视图, 区间视图)。

        标量字段保持旧语义（精确时刻下与旧引擎完全一致）：截止取区间最早端
        （保守），完成时刻取区间中点（展示），剩余分钟取最坏情形。
        期限结论三值化：整区间未超时 compliant / 整区间越界 overdue /
        跨过期限 indeterminate。
        """
        basis_scalar = _mid(basis_range)
        if limit is None:
            pstatus = (
                PhaseStatus.INVALID if invalid
                else PhaseStatus.COMPLETED if done_range is not None
                else PhaseStatus.PENDING
            )
            info = PhaseInfo(
                phase=phase, limit_minutes=None, deadline=None,
                done_at=_mid(done_range) if done_range is not None else None,
                status=pstatus, basis_time=basis_scalar, rule_source=rule_source,
            )
            unc = PhaseUncertainty(
                phase=phase, deadline=None,
                done_at=_iview(done_range) if done_range is not None else None,
                remaining_minutes=None, assessment=None,
            )
            return info, unc

        deadline_range = _shift(basis_range, limit)
        rem = MinutesInterval(
            earliest=_remaining_minutes(deadline_range.earliest, eval_time),
            latest=_remaining_minutes(deadline_range.latest, eval_time),
        )
        remaining_scalar: Optional[int] = None
        if invalid:
            pstatus, assessment = PhaseStatus.INVALID, None
        elif done_range is not None:
            if done_range.latest <= deadline_range.earliest:
                pstatus, assessment = PhaseStatus.COMPLETED, "compliant"
            elif done_range.earliest > deadline_range.latest:
                pstatus, assessment = PhaseStatus.OVERDUE, "overdue"
            else:
                pstatus, assessment = PhaseStatus.INDETERMINATE, "indeterminate"
        elif done_uncertain:
            # 完成记录跨过判定时刻或合样墙：是否已完成无法确定
            pstatus, assessment = PhaseStatus.INDETERMINATE, "indeterminate"
            remaining_scalar = rem.earliest
        else:
            remaining_scalar = rem.earliest
            if eval_time > deadline_range.latest:
                pstatus, assessment = PhaseStatus.OVERDUE, "overdue"
            elif eval_time <= deadline_range.earliest:
                pstatus = (
                    PhaseStatus.CRITICAL
                    if timedelta(minutes=rem.earliest)
                       <= timedelta(minutes=critical_min)
                    else PhaseStatus.PENDING
                )
                assessment = "compliant"
            else:
                pstatus, assessment = PhaseStatus.INDETERMINATE, "indeterminate"
        info = PhaseInfo(
            phase=phase, limit_minutes=limit,
            deadline=deadline_range.earliest,
            done_at=_mid(done_range) if done_range is not None else None,
            status=pstatus, remaining_minutes=remaining_scalar,
            basis_time=basis_scalar, rule_source=rule_source,
        )
        unc = PhaseUncertainty(
            phase=phase, deadline=_iview(deadline_range),
            done_at=_iview(done_range) if done_range is not None else None,
            remaining_minutes=None if pstatus in (
                PhaseStatus.COMPLETED, PhaseStatus.INVALID,
            ) else rem,
            assessment=assessment,
        )
        return info, unc

    def build_clock(self, sid: str, item: str) -> tuple[ClockResult, list[dict]]:
        """返回时钟与原始违规记录列表。"""
        s = self.samples[sid]
        req = self.req
        raw_viol: list[dict] = []
        deriv: list[DerivationStep] = []

        forced = None
        sel = req.selected_candidates.get(f"{sid}/{item}") \
            if req.selected_candidates else None
        if sel is not None:
            forced = (sel.content_hash, sel.version, sel.rule_id)

        selection = self._select_rule(sid, item, forced)
        rule = selection["chosen"].rule if selection["chosen"] else None
        basis_tuple = selection["chosen_basis"]
        display_basis = selection["display_basis"]
        match_status = selection["status"]
        sources = selection["sources"]
        forced_missing = match_status == "forced_missing"

        origin_id = basis_kind = None
        basis_range: Optional[TimeRange] = None
        merged_range: Optional[TimeRange] = None
        if display_basis is not None:
            origin_id, basis_kind, basis_range, merged_range = display_basis
            deriv.append(DerivationStep(
                step="resolve_basis",
                detail=(
                    f"{sid}/{item} 基准来自 {origin_id} 的 {basis_kind}="
                    f"{iso_range(basis_range)}" + (
                        f"；合样墙 merged_at={iso_range(merged_range)}"
                        if merged_range else "")
                    + ("（仅用于展示，未参与截止计算）" if basis_tuple is None else "")
                ),
            ))

        # 结构问题（成环/断档/确定的时间倒置）：基准不可解析或时序非法。
        # 区间冲突（无法确定次序）不置 INVALID，而是把时钟判为 indeterminate。
        # 注意：判定时刻之后的“未来记录”只逐时钟记 time_inversion 违规，
        # 不进入 inv_tainted，因此不会把时钟整体置为失效（与旧行为一致）。
        structural_invalid = (
            sid in self.cycle_tainted or sid in self.break_tainted
            or sid in self.inv_tainted
        )
        clock_conflicted = sid in self.conflict_tainted
        if basis_range is None:
            if sid in self.cycle_tainted:
                raw_viol.append(("source_cycle", sid, [item],
                                 f"样品 {sid} 的来源链成环，基准不可解析",
                                 {"node": sid}))
            elif sid in self.break_tainted:
                raw_viol.append(("source_break", sid, [item],
                                 f"样品 {sid} 的来源不在本批记录中（来源断档），基准不可解析",
                                 {}))

        # 适用性匹配推导（候选/未满足条件/字段来源同时进入响应模型）
        if rule is not None:
            label = {"forced": "试算强制指定", "unique": "唯一匹配"}.get(
                match_status, match_status)
            deriv.append(DerivationStep(
                step="match_rule",
                detail=(
                    f"候选 {len(selection['entries'])} 条，按原始采样时刻 "
                    f"{iso_range(basis_range)} 与实际条件筛选 -> {label} "
                    f"{selection['chosen'].version}#{rule.rule_id} "
                    f"(hash={selection['chosen'].content_hash[:12]})"
                ),
            ))
        else:
            uncertain_hint = ""
            if match_status == "ambiguous" and any(
                v.time_match == "straddle" for v in selection["views"]
            ):
                uncertain_hint = "；基准区间跨过候选生效边界，适用性无法确定"
            deriv.append(DerivationStep(
                step="match_rule",
                detail=(
                    f"候选 {len(selection['entries'])} 条，无唯一适用规则"
                    f"（{match_status}）{uncertain_hint}；"
                    "不计算截止时刻、不形成合规结论"
                ),
            ))

        hist = self.collect_history(sid, item)
        deriv.append(DerivationStep(
            step="collect_history",
            detail=(
                f"可见样品链 {hist['seen']}；温度点 {len(hist['temperature'])} 条，"
                f"防腐 {len(hist['preservation'])} 次，前处理 "
                f"{len(hist['pretreatments'])} 次，交接 {len(hist['transfers'])} 次"
                + ("；" + "；".join(hist["wall_notes"]) if hist["wall_notes"] else "")
            ),
        ))

        # 无/多候选/强制缺失：规则适用性违规（携带候选与字段来源），不产出截止时间
        if rule is None and not (sid in self.inv_tainted and basis_range is None):
            if match_status == "ambiguous":
                msg = f"样品 {sid} 的项目 {item} 存在多个同等适用的规则，无法唯一确定限值"
            elif match_status == "forced_missing":
                msg = (f"样品 {sid} 的项目 {item} 试算强制指定的规则项 "
                       f"{forced[2]} 不在候选池中")
            else:
                msg = f"样品 {sid} 的项目 {item} 没有满足生效区间与适用条件的规则"
            raw_viol.append((
                "rule_applicability", sid, [item], msg,
                {
                    "match_status": match_status,
                    "candidate_count": len(selection["entries"]),
                    "candidates": [c.model_dump(mode="json") for c in selection["views"]],
                    "field_sources": [fs.model_dump(mode="json")
                                      for fs in (sources or {}).values()],
                },
            ))

        if sid in self.inv_tainted:
            for rec in self._inversion_records(sid):
                raw_viol.append(("time_inversion", sid, [item], rec["message"],
                                 rec["detail"]))
        if clock_conflicted:
            for rec in self._conflict_records(sid):
                raw_viol.append(("time_uncertainty_conflict", sid, [item],
                                 rec["message"], rec["detail"]))

        # 判定时刻之后才“发生”的事件属于未来记录：记 time_inversion，
        # 列出受影响项目与完整推导；这些事件不得据此结束任何阶段，也不参与交接判定。
        # 区间跨过判定时刻的事件是否已发生无法确定：不结束阶段，只作不确定传播。
        ignored_future: list[str] = []
        uncertain_events: list[str] = []

        def classify(when: TimeSpec) -> str:
            r = as_range(when)
            if r.earliest > req.eval_time:
                return "future"
            if r.latest <= req.eval_time:
                return "past"
            return "straddle"

        def future_violation(kind: str, label: str, when: TimeSpec,
                             phase_hint: str = "") -> None:
            r = as_range(when)
            ignored_future.append(f"{label}@{iso_range(r)}")
            raw_viol.append((
                "time_inversion", sid, [item],
                f"样品 {sid} 项目 {item} 的{label}时刻 {iso_range(r)} 晚于判定时刻 "
                f"{iso(req.eval_time)}（事件尚未发生{phase_hint}）",
                {"event_type": kind, "event_time": iso_range(r),
                 "eval_time": iso(req.eval_time)},
            ))

        def uncertain_note(kind: str, label: str, when: TimeSpec) -> None:
            r = as_range(when)
            uncertain_events.append(f"{label}@{iso_range(r)}")

        for h in hist["preservation"]:
            cls = classify(h.event.time)
            if cls == "future":
                future_violation("preservation", f"防腐动作 {h.event.name}",
                                 h.event.time)
            elif cls == "straddle":
                uncertain_note("preservation", f"防腐动作 {h.event.name}",
                               h.event.time)
        for h in hist["pretreatments"]:
            e = h.event
            if not e.items or item in e.items:
                cls = classify(e.time)
                if cls == "future":
                    future_violation("pretreatment", f"前处理事件 {e.type}",
                                     e.time, "，不得据此结束预处理阶段")
                elif cls == "straddle":
                    uncertain_note("pretreatment", f"前处理事件 {e.type}", e.time)
        for h in hist["analyses"]:
            e = h.event
            if e.item == item:
                cls = classify(e.time)
                if cls == "future":
                    future_violation("analysis", f"分析事件({item})", e.time,
                                     "，不得据此结束分析阶段")
                elif cls == "straddle":
                    uncertain_note("analysis", f"分析事件({item})", e.time)
        for p in hist["temperature"]:
            if p.time > req.eval_time:
                future_violation("temperature",
                                 f"温度记录 {p.temp_c}℃", p.time)
        for h in hist["transfers"]:
            cls = classify(h.event)
            if cls == "future":
                future_violation("custody_transfer", "交接", h.event,
                                 "，不计入已完成交接")
            elif cls == "straddle":
                uncertain_note("custody_transfer", "交接", h.event)
        if ignored_future:
            deriv.append(DerivationStep(
                step="ignore_future_events",
                detail=(
                    f"{len(ignored_future)} 条记录晚于判定时刻 {iso(req.eval_time)}，"
                    f"已识别为 time_inversion 且不结束任何阶段：{ignored_future}"
                ),
            ))
        if uncertain_events:
            deriv.append(DerivationStep(
                step="uncertain_events",
                detail=(
                    f"{len(uncertain_events)} 条记录的时刻区间跨过判定时刻 "
                    f"{iso(req.eval_time)}，是否已发生无法确定，按不确定传播："
                    f"{uncertain_events}"
                ),
            ))

        deadline_pre_range = deadline_ana_range = None
        pre_done_range = ana_done_range = None
        pre_done_uncertain = ana_done_uncertain = False
        transfer_late_uncertain = False
        missing_preservation = False
        rule_source = (
            f"rule:{selection['chosen'].version}#{rule.rule_id}(item={item})"
            if rule is not None else f"unmatched#item={item}({match_status})"
        )

        # 仅唯一/强制匹配的规则参与截止时刻计算；display_basis 只用于展示
        rule_selected = rule is not None and basis_tuple is not None
        if rule_selected:
            if rule.pretreatment_minutes is not None:
                deadline_pre_range = _shift(basis_range, rule.pretreatment_minutes)
            deadline_ana_range = _shift(basis_range, rule.analysis_minutes)
            deriv.append(DerivationStep(
                step="deadlines",
                detail=(
                    f"预处理截止 {iso_range(deadline_pre_range)}（基准 + "
                    f"{rule.pretreatment_minutes} 分钟）；"
                    f"分析截止 {iso_range(deadline_ana_range)}（基准 + "
                    f"{rule.analysis_minutes} 分钟，前处理不清零）"
                ),
            ))

            def done_candidates(events: list[HistEvent],
                                matches) -> tuple[list[HistEvent],
                                                  list[HistEvent]]:
                """(确定已完成, 可能已完成)：确定 = 整区间不晚于判定时刻且
                确定在合样墙之前；可能 = 区间与判定时刻/合样墙交叠。"""
                certain, maybe = [], []
                for h in events:
                    if not matches(h.event):
                        continue
                    cls = classify(h.event.time)
                    if cls == "future":
                        continue
                    if cls == "past" and h.wall_certain:
                        certain.append(h)
                    else:
                        maybe.append(h)
                return certain, maybe

            pre_certain, pre_maybe = done_candidates(
                hist["pretreatments"],
                lambda e: not e.items or item in e.items)
            ana_certain, ana_maybe = done_candidates(
                hist["analyses"], lambda e: e.item == item)

            def done_range_of(certain: list[HistEvent],
                              maybe: list[HistEvent]) -> Optional[TimeRange]:
                if not certain:
                    return None
                both = certain + maybe
                lo = min(h.range().earliest for h in both)
                hi = max(h.range().latest for h in both)
                return TimeRange(earliest=lo, latest=hi)

            pre_done_range = done_range_of(pre_certain, pre_maybe)
            pre_done_uncertain = pre_done_range is None and bool(pre_maybe)
            ana_done_range = done_range_of(ana_certain, ana_maybe)
            ana_done_uncertain = ana_done_range is None and bool(ana_maybe)

            # 防腐动作（同样只计确定已发生的）；动作来源样品随违规返回
            have = {
                h.event.name for h in hist["preservation"]
                if h.wall_certain and classify(h.event.time) == "past"
            }
            uncertain_have = {
                h.event.name for h in hist["preservation"]
                if classify(h.event.time) != "future"
            } - have
            missing = [p for p in rule.required_preservation if p not in have]
            if missing:
                missing_preservation = True
                raw_viol.append((
                    "missing_preservation", sid, [item],
                    f"样品 {sid} 的项目 {item} 保存动作不足：缺少 {missing}",
                    {
                        "required": rule.required_preservation,
                        "performed": sorted(have),
                        "uncertain": sorted(uncertain_have),
                        "performed_sources": [
                            {"name": name, "records": recs}
                            for name, recs in sorted(hist["pres_sources"].items())
                        ],
                        "field_sources": [
                            {"field": "preservation",
                             "source_sample_id": n["sample_id"],
                             "source_field": "preservation",
                             "value": name}
                            for name, recs in sorted(hist["pres_sources"].items())
                            for n in recs
                        ],
                    },
                ))

            # 交接晚于截止时刻（未来交接不参与；区间跨过截止则无法确定）
            certain_xfer = [
                h for h in hist["transfers"]
                if h.wall_certain and classify(h.event) == "past"
            ]
            maybe_xfer = [
                h for h in hist["transfers"]
                if classify(h.event) != "future" and h not in certain_xfer
            ]
            if certain_xfer:
                latest_xfer = max(
                    certain_xfer,
                    key=lambda h: (h.range().latest, h.range().earliest),
                ).range()
                late_against = None
                for phase_name, dr in (("analysis", deadline_ana_range),
                                       ("pretreatment", deadline_pre_range)):
                    if dr is None:
                        continue
                    if latest_xfer.earliest > dr.latest:
                        late_against = (phase_name, dr)
                        break
                    if latest_xfer.latest > dr.earliest:
                        transfer_late_uncertain = True
                        deriv.append(DerivationStep(
                            step="transfer_uncertain",
                            detail=(
                                f"最近交接区间 {iso_range(latest_xfer)} 跨过"
                                f"{phase_name}截止 {iso_range(dr)}，"
                                "是否晚交接无法确定"
                            ),
                        ))
                        break
                if late_against:
                    raw_viol.append((
                        "late_transfer", sid, [item],
                        f"样品 {sid} 项目 {item} 最近交接 "
                        f"{iso_range(latest_xfer)} 晚于{late_against[0]}截止 "
                        f"{iso_range(late_against[1])}",
                        {"transfer_at": iso_range(latest_xfer),
                         "pretreatment_deadline": iso_range(deadline_pre_range),
                         "analysis_deadline": iso_range(deadline_ana_range),
                         "late_against": late_against[0],
                         "minutes_late": _remaining_minutes(
                             latest_xfer.earliest, late_against[1].latest)},
                    ))
            if maybe_xfer and not transfer_late_uncertain:
                for h in maybe_xfer:
                    r = h.range()
                    for dr in (deadline_ana_range, deadline_pre_range):
                        if dr is not None and r.latest > dr.earliest:
                            transfer_late_uncertain = True
                            deriv.append(DerivationStep(
                                step="transfer_uncertain",
                                detail=(
                                    f"交接记录区间 {iso_range(r)} 跨过判定时刻"
                                    "或合样墙，且可能晚于截止 "
                                    f"{iso_range(dr)}，是否晚交接无法确定"
                                ),
                            ))
                            break
                    if transfer_late_uncertain:
                        break

        phases: list[PhaseInfo] = []
        phase_uncs: list[PhaseUncertainty] = []
        if rule_selected:
            for ph, limit, done_r, done_u in (
                ("pretreatment", rule.pretreatment_minutes,
                 pre_done_range, pre_done_uncertain),
                ("analysis", rule.analysis_minutes,
                 ana_done_range, ana_done_uncertain),
            ):
                info, unc = self._phase(
                    ph, basis_range, limit, done_r, done_u, req.eval_time,
                    req.critical_within_minutes, rule_source,
                    sid in self.inv_tainted,
                )
                phases.append(info)
                phase_uncs.append(unc)
            self._temperature_checks(
                sid, item, rule, hist["temperature"], basis_range.earliest,
                req.eval_time, raw_viol,
            )

        # 时钟汇总状态
        phase_uncertain = any(
            u.assessment == "indeterminate" for u in phase_uncs
        )
        overdue_definite = any(p.status == PhaseStatus.OVERDUE for p in phases)
        if structural_invalid or basis_tuple is None and basis_range is None:
            status = ClockStatus.INVALID
        elif clock_conflicted:
            status = ClockStatus.INDETERMINATE  # 区间冲突：无法确定
        elif not rule_selected:
            status = ClockStatus.INDETERMINATE  # none / ambiguous / forced_missing
        elif overdue_definite:
            status = ClockStatus.OVERDUE
        elif missing_preservation:
            status = ClockStatus.INDETERMINATE  # 保存动作不足：保留期限信息但不下结论
        elif phase_uncertain or transfer_late_uncertain:
            status = ClockStatus.INDETERMINATE  # 区间跨过期限：无法确定
        elif ana_done_range is not None and all(
            p.status == PhaseStatus.COMPLETED
            for p in phases
            if p.limit_minutes is not None
        ):
            status = ClockStatus.COMPLETED
        elif any(p.status == PhaseStatus.CRITICAL for p in phases):
            status = ClockStatus.CRITICAL
        else:
            status = ClockStatus.OK

        next_idx = next(
            (i for i, p in enumerate(phases)
             if p.limit_minutes is not None and p.status
             in (PhaseStatus.PENDING, PhaseStatus.CRITICAL,
                 PhaseStatus.OVERDUE, PhaseStatus.INDETERMINATE)),
            None,
        )
        next_phase = phases[next_idx] if next_idx is not None else None
        next_action = next_phase.phase if next_phase else None  # type: ignore[arg-type]
        next_deadline = next_phase.deadline if next_phase else None
        remaining = (
            _remaining_minutes(next_deadline, req.eval_time)
            if next_deadline is not None else None
        )
        remaining_interval = (
            phase_uncs[next_idx].remaining_minutes
            if next_idx is not None else None
        )
        item_violations = [v for v in raw_viol if v[2] == [item]]

        # 能否形成合规结论：结构失效 / 区间冲突 / 无唯一匹配 / 试算强制 /
        # 保存动作不足 / 期限结论不确定 -> 否。确定超时的时钟即使晚交接
        # 无法确定，其超时结论本身仍然成立（conclusive）。
        conclusive = (
            rule_selected
            and match_status == "unique"
            and not missing_preservation
            and not structural_invalid
            and not clock_conflicted
            and not phase_uncertain
            and not (transfer_late_uncertain and not overdue_definite)
        )
        conforming = (
            conclusive
            and status not in (ClockStatus.OVERDUE,)
            and not item_violations
        )

        matched_ref = None
        if rule is not None and selection["chosen"] is not None:
            matched_ref = ClockRuleRef(
                version=selection["chosen"].version,
                content_hash=selection["chosen"].content_hash,
                rule_id=rule.rule_id, item=item,
            )
        match_context = None
        if sources is not None:
            match_context = MatchContext(
                basis_time=_mid(basis_range) if basis_range is not None
                else _mid(as_range(s.sampling_start)),
                field_sources=list(sources.values()),
            )

        uncertainty = self._build_uncertainty(
            sid, hist, basis_range, phases, phase_uncs,
            remaining_interval, status,
        )
        if uncertainty is not None:
            deriv.append(DerivationStep(
                step="uncertainty_propagation",
                detail=(
                    f"时间区间沿来源链传播：基准 {iso_range(basis_range)}，"
                    f"期限结论 {uncertainty.assessment}，区间输入 "
                    f"{len(uncertainty.sources)} 处"
                ),
            ))

        clock = ClockResult(
            sample_id=sid, item=item,
            origin_sample_id=origin_id or sid,
            basis=basis_kind or "sampling_end",
            basis_time=_mid(basis_range) if basis_range is not None
            else _mid(as_range(s.sampling_start)),
            merged_at=_mid(merged_range) if merged_range is not None else None,
            status=status, conforming=conforming, conclusive=conclusive,
            phases=phases,
            next_action_deadline=next_deadline,
            next_action=next_action,
            remaining_minutes=remaining,
            latest_operation_at=(
                deadline_ana_range.earliest
                if deadline_ana_range is not None else None
            ),
            rule_source=rule_source,
            matched_rule=matched_ref,
            match_status=match_status,
            candidates=selection["views"],
            match_context=match_context,
            uncertainty=uncertainty,
            derivation=deriv,
        )
        return clock, raw_viol

    def _build_uncertainty(
        self,
        sid: str,
        hist: dict,
        basis_range: Optional[TimeRange],
        phases: list[PhaseInfo],
        phase_uncs: list[PhaseUncertainty],
        remaining_interval: Optional[MinutesInterval],
        status: ClockStatus,
    ) -> Optional[ClockUncertainty]:
        """汇总该时钟的时间不确定性；来源链上无区间输入时返回 None。"""
        out: list[UncertaintySource] = []
        seen_keys: set = set()

        def add(src_sid: str, field: str, r: TimeRange,
                label: Optional[str] = None) -> None:
            if r.exact:
                return
            key = (src_sid, field, r.earliest, r.latest, label)
            if key in seen_keys:
                return
            seen_keys.add(key)
            out.append(UncertaintySource(
                sample_id=src_sid, field=field,
                earliest=r.earliest, latest=r.latest, label=label,
            ))

        for sid2 in hist["seen"]:
            node = self.samples.get(sid2)
            if node is None:
                continue
            add(sid2, "sampling_start", as_range(node.sampling_start))
            add(sid2, "sampling_end", as_range(node.sampling_end))
            if node.merged_at is not None:
                add(sid2, "merged_at", as_range(node.merged_at))
        for h in hist["preservation"]:
            add(h.source, "preservation", h.range(),
                getattr(h.event, "name", None))
        for h in hist["pretreatments"]:
            add(h.source, "pretreatment", h.range(),
                getattr(h.event, "type", None))
        for h in hist["analyses"]:
            add(h.source, "analysis", h.range(), getattr(h.event, "item", None))
        for h in hist["transfers"]:
            add(h.source, "custody_transfer", h.range())

        if not out:
            return None
        if status == ClockStatus.OVERDUE:
            assessment = "overdue"
        elif status in (ClockStatus.INDETERMINATE, ClockStatus.INVALID):
            assessment = "indeterminate"
        else:
            assessment = "compliant"
        return ClockUncertainty(
            basis_time=_iview(basis_range) if basis_range is not None else None,
            phases=[u for u in phase_uncs if u.deadline is not None
                    or u.done_at is not None],
            remaining_minutes=remaining_interval,
            assessment=assessment,
            sources=out,
        )

    def _conflict_records(self, sid: str) -> list[dict]:
        out = list(self.conflicts.get(sid, []))
        # 祖先冲突同样污染该时钟：找出链路上的祖先
        seen: set[str] = set()
        stack = [sid]
        while stack:
            cur = stack.pop()
            node = self.samples.get(cur)
            if not node:
                continue
            for p in node.parent_ids:
                if p in seen:
                    continue
                seen.add(p)
                out.extend(self.conflicts.get(p, []))
                stack.append(p)
        return out

    def _inversion_records(self, sid: str) -> list[dict]:
        out = list(self.inversions.get(sid, []))
        # 祖先倒置同样污染该时钟：找出链路上的祖先
        seen: set[str] = set()
        stack = [sid]
        while stack:
            cur = stack.pop()
            node = self.samples.get(cur)
            if not node:
                continue
            for p in node.parent_ids:
                if p in seen:
                    continue
                seen.add(p)
                out.extend(self.inversions.get(p, []))
                stack.append(p)
        return out

    def _temperature_checks(
        self, sid, item, rule, points, basis_time, eval_time, raw_viol
    ) -> None:
        tol = timedelta(minutes=GAP_TOLERANCE_MIN)
        # 仅采纳判定时刻之前（含）的观测点参与越界/断档
        points = [p for p in points if p.time <= eval_time]
        excursions: list[dict] = []
        seg: Optional[dict] = None
        for pt in points:
            if pt.temp_c < rule.min_temp_c or pt.temp_c > rule.max_temp_c:
                if seg is None:
                    seg = {"start": pt.time, "end": pt.time,
                           "min": pt.temp_c, "max": pt.temp_c}
                else:
                    seg["end"] = pt.time
                    seg["min"] = min(seg["min"], pt.temp_c)
                    seg["max"] = max(seg["max"], pt.temp_c)
            elif seg is not None:
                excursions.append(seg)
                seg = None
        if seg is not None:
            excursions.append(seg)
        if excursions:
            raw_viol.append((
                "temperature_excursion", sid, [item],
                f"样品 {sid} 项目 {item} 温度超出保存区间 "
                f"[{rule.min_temp_c}, {rule.max_temp_c}]℃",
                {"allowed": [rule.min_temp_c, rule.max_temp_c],
                 "segments": [
                     {"from": iso(x["start"]), "to": iso(x["end"]),
                      "observed_min": x["min"], "observed_max": x["max"]}
                     for x in excursions
                 ]},
            ))

        gaps: list[dict] = []
        in_window = [p for p in points if basis_time <= p.time <= eval_time]

        def gap_if(start: datetime, end: datetime) -> Optional[dict]:
            delta = end - start
            if delta > tol:
                return {"start": iso(start), "end": iso(end),
                        "minutes": int(round(delta.total_seconds() / 60))}
            return None

        if in_window:
            first = in_window[0]
            if g := gap_if(basis_time, first.time):
                gaps.append(g)
            for a, b in zip(in_window, in_window[1:]):
                if g := gap_if(a.time, b.time):
                    gaps.append(g)
            last = in_window[-1]
            if g := gap_if(last.time, eval_time):
                gaps.append(g)
        elif eval_time > basis_time:
            if g := gap_if(basis_time, eval_time):
                gaps.append(g)
        if gaps:
            raw_viol.append((
                "temperature_gap", sid, [item],
                f"样品 {sid} 项目 {item} 温度记录断档（容差 {GAP_TOLERANCE_MIN:.0f} 分钟）",
                {"tolerance_minutes": GAP_TOLERANCE_MIN, "intervals": gaps},
            ))


class _Cycle(Exception):
    def __init__(self, sid: str):
        self.sid = sid


class _Break(Exception):
    def __init__(self, sid: str):
        self.sid = sid


# ---------------------------------------------------------------- 入口 ----

def evaluate(
    *,
    request: JudgmentRequest,
    pool: list[PoolEntry],
    rule_sets: dict[str, RuleSet],
    package_id: str,
    version_no: int,
    trial: bool,
    created_at: datetime,
    changes_from: Optional[str] = None,
    changes: Optional[list[StatusChange]] = None,
    sample_id: Optional[str] = None,
) -> JudgmentResult:
    eng = _Engine(request, pool)
    clocks: list[ClockResult] = []
    raw: list[tuple] = []
    for s in request.samples:
        for item in s.items:
            clock, rv = eng.build_clock(s.id, item)
            clocks.append(clock)
            raw.extend(rv)

    # 按 (code, sample) 聚合：跨项目同类问题合并为一条，items 为全部受影响项目，
    # 各条消息保留在 detail.occurrences 中以便完整推导。
    grouped: dict[tuple, dict] = {}
    for code, sid, items, message, detail in raw:
        g = grouped.setdefault((code, sid), {
            "code": code, "sample_id": sid, "items": set(),
            "messages": [], "occurrences": [],
        })
        g["items"].update(items)
        if message not in g["messages"]:
            g["messages"].append(message)
        g["occurrences"].append({"items": items, "message": message, **detail})
    violations = []
    for g in grouped.values():
        message = (
            g["messages"][0]
            if len(g["messages"]) == 1
            else "；".join(g["messages"])
        )
        detail_out = (
            g["occurrences"][0]
            if len(g["occurrences"]) == 1
            else {"occurrences": g["occurrences"]}
        )
        violations.append(Violation(
            code=g["code"], sample_id=g["sample_id"],
            items=sorted(g["items"]), message=message,
            detail=detail_out,
            derivation=[DerivationStep(
                step="affected_items",
                detail=(
                    f"该问题沿来源链影响样品 {g['sample_id']} 的 "
                    f"{sorted(g['items'])} 项目时钟"
                ),
            )],
        ))
    violations.sort(key=lambda v: (v.sample_id, v.code))

    # 应优先收样的批次：仍有待办阶段且可形成结论的时钟，按截止时刻升序合并
    pending = [
        c for c in clocks
        if c.status in (ClockStatus.OVERDUE, ClockStatus.CRITICAL, ClockStatus.OK)
        and c.next_action_deadline is not None and c.conclusive
    ]
    by_sample: dict[str, list[ClockResult]] = {}
    for c in pending:
        by_sample.setdefault(c.sample_id, []).append(c)
    priority: list[PriorityBatch] = []
    for sid, cs in by_sample.items():
        earliest = min(cs, key=lambda c: c.next_action_deadline)
        due = earliest.next_action_deadline
        pending_items = sorted({c.item for c in cs})
        reason = (
            f"最紧迫: {earliest.next_action} 截止 {iso(due)}，剩余 "
            f"{_remaining_minutes(due, request.eval_time)} 分钟；"
            f"待办项目 {pending_items}"
        )
        priority.append(PriorityBatch(
            sample_id=sid, items=pending_items, due_at=due,
            due_phase=earliest.next_action,  # type: ignore[arg-type]
            remaining_minutes=_remaining_minutes(due, request.eval_time),
            reason=reason,
        ))
    priority.sort(key=lambda b: b.due_at)

    summary = {
        "total_clocks": len(clocks),
        "conforming": sum(1 for c in clocks if c.conforming),
        "overdue": sum(1 for c in clocks if c.status == ClockStatus.OVERDUE),
        "critical": sum(1 for c in clocks if c.status == ClockStatus.CRITICAL),
        "indeterminate": sum(
            1 for c in clocks if c.status == ClockStatus.INDETERMINATE
        ),
        "non_conclusive": sum(1 for c in clocks if not c.conclusive),
        "invalid": sum(1 for c in clocks if c.status == ClockStatus.INVALID),
        "uncertain": sum(1 for c in clocks if c.uncertainty is not None),
        "violations": len(violations),
        "priority_batch_count": len(priority),
    }

    # 各时钟冻结的规则集（按 content_hash 去重，保持稳定顺序）
    frozen_hashes: list[str] = []
    for c in clocks:
        if c.matched_rule and c.matched_rule.content_hash not in frozen_hashes:
            frozen_hashes.append(c.matched_rule.content_hash)
    rules_ref: list[RuleRef] = []
    for h in frozen_hashes:
        rs = rule_sets.get(h)
        rules_ref.append(RuleRef(
            version=rs.version if rs else next(
                (c.matched_rule.version for c in clocks
                 if c.matched_rule and c.matched_rule.content_hash == h), ""),
            content_hash=h,
            name=rs.name if rs else "environmental-deadline-rules",
            item_count=len(rs.items) if rs else 0,
        ))
    representative = rules_ref[0] if rules_ref else RuleRef(
        version="(unmatched)", content_hash="-",
        name="environmental-deadline-rules", item_count=0,
    )

    per_clock_basis = {
        f"{c.sample_id}/{c.item}": {
            "matched": None if c.matched_rule is None
            else {
                "version": c.matched_rule.version,
                "rule_id": c.matched_rule.rule_id,
                "content_hash": c.matched_rule.content_hash,
            },
            "match_status": c.match_status,
        }
        for c in clocks
    }
    matching_policy = {
        "effective_interval": "half-open [effective_from, effective_to)",
        "dimensions": ["matrix", "method", "container", "storage_condition"],
        "dimension_wildcard": "空列表表示该维度通配",
        "basis_time": "沿来源链解析的原始采样时刻（合样取最早组成样）",
        "basis_time_range": "基准为区间时：整区间在生效区间内=确定适用(in)，"
        "整区间在外=不适用(out)，跨过边界=straddle（适用性不确定，按多候选处理）",
        "method_source": "sample.item_methods[item]",
        "condition_source": "本样品优先；未提交时沿来源链回退到祖先样品",
        "forced_selection": bool(request.selected_candidates),
    }
    basis_policy = {
        "continuous_default": ContinuousBasis.END.value,
        "time_uncertainty": "采样起止/防腐/前处理/分析/交接可提交 "
        "{earliest, latest} 区间；基准、截止、剩余分钟按区间传播："
        "整区间未超时=compliant，整区间越界=overdue，跨过期限=indeterminate；"
        "区间冲突（互不可能/合样边界无有效交集/次序无法确定）"
        "以 time_uncertainty_conflict 违规返回来源",
        "per_clock": per_clock_basis,
    }

    return JudgmentResult(
        package_id=package_id,
        sample_id=sample_id,
        version_no=version_no,
        trial=trial,
        request_id=request.request_id,
        eval_time=request.eval_time,
        created_at=created_at,
        rule=representative,
        rules=rules_ref,
        selection_policy=matching_policy,
        basis_policy=basis_policy,
        summary=summary,
        clocks=clocks,
        violations=violations,
        priority_batches=priority,
        changes_from=changes_from,
        changes=changes or [],
    )
