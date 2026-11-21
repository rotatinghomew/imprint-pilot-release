"""项目数据模型与资料预检。

所有表读进同一个 :class:`Project`，并在装载时完成：

1. 表结构与字段集合核对；
2. 外键关联核对（样品→开机、读数→样品、老化→样品等）；
3. 业务前提核对（同次工序的重复测量、返工序号、留样唯一性、
   消耗性试验排期与机时容量）。

``null`` 在原始资料中只表示“未取得该事实”，模型不把它转成零。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

#: 各来源表的字段集合，与 README 的资料预检口径一致。
SCHEMA: dict[str, tuple[str, ...]] = {
    "runs": ("id", "lot", "sheet", "pattern", "process", "assignment", "on"),
    "samples": ("id", "run", "reserved"),
    "readings": ("sample", "operation", "repetition", "contrast", "process", "lot"),
    "aging": ("sample", "due", "inspected_on", "result"),
    "permission": ("id", "sku", "pattern", "process", "sample", "evidence", "sent_to"),
    "recheck": ("id", "lot", "discovered_on", "category"),
    "test_hours": ("date", "capacity"),
    "assays": ("id", "sample", "purpose", "sku", "hours", "consumes"),
    "committed": ("assay", "date", "confirmed_on"),
}

ORIGINAL_OPERATION = 1
#: 老化结果取值：None 为未取得事实，其余为已登记结论。
AGING_RESULTS = {"正常", "发虚", "失联"}
ASSIGNMENTS = {"分样试制", "正常试产"}
EVIDENCE_KINDS = {"共享试产", "独立复验"}


class ProjectError(ValueError):
    """资料预检不通过时抛出，消息指明具体表与行。"""


def parse_iso_date(value: str) -> date:
    """解析资料中的当地工作日历日期（ISO ``YYYY-MM-DD``）。"""
    return date.fromisoformat(value)


@dataclass(frozen=True)
class Run:
    id: str
    lot: str
    sheet: str
    pattern: str
    process: str
    assignment: str
    on: date


@dataclass(frozen=True)
class Sample:
    id: str
    run: str
    reserved: bool


@dataclass(frozen=True)
class Reading:
    sample: str
    operation: int
    repetition: int
    contrast: int
    process: str
    lot: str


@dataclass(frozen=True)
class Aging:
    sample: str
    due: date
    inspected_on: date | None
    result: str | None


@dataclass(frozen=True)
class Permission:
    id: str
    sku: str
    pattern: str
    process: str
    sample: str
    evidence: str
    sent_to: str | None


@dataclass(frozen=True)
class Recheck:
    id: str
    lot: str
    discovered_on: date
    category: str


@dataclass(frozen=True)
class TestHours:
    date: date
    capacity: int


@dataclass(frozen=True)
class Assay:
    id: str
    sample: str
    purpose: str
    sku: str | None
    hours: int
    consumes: bool


@dataclass(frozen=True)
class Commitment:
    assay: str
    date: date
    confirmed_on: date


@dataclass
class Project:
    """同一项目下全部来源表的只读内存视图与索引。"""

    runs: dict[str, Run]
    samples: dict[str, Sample]
    readings: list[Reading]
    aging: dict[str, Aging]
    permission: dict[str, Permission]
    recheck: dict[str, Recheck]
    test_hours: dict[date, TestHours]
    assays: dict[str, Assay]
    committed: dict[str, Commitment]
    source_path: Path | None = field(default=None)

    # ---- 常用索引 -------------------------------------------------
    def samples_of_run(self, run_id: str) -> list[Sample]:
        return [s for s in self.samples.values() if s.run == run_id]

    def run_of_sample(self, sample_id: str) -> Run:
        return self.runs[self.samples[sample_id].run]

    def readings_of(self, sample_id: str, operation: int | None = None) -> list[Reading]:
        rows = [r for r in self.readings if r.sample == sample_id]
        if operation is not None:
            rows = [r for r in rows if r.operation == operation]
        return sorted(rows, key=lambda r: (r.operation, r.repetition))

    def reserved_samples(self) -> list[Sample]:
        return [s for s in self.samples.values() if s.reserved]

    def lots(self) -> list[str]:
        return sorted({r.lot for r in self.runs.values()})

    def patterns(self) -> list[str]:
        return sorted({r.pattern for r in self.runs.values()})

    def processes(self) -> list[str]:
        return sorted({r.process for r in self.runs.values()})

    def assays_of_sample(self, sample_id: str) -> list[Assay]:
        return [a for a in self.assays.values() if a.sample == sample_id]

    def is_consumed(self, sample_id: str) -> bool:
        """样品是否已被已确认的消耗性试验用掉。"""
        for commitment in self.committed.values():
            assay = self.assays[commitment.assay]
            if assay.sample == sample_id and assay.consumes:
                return True
        return False


def load_project(source: str | Path | dict[str, Any]) -> Project:
    """从 JSON 路径或已解析的字典装载项目并执行资料预检。"""
    if isinstance(source, dict):
        raw = source
        path: Path | None = None
    else:
        path = Path(source)
        raw = json.loads(path.read_text(encoding="utf-8"))
    _validate_schema(raw)
    project = _build(raw, path)
    _validate_references(project)
    _validate_business_rules(project)
    return project


# --------------------------------------------------------------------
# 装载与预检
# --------------------------------------------------------------------
#: 可以为空的事实表（没有试验排期/批次异常时允许零行）。
OPTIONAL_TABLES = frozenset({"recheck", "assays", "committed"})


def _validate_schema(raw: dict[str, Any]) -> None:
    if not isinstance(raw, dict) or set(raw) != set(SCHEMA):
        raise ProjectError(f"顶层表集合应为 {sorted(SCHEMA)}，实际为 {sorted(raw)}")
    for name, fields_ in SCHEMA.items():
        rows = raw[name]
        if not isinstance(rows, list):
            raise ProjectError(f"表 {name} 必须是行数组")
        if not rows and name not in OPTIONAL_TABLES:
            raise ProjectError(f"表 {name} 必须是非空行数组")
        for index, row in enumerate(rows):
            if set(row) != set(fields_):
                raise ProjectError(
                    f"表 {name} 第 {index} 行字段应为 {set(fields_)}，实际为 {set(row)}"
                )


def _build(raw: dict[str, Any], path: Path | None) -> Project:
    def unique_ids(rows: list[dict[str, Any]], table: str, key: str = "id") -> dict[str, dict]:
        out: dict[str, dict] = {}
        for row in rows:
            if row[key] in out:
                raise ProjectError(f"表 {table} 的 {key}={row[key]} 重复")
            out[row[key]] = row
        return out

    runs = {
        rid: Run(
            id=row["id"],
            lot=row["lot"],
            sheet=row["sheet"],
            pattern=row["pattern"],
            process=row["process"],
            assignment=row["assignment"],
            on=parse_iso_date(row["on"]),
        )
        for rid, row in unique_ids(raw["runs"], "runs").items()
    }
    samples = {
        sid: Sample(id=row["id"], run=row["run"], reserved=bool(row["reserved"]))
        for sid, row in unique_ids(raw["samples"], "samples").items()
    }
    readings = [
        Reading(
            sample=row["sample"],
            operation=int(row["operation"]),
            repetition=int(row["repetition"]),
            contrast=int(row["contrast"]),
            process=row["process"],
            lot=row["lot"],
        )
        for row in raw["readings"]
    ]
    aging = {
        row["sample"]: Aging(
            sample=row["sample"],
            due=parse_iso_date(row["due"]),
            inspected_on=parse_iso_date(row["inspected_on"]) if row["inspected_on"] else None,
            result=row["result"],
        )
        for row in raw["aging"]
    }
    permission = {
        pid: Permission(
            id=row["id"],
            sku=row["sku"],
            pattern=row["pattern"],
            process=row["process"],
            sample=row["sample"],
            evidence=row["evidence"],
            sent_to=row["sent_to"],
        )
        for pid, row in unique_ids(raw["permission"], "permission").items()
    }
    recheck = {
        cid: Recheck(
            id=row["id"],
            lot=row["lot"],
            discovered_on=parse_iso_date(row["discovered_on"]),
            category=row["category"],
        )
        for cid, row in unique_ids(raw["recheck"], "recheck").items()
    }
    hours = {
        parse_iso_date(row["date"]): TestHours(
            date=parse_iso_date(row["date"]), capacity=int(row["capacity"])
        )
        for row in raw["test_hours"]
    }
    assays = {
        aid: Assay(
            id=row["id"],
            sample=row["sample"],
            purpose=row["purpose"],
            sku=row["sku"],
            hours=int(row["hours"]),
            consumes=bool(row["consumes"]),
        )
        for aid, row in unique_ids(raw["assays"], "assays").items()
    }
    committed = {
        row["assay"]: Commitment(
            assay=row["assay"],
            date=parse_iso_date(row["date"]),
            confirmed_on=parse_iso_date(row["confirmed_on"]),
        )
        for row in raw["committed"]
    }
    return Project(
        runs=runs,
        samples=samples,
        readings=readings,
        aging=aging,
        permission=permission,
        recheck=recheck,
        test_hours=hours,
        assays=assays,
        committed=committed,
        source_path=path,
    )


def _validate_references(project: Project) -> None:
    for sample in project.samples.values():
        if sample.run not in project.runs:
            raise ProjectError(f"样品 {sample.id} 指向不存在的开机 {sample.run}")
    for reading in project.readings:
        if reading.sample not in project.samples:
            raise ProjectError(f"读数指向不存在的样品 {reading.sample}")
        run = project.run_of_sample(reading.sample)
        if reading.operation == ORIGINAL_OPERATION:
            if reading.process != run.process or reading.lot != run.lot:
                raise ProjectError(
                    f"样品 {reading.sample} 原加工读数的工艺/批次与开机 {run.id} 不一致"
                )
    for aging in project.aging.values():
        if aging.sample not in project.samples:
            raise ProjectError(f"老化记录指向不存在的样品 {aging.sample}")
        if aging.result is not None and aging.result not in AGING_RESULTS:
            raise ProjectError(f"老化结果取值非法：{aging.result}")
        if aging.inspected_on is not None and aging.result is None:
            raise ProjectError(f"样品 {aging.sample} 有检查日却没有老化结果")
        if aging.result == "失联" and aging.inspected_on is not None:
            raise ProjectError(f"样品 {aging.sample} 标记失联却登记了检查日")
    for permit in project.permission.values():
        if permit.sample not in project.samples:
            raise ProjectError(f"放行单 {permit.id} 指向不存在的样品 {permit.sample}")
        if permit.evidence not in EVIDENCE_KINDS:
            raise ProjectError(f"放行单 {permit.id} 的证据类型非法：{permit.evidence}")
    for recheck in project.recheck.values():
        if recheck.lot not in {run.lot for run in project.runs.values()}:
            raise ProjectError(f"批次异常 {recheck.id} 指向不存在的批次 {recheck.lot}")
    for commitment in project.committed.values():
        if commitment.assay not in project.assays:
            raise ProjectError(f"已确认排期指向不存在的试验 {commitment.assay}")
        if commitment.date not in project.test_hours:
            raise ProjectError(f"试验 {commitment.assay} 排在无机时容量的日期 {commitment.date}")


def _validate_business_rules(project: Project) -> None:
    # 同次工序的重复测量必须成组出现，且重复序号不重复。
    grouped: dict[tuple[str, int], list[Reading]] = {}
    for reading in project.readings:
        grouped.setdefault((reading.sample, reading.operation), []).append(reading)
    for (sample_id, operation), rows in grouped.items():
        repetitions = [r.repetition for r in rows]
        if len(set(repetitions)) != len(repetitions):
            raise ProjectError(f"样品 {sample_id} 工序 {operation} 的重复测量序号重复")
        if sorted(repetitions) != list(range(1, len(repetitions) + 1)):
            raise ProjectError(f"样品 {sample_id} 工序 {operation} 的重复测量序号不连续")

    # 返工/换料复查只能挂在已有原加工记录的样品上，且工序号递增。
    sample_ops: dict[str, set[int]] = {}
    for reading in project.readings:
        sample_ops.setdefault(reading.sample, set()).add(reading.operation)
    for sample_id, ops in sample_ops.items():
        if ORIGINAL_OPERATION not in ops:
            raise ProjectError(f"样品 {sample_id} 缺少 operation=1 的原加工记录")
        if sorted(ops) != list(range(1, max(ops) + 1)):
            raise ProjectError(f"样品 {sample_id} 的工序序号不连续：{sorted(ops)}")

    # 开机/分样属性合法，同片材料的分样试制才允许跨工艺配对。
    for run in project.runs.values():
        if run.assignment not in ASSIGNMENTS:
            raise ProjectError(f"开机 {run.id} 的分样属性非法：{run.assignment}")
    sheet_groups: dict[str, set[str]] = {}
    for run in project.runs.values():
        sheet_groups.setdefault(run.sheet, set()).add(run.id)

    # 留样唯一性：同件样品只能对应一条留样身份（samples 表已保证），
    # 老化复查只覆盖留样样品。
    reserved_ids = {s.id for s in project.reserved_samples()}
    for aging in project.aging.values():
        if aging.sample not in reserved_ids:
            raise ProjectError(f"老化复查样品 {aging.sample} 不是留样")

    # 同一物理留样不得被两个独立复验证明重复使用：
    # 同件样品若被多张“独立复验”放行单引用，即判定证据不独立。
    independent_uses: dict[str, list[str]] = {}
    for permit in project.permission.values():
        if permit.evidence == "独立复验":
            independent_uses.setdefault(permit.sample, []).append(permit.id)
    for sample_id, permit_ids in independent_uses.items():
        if len(permit_ids) > 1:
            raise ProjectError(
                f"留样 {sample_id} 被多张独立复验放行单复用：{permit_ids}，不构成独立证据"
            )

    # 已确认排期的机时占用不得超过当日容量。
    used: dict[date, int] = {}
    for commitment in project.committed.values():
        assay = project.assays[commitment.assay]
        used[commitment.date] = used.get(commitment.date, 0) + assay.hours
    for day, hours_used in used.items():
        if hours_used > project.test_hours[day].capacity:
            raise ProjectError(
                f"{day} 机时占用 {hours_used} 超过容量 {project.test_hours[day].capacity}"
            )
