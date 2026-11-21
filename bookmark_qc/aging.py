"""老化复查看板：未到期、失联、成熟不合格分别显示。

状态判定以“看板日”（``as_of``，默认取项目中最近一次批次级异常的发现日；
没有异常时取今天）为基准：

* ``未到期``       —— 到期日晚于看板日，结果尚未取得，``null`` 不等于合格；
* ``成熟合格``     —— 已到期且检查结论为正常；
* ``成熟不合格``   —— 已到期且检查结论为发虚等缺陷；
* ``留样失联``     —— 登记结果为失联，无法取得事实；
* ``逾期未检``     —— 已到期但检查日与结论都还没登记，是缺口不是合格。

运营结论必须带适用范围：看板同时按批次与纹路汇总，说明结论在哪些
批次与纹路上成立、哪些仍在等待。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .model import Project

PENDING = "未到期"
MATURE_OK = "成熟合格"
MATURE_BAD = "成熟不合格"
LOST = "留样失联"
OVERDUE = "逾期未检"

#: 需要在报告中分别显示、不得合并的状态。
STATUS_ORDER = (PENDING, MATURE_OK, MATURE_BAD, LOST, OVERDUE)
#: 尚不能支持任何放行/归因结论的状态。
UNRESOLVED = (PENDING, LOST, OVERDUE)


@dataclass(frozen=True)
class AgingRow:
    sample: str
    lot: str
    sheet: str
    pattern: str
    process: str
    due: date
    inspected_on: date | None
    raw_result: str | None
    status: str

    def to_dict(self) -> dict:
        return {
            "sample": self.sample,
            "lot": self.lot,
            "sheet": self.sheet,
            "pattern": self.pattern,
            "process": self.process,
            "due": self.due.isoformat(),
            "inspected_on": self.inspected_on.isoformat() if self.inspected_on else None,
            "raw_result": self.raw_result,
            "status": self.status,
        }


@dataclass(frozen=True)
class GroupSummary:
    key: tuple[str, str]
    total: int
    counts: dict[str, int]
    unresolved_samples: tuple[str, ...]
    bad_samples: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "lot": self.key[0],
            "pattern": self.key[1],
            "total": self.total,
            "counts": {status: self.counts.get(status, 0) for status in STATUS_ORDER},
            "unresolved_samples": list(self.unresolved_samples),
            "bad_samples": list(self.bad_samples),
        }


@dataclass(frozen=True)
class AgingBoard:
    as_of: date
    rows: tuple[AgingRow, ...]
    overall_counts: dict[str, int]
    by_lot_pattern: tuple[GroupSummary, ...]
    applicable_scope: tuple[tuple[str, str], ...]
    pending_scope: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "rows": [r.to_dict() for r in self.rows],
            "overall_counts": {status: self.overall_counts.get(status, 0) for status in STATUS_ORDER},
            "by_lot_pattern": [g.to_dict() for g in self.by_lot_pattern],
            "applicable_scope": [list(k) for k in self.applicable_scope],
            "pending_scope": [list(k) for k in self.pending_scope],
        }


def default_as_of(project: Project) -> date:
    """默认看板日：最近一次批次级异常发现日；无异常时取本机当天。"""
    if project.recheck:
        return max(c.discovered_on for c in project.recheck.values())
    return date.today()


def classify(inspected_on: date | None, result: str | None, due: date, as_of: date) -> str:
    if result == "失联":
        return LOST
    # 已取得检查结论时以结论为准（允许提前完成复查）；未取得结论时才看到期日。
    if result is not None and inspected_on is not None:
        return MATURE_BAD if result != "正常" else MATURE_OK
    if due > as_of:
        return PENDING
    # 已到期却没有检查结论
    return OVERDUE


def aging_board(project: Project, as_of: date | None = None) -> AgingBoard:
    as_of = as_of or default_as_of(project)
    rows: list[AgingRow] = []
    for aging in project.aging.values():
        run = project.run_of_sample(aging.sample)
        rows.append(
            AgingRow(
                sample=aging.sample,
                lot=run.lot,
                sheet=run.sheet,
                pattern=run.pattern,
                process=run.process,
                due=aging.due,
                inspected_on=aging.inspected_on,
                raw_result=aging.result,
                status=classify(aging.inspected_on, aging.result, aging.due, as_of),
            )
        )
    rows.sort(key=lambda r: (r.due, r.sample))

    overall = {status: 0 for status in STATUS_ORDER}
    for row in rows:
        overall[row.status] += 1

    groups: dict[tuple[str, str], list[AgingRow]] = {}
    for row in rows:
        groups.setdefault((row.lot, row.pattern), []).append(row)
    summaries: list[GroupSummary] = []
    for key in sorted(groups):
        group_rows = groups[key]
        counts = {status: 0 for status in STATUS_ORDER}
        for row in group_rows:
            counts[row.status] += 1
        summaries.append(
            GroupSummary(
                key=key,
                total=len(group_rows),
                counts=counts,
                unresolved_samples=tuple(
                    sorted(r.sample for r in group_rows if r.status in UNRESOLVED)
                ),
                bad_samples=tuple(
                    sorted(r.sample for r in group_rows if r.status == MATURE_BAD)
                ),
            )
        )

    # 结论适用范围：该格子没有任何未决项（全部成熟且有结论，无论合格与否）。
    applicable = tuple(
        g.key for g in summaries if not g.unresolved_samples and g.total > 0
    )
    pending = tuple(g.key for g in summaries if g.unresolved_samples)
    return AgingBoard(
        as_of=as_of,
        rows=tuple(rows),
        overall_counts=overall,
        by_lot_pattern=tuple(summaries),
        applicable_scope=applicable,
        pending_scope=pending,
    )
