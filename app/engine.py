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
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .matching import DIMENSIONS, evaluate_applicability
from .models import (
    CandidateRuleView,
    ClockResult,
    ClockRuleRef,
    ClockStatus,
    ContinuousBasis,
    DerivationStep,
    FieldSource,
    ItemRule,
    JudgmentRequest,
    JudgmentResult,
    MatchContext,
    PhaseInfo,
    PhaseStatus,
    PriorityBatch,
    RuleRef,
    RuleSet,
    Sample,
    StatusChange,
    UnmetCondition,
    Violation,
)

GAP_TOLERANCE_MIN = 15.0  # 温度记录断档容差（分钟）


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _remaining_minutes(deadline: datetime, eval_time: datetime) -> int:
    return int(round((deadline - eval_time).total_seconds() / 60.0))


@dataclass(frozen=True)
class PoolEntry:
    """候选池中的一条项目规则。"""
    version: str
    content_hash: str
    rule: ItemRule
    adhoc: bool = False  # 请求携带（未登记）规则集


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

    def _root_basis(
        self, s: Sample, item: str, rule: Optional[ItemRule]
    ) -> tuple[str, str, datetime]:
        use_start = (
            s.kind.value == "continuous"
            and rule is not None
            and rule.continuous_basis == ContinuousBasis.START
        )
        if use_start:
            return s.id, "sampling_start", s.sampling_start
        return s.id, "sampling_end", s.sampling_end

    def resolve_basis(
        self,
        sid: str,
        item: str,
        rule: Optional[ItemRule] = None,
        stack: Optional[list[str]] = None,
    ) -> tuple[str, str, datetime, Optional[datetime]]:
        """返回 (origin_sample_id, basis_kind, basis_time, merged_at)。

        连续样取开始还是结束取决于候选规则自身的 continuous_basis，因此基准
        解析按 (sid, item, basis) 记忆。
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
        chosen = min(parent_bases, key=lambda b: b[2])
        merged_at = s.merged_at if s.kind.value == "composite" else None
        res = (chosen[0], chosen[1], chosen[2], merged_at)
        memo[key] = res
        return res

    # ------------------------------------------------- 共享历史收集 ----

    def collect_history(self, sid: str, item: str) -> dict:
        """沿来源链收集该时钟可见的历史。

        穿过合样边界时，只取该合样 ``merged_at`` 之前（含）的组成样记录；
        穿过分样边界不过滤（同一段共享历史）。防腐动作另外记录动作来源样品。
        """
        pres, temps, pre, ana, xfer = [], [], [], [], []
        pres_sources: dict[str, list[dict]] = {}
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

            for a in node.preservation:
                if ok(a.time):
                    pres.append(a)
                    pres_sources.setdefault(a.name, []).append(
                        {"sample_id": cur, "time": iso(a.time)}
                    )
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
        basis_time: datetime
    ) -> dict[str, FieldSource]:
        """构建全部匹配维度的字段来源（取值可能为 None）。"""
        sources: dict[str, FieldSource] = {}
        sources["effective_time"] = FieldSource(
            field="basis_time", value=iso(basis_time), source_sample_id=origin_id,
            source_field=basis_kind,
            note="原始采样时刻（沿来源链解析；连续样按规则 continuous_basis 取端）",
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
        if basis is not None:
            ctx = {
                "basis_time": basis[2],
                "matrix": sources["matrix"].value,
                "method": sources["method"].value,
                "container": sources["container"].value,
                "storage_condition": sources["storage_condition"].value,
            }
            raw_unmet = evaluate_applicability(r, ctx)
        else:
            raw_unmet = [{"dimension": "effective_time",
                          "required": [r.effective_from, r.effective_to],
                          "actual": None}]
        unmet = []
        for u in raw_unmet:
            dim = u["dimension"]
            src = sources.get(dim)
            unmet.append(UnmetCondition(
                dimension=dim,
                required=[iso(x) for x in u["required"]]
                if dim == "effective_time" else list(u["required"]),
                actual=iso(u["actual"]) if dim == "effective_time" else u["actual"],
                field_sources=[src] if src else [],
            ))
        return CandidateRuleView(
            version=entry.version, content_hash=entry.content_hash,
            rule_id=r.rule_id, item=r.item, item_name=r.item_name,
            applies=not unmet, unmet=unmet,
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
        applying: list[tuple[PoolEntry, tuple]] = []
        resolved: dict[tuple, tuple] = {}  # entry key -> basis
        sources: Optional[dict[str, FieldSource]] = None
        basis_failed = fallback_basis is None
        if fallback_basis is not None:
            ob, bk, bt, _ = fallback_basis
            sources = self._condition_sources(sid, item, ob, bk, bt)
        else:
            s = self.samples[sid]
            sources = self._condition_sources(
                sid, item, sid, "sampling_end", s.sampling_start
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
        elif len(applying) == 1:
            chosen, chosen_basis = applying[0]
            status = "unique"
        elif len(applying) == 0:
            status = "none"
        else:
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
        basis_time: datetime,
        limit: Optional[float],
        done_at: Optional[datetime],
        eval_time: datetime,
        critical_min: float,
        rule_source: str,
        invalid: bool,
    ) -> PhaseInfo:
        if limit is None:
            pstatus = (
                PhaseStatus.INVALID if invalid
                else PhaseStatus.COMPLETED if done_at is not None
                else PhaseStatus.PENDING
            )
            return PhaseInfo(
                phase=phase, limit_minutes=None, deadline=None, done_at=done_at,
                status=pstatus, basis_time=basis_time, rule_source=rule_source,
            )
        deadline = basis_time + timedelta(minutes=limit)
        if invalid:
            pstatus = PhaseStatus.INVALID
            remaining = None
        elif done_at is not None:
            pstatus = (
                PhaseStatus.COMPLETED if done_at <= deadline else PhaseStatus.OVERDUE
            )
            remaining = None
        else:
            remaining = _remaining_minutes(deadline, eval_time)
            if eval_time > deadline:
                pstatus = PhaseStatus.OVERDUE
            elif timedelta(minutes=remaining) <= timedelta(minutes=critical_min):
                pstatus = PhaseStatus.CRITICAL
            else:
                pstatus = PhaseStatus.PENDING
        return PhaseInfo(
            phase=phase, limit_minutes=limit, deadline=deadline, done_at=done_at,
            status=pstatus, remaining_minutes=remaining, basis_time=basis_time,
            rule_source=rule_source,
        )

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

        origin_id = basis_kind = basis_time = merged_at = None
        if display_basis is not None:
            origin_id, basis_kind, basis_time, merged_at = display_basis
            deriv.append(DerivationStep(
                step="resolve_basis",
                detail=(
                    f"{sid}/{item} 基准来自 {origin_id} 的 {basis_kind}="
                    f"{iso(basis_time)}" + (f"；合样墙 merged_at={iso(merged_at)}"
                                            if merged_at else "")
                    + ("（仅用于展示，未参与截止计算）" if basis_tuple is None else "")
                ),
            ))

        # 结构问题（成环/断档/真实时间倒置）：基准不可解析或时序非法。
        # 注意：判定时刻之后的“未来记录”只逐时钟记 time_inversion 违规，
        # 不进入 inv_tainted，因此不会把时钟整体置为失效（与旧行为一致）。
        structural_invalid = (
            sid in self.cycle_tainted or sid in self.break_tainted
            or sid in self.inv_tainted
        )
        if basis_time is None:
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
                    f"{iso(basis_time)} 与实际条件筛选 -> {label} "
                    f"{selection['chosen'].version}#{rule.rule_id} "
                    f"(hash={selection['chosen'].content_hash[:12]})"
                ),
            ))
        else:
            deriv.append(DerivationStep(
                step="match_rule",
                detail=(
                    f"候选 {len(selection['entries'])} 条，无唯一适用规则"
                    f"（{match_status}）；不计算截止时刻、不形成合规结论"
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
        if rule is None and not (sid in self.inv_tainted and basis_time is None):
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

        # 判定时刻之后才“发生”的事件属于未来记录：记 time_inversion，
        # 列出受影响项目与完整推导；这些事件不得据此结束任何阶段，也不参与交接判定。
        ignored_future: list[str] = []

        def future_violation(kind: str, label: str, when: datetime,
                             phase_hint: str = "") -> None:
            ignored_future.append(f"{label}@{iso(when)}")
            raw_viol.append((
                "time_inversion", sid, [item],
                f"样品 {sid} 项目 {item} 的{label}时刻 {iso(when)} 晚于判定时刻 "
                f"{iso(req.eval_time)}（事件尚未发生{phase_hint}）",
                {"event_type": kind, "event_time": iso(when),
                 "eval_time": iso(req.eval_time)},
            ))

        for a in hist["preservation"]:
            if a.time > req.eval_time:
                future_violation("preservation", f"防腐动作 {a.name}", a.time)
        for e in hist["pretreatments"]:
            if e.time > req.eval_time and (not e.items or item in e.items):
                future_violation("pretreatment", f"前处理事件 {e.type}", e.time,
                                 "，不得据此结束预处理阶段")
        for e in hist["analyses"]:
            if e.item == item and e.time > req.eval_time:
                future_violation("analysis", f"分析事件({item})", e.time,
                                 "，不得据此结束分析阶段")
        for p in hist["temperature"]:
            if p.time > req.eval_time:
                future_violation("temperature",
                                 f"温度记录 {p.temp_c}℃", p.time)
        eligible_transfers = [t for t in hist["transfers"] if t <= req.eval_time]
        for t in hist["transfers"]:
            if t > req.eval_time:
                future_violation("custody_transfer", "交接", t,
                                 "，不计入已完成交接")
        if ignored_future:
            deriv.append(DerivationStep(
                step="ignore_future_events",
                detail=(
                    f"{len(ignored_future)} 条记录晚于判定时刻 {iso(req.eval_time)}，"
                    f"已识别为 time_inversion 且不结束任何阶段：{ignored_future}"
                ),
            ))

        deadline_pre = deadline_ana = None
        pre_done = ana_done = None
        missing_preservation = False
        rule_source = (
            f"rule:{selection['chosen'].version}#{rule.rule_id}(item={item})"
            if rule is not None else f"unmatched#item={item}({match_status})"
        )

        # 仅唯一/强制匹配的规则参与截止时刻计算；display_basis 只用于展示
        rule_selected = rule is not None and basis_tuple is not None
        if rule_selected:
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

            # 仅判定时刻之前（含）的事件才能结束对应阶段
            scoped_pre = [
                e for e in hist["pretreatments"]
                if e.time <= req.eval_time and (not e.items or item in e.items)
            ]
            if scoped_pre:
                pre_done = scoped_pre[-1].time
            ana_events = [
                e for e in hist["analyses"]
                if e.item == item and e.time <= req.eval_time
            ]
            if ana_events:
                ana_done = ana_events[-1].time

            # 防腐动作（同样只计已发生的）；动作来源样品随违规返回
            have = {a.name for a in hist["preservation"] if a.time <= req.eval_time}
            missing = [p for p in rule.required_preservation if p not in have]
            if missing:
                missing_preservation = True
                raw_viol.append((
                    "missing_preservation", sid, [item],
                    f"样品 {sid} 的项目 {item} 保存动作不足：缺少 {missing}",
                    {
                        "required": rule.required_preservation,
                        "performed": sorted(have),
                        "performed_sources": [
                            {"name": name, "records": recs}
                            for name, recs in sorted(hist["pres_sources"].items())
                            if any(
                                datetime.fromisoformat(r["time"]) <= req.eval_time
                                for r in recs
                            )
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

            # 交接晚于截止时刻（未来交接不参与）
            if eligible_transfers:
                latest_xfer = eligible_transfers[-1]
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
        if rule_selected:
            phases.append(self._phase(
                "pretreatment", basis_time,
                rule.pretreatment_minutes, pre_done, req.eval_time,
                req.critical_within_minutes, rule_source,
                sid in self.inv_tainted,
            ))
            phases.append(self._phase(
                "analysis", basis_time,
                rule.analysis_minutes, ana_done, req.eval_time,
                req.critical_within_minutes, rule_source,
                sid in self.inv_tainted,
            ))
            self._temperature_checks(
                sid, item, rule, hist["temperature"], basis_time,
                req.eval_time, raw_viol,
            )

        # 时钟汇总状态
        if structural_invalid or basis_tuple is None and basis_time is None:
            status = ClockStatus.INVALID
        elif not rule_selected:
            status = ClockStatus.INDETERMINATE  # none / ambiguous / forced_missing
        elif any(p.status == PhaseStatus.OVERDUE for p in phases):
            status = ClockStatus.OVERDUE
        elif missing_preservation:
            status = ClockStatus.INDETERMINATE  # 保存动作不足：保留期限信息但不下结论
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

        # 能否形成合规结论：结构失效 / 无唯一匹配 / 试算强制 / 保存动作不足 -> 否
        conclusive = (
            rule_selected
            and match_status == "unique"
            and not missing_preservation
            and not structural_invalid
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
                basis_time=basis_time or s.sampling_start,
                field_sources=list(sources.values()),
            )

        clock = ClockResult(
            sample_id=sid, item=item,
            origin_sample_id=origin_id or sid,
            basis=basis_kind or "sampling_end",
            basis_time=basis_time or s.sampling_start,
            merged_at=merged_at,
            status=status, conforming=conforming, conclusive=conclusive,
            phases=phases,
            next_action_deadline=next_deadline,
            next_action=next_action,
            remaining_minutes=remaining,
            latest_operation_at=deadline_ana,
            rule_source=rule_source,
            matched_rule=matched_ref,
            match_status=match_status,
            candidates=selection["views"],
            match_context=match_context,
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
        "method_source": "sample.item_methods[item]",
        "condition_source": "本样品优先；未提交时沿来源链回退到祖先样品",
        "forced_selection": bool(request.selected_candidates),
    }
    basis_policy = {
        "continuous_default": ContinuousBasis.END.value,
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
