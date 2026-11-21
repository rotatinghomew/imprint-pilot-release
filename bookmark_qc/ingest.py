"""新复查结果入口与判断变化追踪。

后续出现的老化结果、批次级异常或返工复查读数通过同一个入口进入项目，
入口返回**装载了新事实的新项目**（原项目不就地修改）与一份变化报告：

* 改变了哪条判断（老化状态迁移、新增批次异常、新增返工判读）；
* 哪张旧放行单需要通知（按证据链反查，已交付与未交付分别标注）；
* 哪些款仍然不能从这批结果推断能否销售（与新事实不在同一条证据链上的款）。

入口只接受与原资料同构的增量记录，仍然执行完整资料预检。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from .aging import AgingBoard, aging_board
from .model import Project, load_project
from .release import release_report
from .rework import rework_records

AGING_CHANGE = "老化状态变化"
RECHECK_CHANGE = "新增批次级异常"
REWORK_CHANGE = "新增返工复查判读"


@dataclass(frozen=True)
class ChangeEntry:
    change_type: str
    subject: str
    before: str | None
    after: str
    judgment: str
    permission_ids: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "change_type": self.change_type,
            "subject": self.subject,
            "before": self.before,
            "after": self.after,
            "judgment": self.judgment,
            "permission_ids": list(self.permission_ids),
        }


@dataclass(frozen=True)
class ChangeReport:
    as_of: date
    changes: tuple[ChangeEntry, ...]
    notify_permissions: tuple[str, ...]
    sent_permissions: tuple[str, ...]
    held_permissions: tuple[str, ...]
    notice_only_permissions: tuple[str, ...]
    uninferrable_skus: tuple[str, ...]
    new_board: AgingBoard

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "changes": [c.to_dict() for c in self.changes],
            "notify_permissions": list(self.notify_permissions),
            "sent_permissions": list(self.sent_permissions),
            "held_permissions": list(self.held_permissions),
            "notice_only_permissions": list(self.notice_only_permissions),
            "uninferrable_skus": list(self.uninferrable_skus),
            "new_board": self.new_board.to_dict(),
        }


def _raw(project: Project) -> dict[str, Any]:
    """把项目还原成与 drill.json 同构的字典，以便追加增量后重新预检。"""
    return {
        "runs": [
            {
                "id": r.id,
                "lot": r.lot,
                "sheet": r.sheet,
                "pattern": r.pattern,
                "process": r.process,
                "assignment": r.assignment,
                "on": r.on.isoformat(),
            }
            for r in project.runs.values()
        ],
        "samples": [
            {"id": s.id, "run": s.run, "reserved": s.reserved}
            for s in project.samples.values()
        ],
        "readings": [
            {
                "sample": r.sample,
                "operation": r.operation,
                "repetition": r.repetition,
                "contrast": r.contrast,
                "process": r.process,
                "lot": r.lot,
            }
            for r in project.readings
        ],
        "aging": [
            {
                "sample": a.sample,
                "due": a.due.isoformat(),
                "inspected_on": a.inspected_on.isoformat() if a.inspected_on else None,
                "result": a.result,
            }
            for a in project.aging.values()
        ],
        "permission": [
            {
                "id": p.id,
                "sku": p.sku,
                "pattern": p.pattern,
                "process": p.process,
                "sample": p.sample,
                "evidence": p.evidence,
                "sent_to": p.sent_to,
            }
            for p in project.permission.values()
        ],
        "recheck": [
            {
                "id": c.id,
                "lot": c.lot,
                "discovered_on": c.discovered_on.isoformat(),
                "category": c.category,
            }
            for c in project.recheck.values()
        ],
        "test_hours": [
            {"date": d.isoformat(), "capacity": th.capacity}
            for d, th in sorted(project.test_hours.items())
        ],
        "assays": [
            {
                "id": a.id,
                "sample": a.sample,
                "purpose": a.purpose,
                "sku": a.sku,
                "hours": a.hours,
                "consumes": a.consumes,
            }
            for a in project.assays.values()
        ],
        "committed": [
            {
                "assay": c.assay,
                "date": c.date.isoformat(),
                "confirmed_on": c.confirmed_on.isoformat(),
            }
            for c in project.committed.values()
        ],
    }


def _permissions_using_sample(project: Project, sample_id: str) -> tuple[str, ...]:
    return tuple(
        sorted(p.id for p in project.permission.values() if p.sample == sample_id)
    )


def ingest_results(
    project: Project,
    aging_updates: list[dict[str, Any]] | None = None,
    new_rechecks: list[dict[str, Any]] | None = None,
    new_readings: list[dict[str, Any]] | None = None,
    as_of: date | None = None,
) -> tuple[Project, ChangeReport]:
    """把增量复查结果并入项目，返回新项目与变化报告。

    参数均为与来源表同构的行数组：

    * ``aging_updates``：行含 ``sample/due/inspected_on/result``，用于补登
      或更新该留样的老化结论；
    * ``new_rechecks``：行含 ``id/lot/discovered_on/category``；
    * ``new_readings``：行含 ``sample/operation/repetition/contrast/process/lot``，
      用于新到的返工复查读数（``operation>1``）。
    """
    aging_updates = aging_updates or []
    new_rechecks = new_rechecks or []
    new_readings = new_readings or []

    raw = _raw(project)
    old_rework = {(r.sample, r.operation): r.verdict for r in rework_records(project)}

    # 老化：同一样品以新结论替换旧行（仍保留 due）。
    aging_index = {row["sample"]: row for row in raw["aging"]}
    for row in aging_updates:
        aging_index[row["sample"]] = dict(row)
    raw["aging"] = list(aging_index.values())

    for row in new_rechecks:
        if row["id"] in {r["id"] for r in raw["recheck"]}:
            raise ValueError(f"批次异常编号冲突：{row['id']}")
        raw["recheck"].append(dict(row))

    for row in new_readings:
        raw["readings"].append(dict(row))

    new_project = load_project(raw)
    # 新旧看板都按传入的同一看板日评估；只有增量实际触及的样品才报告变化，
    # 单纯由看板日滚动造成的到期状态迁移不记入“新结果改变的判断”。
    new_board = aging_board(new_project, as_of)
    old_board_at_asof = aging_board(project, as_of)
    effective_as_of = new_board.as_of

    touched_aging = {row["sample"] for row in aging_updates}
    touched_rework = {
        (row["sample"], int(row["operation"])) for row in new_readings
    }
    changes: list[ChangeEntry] = []

    # 1) 老化状态迁移（仅增量补登/更新的样品）
    old_status = {row.sample: row.status for row in old_board_at_asof.rows}
    for row in new_board.rows:
        if row.sample not in touched_aging:
            continue
        before = old_status.get(row.sample)
        if before != row.status:
            linked = _permissions_using_sample(project, row.sample)
            if row.status == "成熟不合格":
                judgment = f"{row.sample} 老化成熟且不合格，相关放行证据需要重新评估"
            elif row.status == "留样失联":
                judgment = f"{row.sample} 留样失联，无法取得事实，相关保留不能解除"
            elif row.status == "逾期未检":
                judgment = f"{row.sample} 已到期未检，是证据缺口而非合格"
            elif row.status == "成熟合格":
                judgment = f"{row.sample} 老化成熟且合格，可支持其证据链上的结论"
            else:
                judgment = f"{row.sample} 状态迁移为{row.status}"
            changes.append(
                ChangeEntry(
                    change_type=AGING_CHANGE,
                    subject=row.sample,
                    before=before,
                    after=row.status,
                    judgment=judgment,
                    permission_ids=linked,
                )
            )

    # 2) 新增批次级异常
    for row in new_rechecks:
        recheck = new_project.recheck[row["id"]]
        linked = tuple(
            sorted(
                p.id
                for p in new_project.permission.values()
                if new_project.run_of_sample(p.sample).lot == recheck.lot
            )
        )
        changes.append(
            ChangeEntry(
                change_type=RECHECK_CHANGE,
                subject=recheck.id,
                before=None,
                after=f"{recheck.lot}/{recheck.category}",
                judgment=(
                    f"批次 {recheck.lot} 出现{recheck.category}，共享试产证据链上的"
                    "放行单进入保留，独立复验不受自动影响"
                ),
                permission_ids=linked,
            )
        )

    # 3) 新增返工复查判读（仅增量带来的工序）
    for record in rework_records(new_project):
        key = (record.sample, record.operation)
        if key not in old_rework and key in touched_rework:
            changes.append(
                ChangeEntry(
                    change_type=REWORK_CHANGE,
                    subject=f"{record.sample}#op{record.operation}",
                    before=None,
                    after=record.verdict,
                    judgment=(
                        f"{record.change_scope}，对比度变化 {record.delta:+.2f}，"
                        f"判读：{record.verdict}"
                    ),
                    permission_ids=_permissions_using_sample(project, record.sample),
                )
            )

    # 放行影响在新项目上重算，决定通知名单与仍不可推断的款。
    new_release = release_report(new_project)
    notify: set[str] = set()
    sent: set[str] = set()
    held: set[str] = set()
    for entry in changes:
        notify.update(entry.permission_ids)
    for impact in new_release.impacts:
        for item in impact.holds:
            if item.permission_id in notify:
                held.add(item.permission_id)
        for item in impact.trace_scope:
            if item.permission_id in notify:
                sent.add(item.permission_id)
    # 需通知但未进保留/追查的（如独立复验单）：仅通知，不自动改变其放行状态。
    notice_only = tuple(sorted(notify - sent - held))

    all_skus = {p.sku for p in new_project.permission.values()}
    connected_skus = {
        new_project.permission[pid].sku
        for pid in notify
        if pid in new_project.permission
    }
    uninferrable = tuple(sorted(all_skus - connected_skus))

    return new_project, ChangeReport(
        as_of=effective_as_of,
        changes=tuple(changes),
        notify_permissions=tuple(sorted(notify)),
        sent_permissions=tuple(sorted(sent)),
        held_permissions=tuple(sorted(held)),
        notice_only_permissions=notice_only,
        uninferrable_skus=uninferrable,
        new_board=new_board,
    )
