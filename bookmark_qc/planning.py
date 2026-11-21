"""一周内重新开卖的机时与留样规划。

工坊在机时（整小时）与留样都有限的情况下，要比较把剩余留样用于
**定位缺陷（批次归因）** 还是 **验证某款恢复销售（复验放行）**。
规划器遵守三条硬约束：

1. **消耗性互斥**：同一件留样的消耗性试验一旦确认（``committed``），
   其他消耗该件的方案即不可选；同一方案内也不能出现两件消耗同一样品
   的试验；
2. **机时窗口**：试验只能排在 ``test_hours`` 给出容量的日期，已确认
   排期先占用容量，方案内试验按最早可容纳日期顺排；
3. **留样可得**：只有留样（``reserved=true``）且未被已确认消耗性试验
   用掉的样品才能进入方案。

每个方案显式列出能解除哪些保留（暂停新放行/追查范围）、纹路适用范围
是否匹配，以及仍有哪些款无法从结果推断能否销售。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from itertools import combinations

from .model import Assay, Project
from .release import release_report

PURPOSE_DIAGNOSE = "批次归因"
PURPOSE_REVALIDATE = "复验放行"

DIAGNOSE = "定位缺陷"
RESUME_SALES = "验证恢复销售"

#: 方案路线标签，按试验目的归并。
ROUTE_BY_PURPOSE = {
    PURPOSE_DIAGNOSE: DIAGNOSE,
    PURPOSE_REVALIDATE: RESUME_SALES,
}


@dataclass(frozen=True)
class Hold:
    permission_id: str
    sku: str
    pattern: str
    process: str
    lot: str
    sample: str
    action: str
    delivered: bool

    def to_dict(self) -> dict:
        return {
            "permission_id": self.permission_id,
            "sku": self.sku,
            "pattern": self.pattern,
            "process": self.process,
            "lot": self.lot,
            "sample": self.sample,
            "action": self.action,
            "delivered": self.delivered,
        }


@dataclass(frozen=True)
class Lift:
    hold: Hold
    via_assay: str
    lift_kind: str  # 定位解除 / 复验解除
    pattern_matched: bool

    def to_dict(self) -> dict:
        data = self.hold.to_dict()
        data.update(
            via_assay=self.via_assay,
            lift_kind=self.lift_kind,
            pattern_matched=self.pattern_matched,
        )
        return data


@dataclass(frozen=True)
class AssayOption:
    assay: Assay
    route: str
    eligible: bool
    blockers: tuple[str, ...]
    sample_lot: str
    sample_pattern: str
    sample_available: bool
    lifts: tuple[Lift, ...]
    scope_warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "assay": self.assay.id,
            "sample": self.assay.sample,
            "purpose": self.assay.purpose,
            "sku": self.assay.sku,
            "hours": self.assay.hours,
            "consumes": self.assay.consumes,
            "route": self.route,
            "eligible": self.eligible,
            "blockers": list(self.blockers),
            "sample_lot": self.sample_lot,
            "sample_pattern": self.sample_pattern,
            "sample_available": self.sample_available,
            "lifts": [lift.to_dict() for lift in self.lifts],
            "scope_warnings": list(self.scope_warnings),
        }


@dataclass(frozen=True)
class Plan:
    assay_ids: tuple[str, ...]
    route: str
    schedule: dict[str, tuple[str, ...]]
    total_hours: int
    consumed_samples: tuple[str, ...]
    lifts: tuple[Lift, ...]
    lifted_hold_ids: tuple[str, ...]
    remaining_holds: tuple[Hold, ...]
    uninferrable_skus: tuple[str, ...]
    conflicts: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "assay_ids": list(self.assay_ids),
            "route": self.route,
            "schedule": {day: list(ids) for day, ids in sorted(self.schedule.items())},
            "total_hours": self.total_hours,
            "consumed_samples": list(self.consumed_samples),
            "lifts": [lift.to_dict() for lift in self.lifts],
            "lifted_hold_ids": list(self.lifted_hold_ids),
            "remaining_holds": [hold.to_dict() for hold in self.remaining_holds],
            "uninferrable_skus": list(self.uninferrable_skus),
            "conflicts": list(self.conflicts),
        }


@dataclass(frozen=True)
class PlanningReport:
    window: tuple[date, date]
    window_deadline: date
    capacity_by_day: dict[str, int]
    committed_hours_by_day: dict[str, int]
    out_of_window_capacity: dict[str, int]
    options: tuple[AssayOption, ...]
    plans: tuple[Plan, ...]
    consumed_blocked: tuple[str, ...]
    scope_note: str

    def to_dict(self) -> dict:
        return {
            "window": [self.window[0].isoformat(), self.window[1].isoformat()],
            "window_deadline": self.window_deadline.isoformat(),
            "capacity_by_day": dict(self.capacity_by_day),
            "committed_hours_by_day": dict(self.committed_hours_by_day),
            "out_of_window_capacity": dict(self.out_of_window_capacity),
            "options": [o.to_dict() for o in self.options],
            "plans": [p.to_dict() for p in self.plans],
            "consumed_blocked": list(self.consumed_blocked),
            "scope_note": self.scope_note,
        }


# --------------------------------------------------------------------
# 保留清单
# --------------------------------------------------------------------
def active_holds(project: Project) -> list[Hold]:
    """当前仍生效的保留：暂停新放行与已交付追查范围。"""
    report = release_report(project)
    holds: list[Hold] = []
    for impact in report.impacts:
        for item in (*impact.holds, *impact.trace_scope):
            run = project.run_of_sample(item.sample)
            holds.append(
                Hold(
                    permission_id=item.permission_id,
                    sku=item.sku,
                    pattern=item.pattern,
                    process=item.process,
                    lot=run.lot,
                    sample=item.sample,
                    action=item.action,
                    delivered=item.delivered,
                )
            )
    return holds


# --------------------------------------------------------------------
# 试验选项与可解除的保留
# --------------------------------------------------------------------
def _lifts_for_assay(project: Project, assay: Assay, holds: list[Hold]) -> tuple[list[Lift], list[str]]:
    run = project.run_of_sample(assay.sample)
    lifts: list[Lift] = []
    warnings: list[str] = []
    if assay.purpose == PURPOSE_DIAGNOSE:
        for hold in holds:
            if hold.lot == run.lot:
                lifts.append(
                    Lift(hold=hold, via_assay=assay.id, lift_kind="定位解除", pattern_matched=True)
                )
    elif assay.purpose == PURPOSE_REVALIDATE:
        for hold in holds:
            if hold.sku != assay.sku:
                continue
            matched = run.pattern == hold.pattern
            if not matched:
                warnings.append(
                    f"{assay.id} 留样纹路「{run.pattern}」与 {hold.permission_id}（{hold.sku}）"
                    f"声明纹路「{hold.pattern}」不一致，复验不能覆盖该纹路的保留"
                )
            lifts.append(
                Lift(
                    hold=hold,
                    via_assay=assay.id,
                    lift_kind="复验解除",
                    pattern_matched=matched,
                )
            )
    return lifts, warnings


def assay_options(project: Project, window_end: date | None = None) -> list[AssayOption]:
    holds = active_holds(project)
    options: list[AssayOption] = []
    for assay in sorted(project.assays.values(), key=lambda a: a.id):
        run = project.run_of_sample(assay.sample)
        sample = project.samples[assay.sample]
        blockers: list[str] = []
        warnings: list[str] = []

        already_committed = assay.id in project.committed
        if already_committed:
            blockers.append("试验已确认排期，不再是可选方案")
        if not sample.reserved:
            blockers.append(f"{assay.sample} 不是留样，不能用于试验")
        if project.is_consumed(assay.sample) and not already_committed:
            blockers.append(f"留样 {assay.sample} 已被已确认的消耗性试验用掉")
        if assay.hours <= 0:
            blockers.append("试验机时必须为正整数")

        lifts, lift_warnings = _lifts_for_assay(project, assay, holds)
        warnings.extend(lift_warnings)
        if assay.purpose == PURPOSE_REVALIDATE and not lifts:
            warnings.append(
                f"{assay.id} 拟复验的 {assay.sku} 当前没有生效保留，试验不解除任何保留"
            )

        options.append(
            AssayOption(
                assay=assay,
                route=ROUTE_BY_PURPOSE.get(assay.purpose, assay.purpose),
                eligible=not blockers,
                blockers=tuple(blockers),
                sample_lot=run.lot,
                sample_pattern=run.pattern,
                sample_available=sample.reserved and not project.is_consumed(assay.sample),
                lifts=tuple(lifts),
                scope_warnings=tuple(warnings),
            )
        )
    return options


# --------------------------------------------------------------------
# 机时窗口与方案枚举
# --------------------------------------------------------------------
def _window(project: Project, as_of: date | None, window_end: date | None) -> tuple[date, date, date]:
    """返回（起算日, 实际覆盖末日, 一周自然截止日）。

    “一周内重新开卖”的自然截止日是起算日 +7 天；实际覆盖末日取窗口内
    最后一个登记了机时容量的日期（可能早于自然截止日）。
    """
    if as_of is None:
        if project.recheck:
            as_of = max(c.discovered_on for c in project.recheck.values())
        else:
            as_of = max(project.test_hours)
    if window_end is None:
        window_end = as_of + timedelta(days=7)
    days = sorted(d for d in project.test_hours if as_of < d <= window_end)
    covered_end = max(days) if days else window_end
    return as_of, covered_end, window_end


def _committed_usage(project: Project) -> dict[date, int]:
    used: dict[date, int] = {}
    for commitment in project.committed.values():
        assay = project.assays[commitment.assay]
        used[commitment.date] = used.get(commitment.date, 0) + assay.hours
    return used


def _try_schedule(
    project: Project, assays: list[Assay], window: tuple[date, date]
) -> dict[date, list[str]] | None:
    """最早可容纳日顺排；任一天放不下则该组合不可行。"""
    usage = _committed_usage(project)
    schedule: dict[date, list[str]] = {}
    days = sorted(d for d in project.test_hours if window[0] < d <= window[1])
    for assay in sorted(assays, key=lambda a: (-a.hours, a.id)):
        placed = False
        for day in days:
            used = usage.get(day, 0)
            if used + assay.hours <= project.test_hours[day].capacity:
                usage[day] = used + assay.hours
                schedule.setdefault(day, []).append(assay.id)
                placed = True
                break
        if not placed:
            return None
    return schedule


def _conflicts(project: Project, assay: Assay) -> list[str]:
    """与该试验消耗同一件留样的互斥试验（含已确认/已关闭的选项）。

    消耗性试验一旦确认，同件留样不能再算进另一个方案，因此互斥名单
    不受对方当前是否可选影响。
    """
    out = []
    for other in project.assays.values():
        if other.id == assay.id:
            continue
        if other.sample == assay.sample and assay.consumes and other.consumes:
            out.append(other.id)
    return sorted(out)


def build_planning_report(
    project: Project, as_of: date | None = None, window_end: date | None = None
) -> PlanningReport:
    window_start, covered_end, deadline = _window(project, as_of, window_end)
    window = (window_start, covered_end)
    options = assay_options(project, covered_end)
    eligible = [o.assay for o in options if o.eligible]
    holds = active_holds(project)
    all_hold_ids = {h.permission_id for h in holds}

    plans: list[Plan] = []
    # 枚举所有非空可行子集（含纯定位、纯复验与混合路线）。
    for size in range(1, len(eligible) + 1):
        for combo in combinations(sorted(eligible, key=lambda a: a.id), size):
            combo_list = list(combo)
            consumed = [a.sample for a in combo_list if a.consumes]
            if len(set(consumed)) != len(consumed):
                continue  # 同方案内消耗同件留样，互斥
            schedule = _try_schedule(project, combo_list, window)
            if schedule is None:
                continue

            lifts: list[Lift] = []
            option_by_id = {o.assay.id: o for o in options}
            for assay in combo_list:
                lifts.extend(option_by_id[assay.id].lifts)
            # 同一保留被多个试验覆盖时只计一次，优先取纹路匹配的解除。
            best_lift: dict[str, Lift] = {}
            for lift in lifts:
                hold_id = lift.hold.permission_id
                if hold_id not in best_lift or (
                    not best_lift[hold_id].pattern_matched and lift.pattern_matched
                ):
                    best_lift[hold_id] = lift
            lifted_ids = set(best_lift)
            remaining = tuple(h for h in holds if h.permission_id not in lifted_ids)
            skus = {h.sku for h in holds}
            lifted_skus = {
                lift.hold.sku for lift in best_lift.values() if lift.pattern_matched
            }
            uninferrable = tuple(sorted(skus - lifted_skus))
            routes = {ROUTE_BY_PURPOSE.get(a.purpose, a.purpose) for a in combo_list}
            route = "混合：" + "+".join(sorted(routes)) if len(routes) > 1 else next(iter(routes))
            conflicts = sorted({c for a in combo_list for c in _conflicts(project, a)})
            plans.append(
                Plan(
                    assay_ids=tuple(a.id for a in combo_list),
                    route=route,
                    schedule={
                        day.isoformat(): tuple(sorted(ids))
                        for day, ids in sorted(schedule.items())
                    },
                    total_hours=sum(a.hours for a in combo_list),
                    consumed_samples=tuple(sorted(set(consumed))),
                    lifts=tuple(best_lift.values()),
                    lifted_hold_ids=tuple(sorted(lifted_ids)),
                    remaining_holds=remaining,
                    uninferrable_skus=uninferrable,
                    conflicts=tuple(conflicts),
                )
            )

    capacity = {
        day.isoformat(): th.capacity
        for day, th in sorted(project.test_hours.items())
        if window[0] < day <= window[1]
    }
    out_of_window = {
        day.isoformat(): th.capacity
        for day, th in sorted(project.test_hours.items())
        if day > window[1]
    }
    committed_hours = {
        day.isoformat(): hours for day, hours in sorted(_committed_usage(project).items())
    }
    blocked = tuple(sorted(o.assay.id for o in options if not o.eligible))
    scope = (
        "复验放行只对纹路匹配且当前有生效保留的款解除销售保留；批次归因在同批次内"
        "定位根因。未出现在任何方案 lifts 中的款仍无法从试验结果推断能否销售。"
        "out_of_window_capacity 是一周窗口之外的机时，不参与本次方案。"
    )
    return PlanningReport(
        window=window,
        window_deadline=deadline,
        capacity_by_day=capacity,
        committed_hours_by_day=committed_hours,
        out_of_window_capacity=out_of_window,
        options=tuple(options),
        plans=tuple(plans),
        consumed_blocked=blocked,
        scope_note=scope,
    )
