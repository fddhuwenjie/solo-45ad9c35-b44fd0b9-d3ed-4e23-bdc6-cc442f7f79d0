"""时限判定引擎。

为每个“样品—项目”建立独立时钟：

* 瞬时样基准为采样时刻；连续样默认按采样结束（规则可改为开始）。
* 分样（aliquot）继承母体基准与全部历史；合样（composite）取组成样中最早的
  基准，``merged_at`` 之前的历史共享、之后独立——分析期限绝不会被重置。
* 前处理事件只结束“预处理”阶段；“分析”期限始终从原始基准连续计算。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from .models import (
    ClockResult,
    ClockStatus,
    ContinuousBasis,
    DerivationStep,
    JudgmentRequest,
    JudgmentResult,
    PhaseInfo,
    PhaseStatus,
    PriorityBatch,
    RuleRef,
    RuleSet,
    Sample,
    StatusChange,
    Violation,
)

GAP_TOLERANCE_MIN = 15.0  # 温度记录断档容差（分钟）


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _remaining_minutes(deadline: datetime, eval_time: datetime) -> int:
    return int(round((deadline - eval_time).total_seconds() / 60.0))


class _Engine:
    def __init__(self, request: JudgmentRequest, rule_set: RuleSet):
        self.req = request
        self.rules = {r.item: r for r in rule_set.items}
        self.samples: dict[str, Sample] = {s.id: s for s in request.samples}
        self.children: dict[str, list[str]] = {sid: [] for sid in self.samples}
        for s in request.samples:
            for p in s.parent_ids:
                self.children.setdefault(p, []).append(s.id)

        self.inversions: dict[str, list[dict]] = {}  # sample_id -> 原始倒置记录
        self._detect_inversions()
        self.inv_tainted = self._downward(set(self.inversions))

        self.cycle_tainted = self._cycle_nodes()
        self.break_nodes: set[str] = set()
        for s in request.samples:
            for p in s.parent_ids:
                if p not in self.samples:
                    self.break_nodes.add(s.id)
        self.break_tainted = self._downward(set(self.break_nodes))

        # 基准解析记忆：sid -> {item: (origin_id, basis_kind, time, merged_at)}
        self._basis_memo: dict[str, dict[str, tuple]] = {}

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
        for s in self.req.samples:
            if self.req.eval_time < s.sampling_start:
                self._add_inversion(
                    s.id, "eval_before_sampling",
                    f"样品 {s.id} 判定时刻早于采样开始",
                    {"eval_time": iso(self.req.eval_time),
                     "sampling_start": iso(s.sampling_start)},
                )
            if s.sampling_end < s.sampling_start:
                self._add_inversion(
                    s.id,
                    "sampling_interval_reversed",
                    f"样品 {s.id} 采样结束早于采样开始",
                    {"sampling_start": iso(s.sampling_start),
                     "sampling_end": iso(s.sampling_end)},
                )
            if s.kind.value == "composite":
                wall = s.merged_at or s.sampling_start
                if s.merged_at is not None and s.merged_at < s.sampling_start:
                    self._add_inversion(
                        s.id, "merge_before_sampling",
                        f"样品 {s.id} 合样时刻早于其采样开始",
                        {"merged_at": iso(s.merged_at),
                         "sampling_start": iso(s.sampling_start)},
                    )
                if wall < s.sampling_end:
                    self._add_inversion(
                        s.id, "merge_before_sampling_end",
                        f"样品 {s.id} 合样时刻早于采样结束",
                        {"merged_at": iso(wall), "sampling_end": iso(s.sampling_end)},
                    )
            if s.kind.value == "aliquot":
                for pid in s.parent_ids:
                    p = self.samples.get(pid)
                    if p and s.sampling_start < p.sampling_end:
                        self._add_inversion(
                            s.id, "split_before_parent_ready",
                            f"分样 {s.id} 分装时刻早于母体 {pid} 采样结束",
                            {"split_at": iso(s.sampling_start),
                             "parent_sampling_end": iso(p.sampling_end)},
                        )
            labels = [
                ("preservation", [a.time for a in s.preservation]),
                ("pretreatments", [e.time for e in s.pretreatments]),
                ("analyses", [e.time for e in s.analyses]),
                ("temperature", [t.time for t in s.temperature]),
                ("custody_transfers", list(s.custody_transfers)),
            ]
            for name, times in labels:
                if len(times) >= 2 and sorted(times) != times:
                    self._add_inversion(
                        s.id, f"{name}_not_monotonic",
                        f"样品 {s.id} 的 {name} 时间序列存在倒置",
                        {"times": [iso(t) for t in times]},
                    )
                for t in times:
                    if t < s.sampling_start:
                        self._add_inversion(
                            s.id, f"{name}_before_sampling",
                            f"样品 {s.id} 的 {name} 记录早于采样开始",
                            {"event_time": iso(t),
                             "sampling_start": iso(s.sampling_start)},
                        )

    # ------------------------------------------------------- 基准 ----

    def _root_basis(self, s: Sample, item: str) -> tuple[str, str, datetime]:
        rule = self.rules.get(item)
        use_start = (
            s.kind.value == "continuous"
            and rule is not None
            and rule.continuous_basis == ContinuousBasis.START
        )
        if use_start:
            return s.id, "sampling_start", s.sampling_start
        return s.id, "sampling_end", s.sampling_end

    def resolve_basis(
        self, sid: str, item: str, stack: Optional[list[str]] = None
    ) -> tuple[str, str, datetime, Optional[datetime]]:
        """返回 (origin_sample_id, basis_kind, basis_time, merged_at)。"""
        stack = stack or []
        memo = self._basis_memo.setdefault(sid, {})
        if item in memo:
            return memo[item]
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
            res = (*self._root_basis(s, item), None)
            memo[item] = res
            return res

        parent_bases = []
        for p in s.parent_ids:
            parent_bases.append(self.resolve_basis(p, item, stack + [sid]))
        chosen = min(parent_bases, key=lambda b: b[2])
        merged_at = s.merged_at if s.kind.value == "composite" else None
        res = (chosen[0], chosen[1], chosen[2], merged_at)
        memo[item] = res
        return res

    # ------------------------------------------------- 共享历史收集 ----

    def collect_history(self, sid: str, item: str) -> dict:
        """沿来源链收集该时钟可见的历史。

        穿过合样边界时，只取该合样 ``merged_at`` 之前（含）的组成样记录；
        穿过分样边界不过滤（同一段共享历史）。
        """
        pres, temps, pre, ana, xfer = [], [], [], [], []
        seen: set[str] = set()
        stack: list[tuple[str, Optional[datetime]]] = [(sid, None)]
        walls: list[str] = []
        while stack:
            cur, wall = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            node = self.samples.get(cur)
            if node is None:
                continue

            def ok(t: datetime) -> bool:
                return wall is None or t <= wall

            pres += [a for a in node.preservation if ok(a.time)]
            temps += [t for t in node.temperature if ok(t.time)]
            pre += [e for e in node.pretreatments if ok(e.time)]
            ana += [e for e in node.analyses if ok(e.time)]
            xfer += [t for t in node.custody_transfers if ok(t)]
            if wall is not None:
                walls.append(f"{cur} 的记录仅取 <= {iso(wall)}（合样墙）")

            for p in node.parent_ids:
                if node.kind.value == "composite":
                    w = node.merged_at or node.sampling_start
                    new_wall = w if wall is None else min(wall, w)
                else:
                    new_wall = wall
                stack.append((p, new_wall))

        pres.sort(key=lambda a: a.time)
        temps.sort(key=lambda t: t.time)
        pre.sort(key=lambda e: e.time)
        ana.sort(key=lambda e: e.time)
        xfer.sort()
        return {
            "preservation": pres, "temperature": temps, "pretreatments": pre,
            "analyses": ana, "transfers": xfer, "wall_notes": walls,
            "seen": sorted(seen),
        }

    # ----------------------------------------------------- 单时钟 ----

    def _phase(
        self,
        phase: str,
        basis_time: datetime,
        limit: Optional[float],
        done_at: Optional[datetime],
        eval_time: datetime,
        critical_min: float,
        rule_source: str,
        invalid: bool,
    ) -> PhaseInfo:
        if limit is None:
            status = (
                PhaseStatus.INVALID if invalid
                else PhaseStatus.COMPLETED if done_at is not None
                else PhaseStatus.PENDING
            )
            return PhaseInfo(
                phase=phase, limit_minutes=None, deadline=None, done_at=done_at,
                status=status, basis_time=basis_time, rule_source=rule_source,
            )
        deadline = basis_time + timedelta(minutes=limit)
        if invalid:
            status = PhaseStatus.INVALID
            remaining = None
        elif done_at is not None:
            status = (
                PhaseStatus.COMPLETED if done_at <= deadline else PhaseStatus.OVERDUE
            )
            remaining = None
        else:
            remaining = _remaining_minutes(deadline, eval_time)
            if eval_time > deadline:
                status = PhaseStatus.OVERDUE
            elif timedelta(minutes=remaining) <= timedelta(minutes=critical_min):
                status = PhaseStatus.CRITICAL
            else:
                status = PhaseStatus.PENDING
        return PhaseInfo(
            phase=phase, limit_minutes=limit, deadline=deadline, done_at=done_at,
            status=status, remaining_minutes=remaining, basis_time=basis_time,
            rule_source=rule_source,
        )

    def build_clock(self, sid: str, item: str) -> tuple[ClockResult, list[dict]]:
        """返回时钟与原始违规记录列表。"""
        s = self.samples[sid]
        req = self.req
        raw_viol: list[dict] = []
        deriv: list[DerivationStep] = []
        rule = self.rules.get(item)
        rule_source = (
            f"rule:{req.rule_set.version if req.rule_set else req.rule_version}"
            f"#item={item}"
            if rule is not None else f"missing#item={item}"
        )

        invalid = (
            sid in self.cycle_tainted
            or sid in self.break_tainted
            or sid in self.inv_tainted
        )

        basis_time = origin_id = basis_kind = merged_at = None
        try:
            origin_id, basis_kind, basis_time, merged_at = self.resolve_basis(sid, item)
            deriv.append(DerivationStep(
                step="resolve_basis",
                detail=(
                    f"{sid}/{item} 基准来自 {origin_id} 的 {basis_kind}="
                    f"{iso(basis_time)}" + (f"；合样墙 merged_at={iso(merged_at)}"
                                            if merged_at else "")
                ),
            ))
        except _Cycle as exc:
            raw_viol.append(("source_cycle", sid, [item],
                             f"样品 {sid} 的来源链成环（触及 {exc.sid}），基准不可解析",
                             {"node": exc.sid}))
        except _Break as exc:
            raw_viol.append(("source_break", sid, [item],
                             f"样品 {sid} 的来源 {exc.sid} 不在本批记录中（来源断档）",
                             {"missing_parent": exc.sid}))

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

        if rule is None:
            raw_viol.append(("item_rule_missing", sid, [item],
                             f"样品 {sid} 的项目 {item} 没有对应时限规则", {}))
            invalid = True

        if sid in self.inv_tainted:
            for rec in self._inversion_records(sid):
                raw_viol.append(("time_inversion", sid, [item], rec["message"],
                                 rec["detail"]))

        deadline_pre = deadline_ana = None
        pre_done = ana_done = None

        if rule is not None and basis_time is not None:
            if rule.pretreatment_minutes is not None:
                deadline_pre = basis_time + timedelta(
                    minutes=rule.pretreatment_minutes)
            deadline_ana = basis_time + timedelta(minutes=rule.analysis_minutes)
            deriv.append(DerivationStep(
                step="deadlines",
                detail=(
                    f"预处理截止 {iso(deadline_pre)}（基准 + "
                    f"{rule.pretreatment_minutes} 分钟）；"
                    f"分析截止 {iso(deadline_ana)}（基准 + "
                    f"{rule.analysis_minutes} 分钟，前处理不清零）"
                ),
            ))

            scoped_pre = [
                e for e in hist["pretreatments"]
                if not e.items or item in e.items
            ]
            if scoped_pre:
                pre_done = scoped_pre[-1].time
            ana_events = [e for e in hist["analyses"] if e.item == item]
            if ana_events:
                ana_done = ana_events[-1].time

            # 防腐动作
            have = {a.name for a in hist["preservation"] if a.time <= req.eval_time}
            missing = [p for p in rule.required_preservation if p not in have]
            if missing:
                raw_viol.append((
                    "missing_preservation", sid, [item],
                    f"样品 {sid} 的项目 {item} 缺少防腐动作 {missing}",
                    {"required": rule.required_preservation, "performed": sorted(have)},
                ))

            # 交接晚于截止时刻
            if hist["transfers"]:
                latest_xfer = hist["transfers"][-1]
                late_against = None
                if deadline_ana is not None and latest_xfer > deadline_ana:
                    late_against = ("analysis", deadline_ana)
                elif deadline_pre is not None and latest_xfer > deadline_pre:
                    late_against = ("pretreatment", deadline_pre)
                if late_against:
                    raw_viol.append((
                        "late_transfer", sid, [item],
                        f"样品 {sid} 项目 {item} 最近交接 {iso(latest_xfer)} "
                        f"晚于{late_against[0]}截止 {iso(late_against[1])}",
                        {"transfer_at": iso(latest_xfer),
                         "pretreatment_deadline": iso(deadline_pre),
                         "analysis_deadline": iso(deadline_ana),
                         "late_against": late_against[0],
                         "minutes_late": _remaining_minutes(latest_xfer,
                                                            late_against[1])},
                    ))

        phases: list[PhaseInfo] = []
        if rule is not None:
            phases.append(self._phase(
                "pretreatment", basis_time or s.sampling_start,
                rule.pretreatment_minutes, pre_done, req.eval_time,
                req.critical_within_minutes, rule_source,
                invalid or basis_time is None,
            ))
            phases.append(self._phase(
                "analysis", basis_time or s.sampling_start,
                rule.analysis_minutes, ana_done, req.eval_time,
                req.critical_within_minutes, rule_source,
                invalid or basis_time is None,
            ))
            # 基准不可解析（成环/断档）时温度检查仍要做：回退到该样品采样窗口
            temp_basis = basis_time or s.sampling_start
            self._temperature_checks(
                sid, item, rule, hist["temperature"], temp_basis,
                req.eval_time, raw_viol,
            )
        else:
            phases.append(self._phase(
                "analysis", s.sampling_start, None, ana_done, req.eval_time,
                req.critical_within_minutes, rule_source, True,
            ))

        # 时钟汇总状态
        if invalid or basis_time is None or rule is None:
            status = ClockStatus.INVALID
        elif any(p.status == PhaseStatus.OVERDUE for p in phases):
            status = ClockStatus.OVERDUE
        elif ana_done is not None and all(
            p.status == PhaseStatus.COMPLETED
            for p in phases
            if p.limit_minutes is not None
        ):
            status = ClockStatus.COMPLETED
        elif any(p.status == PhaseStatus.CRITICAL for p in phases):
            status = ClockStatus.CRITICAL
        else:
            status = ClockStatus.OK

        next_phase = next(
            (p for p in phases
             if p.limit_minutes is not None and p.status
             in (PhaseStatus.PENDING, PhaseStatus.CRITICAL, PhaseStatus.OVERDUE)),
            None,
        )
        next_action = next_phase.phase if next_phase else None  # type: ignore[arg-type]
        next_deadline = next_phase.deadline if next_phase else None
        remaining = (
            _remaining_minutes(next_deadline, req.eval_time)
            if next_deadline is not None else None
        )
        item_violations = [v for v in raw_viol if v[2] == [item]]
        conforming = (
            status not in (ClockStatus.INVALID, ClockStatus.OVERDUE)
            and not item_violations
        )

        clock = ClockResult(
            sample_id=sid, item=item,
            origin_sample_id=origin_id or sid,
            basis=basis_kind or "sampling_end",
            basis_time=basis_time or s.sampling_start,
            merged_at=merged_at,
            status=status, conforming=conforming, phases=phases,
            next_action_deadline=next_deadline,
            next_action=next_action,
            remaining_minutes=remaining,
            latest_operation_at=deadline_ana,
            rule_source=rule_source if rule else f"missing#item={item}",
            derivation=deriv,
        )
        return clock, raw_viol

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
    rule_set: RuleSet,
    content_hash: str,
    package_id: str,
    version_no: int,
    trial: bool,
    created_at: datetime,
    changes_from: Optional[str] = None,
    changes: Optional[list[StatusChange]] = None,
    sample_id: Optional[str] = None,
) -> JudgmentResult:
    eng = _Engine(request, rule_set)
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

    # 应优先收样的批次：仍有待办阶段的时钟，按截止时刻升序，按样品合并
    pending = [
        c for c in clocks
        if c.status in (ClockStatus.OVERDUE, ClockStatus.CRITICAL, ClockStatus.OK)
        and c.next_action_deadline is not None
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
        "invalid": sum(1 for c in clocks if c.status == ClockStatus.INVALID),
        "violations": len(violations),
        "priority_batch_count": len(priority),
    }

    version_tag = rule_set.version
    return JudgmentResult(
        package_id=package_id,
        sample_id=sample_id,
        version_no=version_no,
        trial=trial,
        request_id=request.request_id,
        eval_time=request.eval_time,
        created_at=created_at,
        rule=RuleRef(
            version=version_tag, content_hash=content_hash,
            name=rule_set.name, item_count=len(rule_set.items),
        ),
        basis_policy={
            "continuous_default": ContinuousBasis.END.value,
            "per_item": {
                r.item: r.continuous_basis.value for r in rule_set.items
            },
        },
        summary=summary,
        clocks=clocks,
        violations=violations,
        priority_batches=priority,
        changes_from=changes_from,
        changes=changes or [],
    )
