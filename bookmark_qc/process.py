"""工艺对比：压纹压力（改压）与标准工艺的可比分析。

防误判的两条硬规则落在本模块：

1. **同片材料切出的多件样品不是独立开机**。独立单元是开机记录
   （``runs`` 行），重复测量先在样品内取均值，再在开机内取均值；
   同片十件样品不能被当成十次独立开机。
2. **材料只在某个工艺出现时，差异不可归因于工艺**。只有同一坯料片
   （同批次、同片号、同纹路）上同时存在两种工艺的“分样试制”开机，
   才构成一个配对片；只在单一工艺出现的（批次 × 纹路）格子标记为
   无支持，不参与工艺差异结论。

“分样试制”（随机分样证据）与“正常试产”（含返工补测场景的常规证据）
分别统计，不混成同一种证据。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean

from .model import ORIGINAL_OPERATION, Project, Run

#: 对比结论中使用的证据类型标签。
SPLIT_EVIDENCE = "分样试制"
ROUTINE_EVIDENCE = "正常试产"


@dataclass(frozen=True)
class RunStat:
    """一次开机在原加工（operation=1）上的汇总。"""

    run_id: str
    lot: str
    sheet: str
    pattern: str
    process: str
    assignment: str
    sample_count: int
    repetition_count: int
    mean_contrast: float

    def to_dict(self) -> dict:
        return {
            "run": self.run_id,
            "lot": self.lot,
            "sheet": self.sheet,
            "pattern": self.pattern,
            "process": self.process,
            "assignment": self.assignment,
            "sample_count": self.sample_count,
            "repetition_count": self.repetition_count,
            "mean_contrast": round(self.mean_contrast, 4),
        }


@dataclass(frozen=True)
class PairedSheet:
    """同片材料上两种工艺各一次开机的配对。"""

    sheet: str
    lot: str
    pattern: str
    process_a: str
    process_b: str
    run_a: str
    run_b: str
    mean_a: float
    mean_b: float
    difference: float  # process_b - process_a

    def to_dict(self) -> dict:
        return {
            "sheet": self.sheet,
            "lot": self.lot,
            "pattern": self.pattern,
            "process_a": self.process_a,
            "process_b": self.process_b,
            "run_a": self.run_a,
            "run_b": self.run_b,
            "mean_a": round(self.mean_a, 4),
            "mean_b": round(self.mean_b, 4),
            "difference": round(self.difference, 4),
        }


@dataclass(frozen=True)
class StratumComparison:
    """（批次 × 纹路）格子上的工艺对比。"""

    lot: str
    pattern: str
    supported: bool
    reason: str
    processes: tuple[str, ...]
    paired_sheets: tuple[PairedSheet, ...]
    paired_difference_mean: float | None
    independent_run_counts: dict[str, int]
    routine_runs: tuple[RunStat, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "lot": self.lot,
            "pattern": self.pattern,
            "supported": self.supported,
            "reason": self.reason,
            "processes": list(self.processes),
            "paired_sheets": [p.to_dict() for p in self.paired_sheets],
            "paired_difference_mean": (
                round(self.paired_difference_mean, 4)
                if self.paired_difference_mean is not None
                else None
            ),
            "independent_run_counts": dict(self.independent_run_counts),
            "routine_runs": [r.to_dict() for r in self.routine_runs],
        }


@dataclass(frozen=True)
class ProcessReport:
    processes: tuple[str, ...]
    strata: tuple[StratumComparison, ...]
    supported_strata: tuple[tuple[str, str], ...]
    unsupported_strata: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict:
        return {
            "processes": list(self.processes),
            "strata": [s.to_dict() for s in self.strata],
            "supported_strata": [list(k) for k in self.supported_strata],
            "unsupported_strata": [list(k) for k in self.unsupported_strata],
        }


def run_statistics(project: Project, run: Run) -> RunStat:
    """按开机汇总：重复测量先在样品内取均值，再对该开机的样品取均值。

    只统计原加工（operation=1），返工补测由 :mod:`bookmark_qc.rework` 处理，
    二者不混入同一证据。
    """
    sample_means: list[float] = []
    repetition_count = 0
    samples = [s for s in project.samples_of_run(run.id)]
    for sample in samples:
        rows = project.readings_of(sample.id, ORIGINAL_OPERATION)
        if not rows:
            continue
        sample_means.append(mean(r.contrast for r in rows))
        repetition_count += len(rows)
    if not sample_means:
        raise ValueError(f"开机 {run.id} 没有原加工读数，无法汇总")
    return RunStat(
        run_id=run.id,
        lot=run.lot,
        sheet=run.sheet,
        pattern=run.pattern,
        process=run.process,
        assignment=run.assignment,
        sample_count=len(sample_means),
        repetition_count=repetition_count,
        mean_contrast=mean(sample_means),
    )


def all_run_statistics(project: Project) -> list[RunStat]:
    return [run_statistics(project, run) for run in project.runs.values()]


def _runs_by_sheet(project: Project) -> dict[str, list[Run]]:
    out: dict[str, list[Run]] = {}
    for run in project.runs.values():
        out.setdefault(run.sheet, []).append(run)
    return out


def paired_sheets(project: Project, process_a: str, process_b: str) -> list[PairedSheet]:
    """找出共同材料配对片：同片上分样试制、两工艺各一次开机。"""
    stats = {stat.run_id: stat for stat in all_run_statistics(project)}
    pairs: list[PairedSheet] = []
    for sheet, runs in sorted(_runs_by_sheet(project).items()):
        split = [r for r in runs if r.assignment == SPLIT_EVIDENCE]
        a = [r for r in split if r.process == process_a]
        b = [r for r in split if r.process == process_b]
        if not a or not b:
            continue
        if len(a) != 1 or len(b) != 1:
            raise ValueError(
                f"片材 {sheet} 上工艺配对不唯一：{[r.id for r in split]}"
            )
        run_a, run_b = a[0], b[0]
        if run_a.lot != run_b.lot or run_a.pattern != run_b.pattern:
            raise ValueError(f"片材 {sheet} 的配对开机批次或纹路不一致")
        stat_a, stat_b = stats[run_a.id], stats[run_b.id]
        pairs.append(
            PairedSheet(
                sheet=sheet,
                lot=run_a.lot,
                pattern=run_a.pattern,
                process_a=process_a,
                process_b=process_b,
                run_a=run_a.id,
                run_b=run_b.id,
                mean_a=stat_a.mean_contrast,
                mean_b=stat_b.mean_contrast,
                difference=stat_b.mean_contrast - stat_a.mean_contrast,
            )
        )
    return pairs


def compare_processes(project: Project, process_a: str, process_b: str) -> ProcessReport:
    """逐（批次 × 纹路）格子比较两种工艺。

    只有含至少一个共同材料配对片的格子才标记为 ``supported``；
    材料只在单一工艺出现的格子给出不可归因原因，不计算工艺差异。
    """
    known = set(project.processes())
    if process_a not in known or process_b not in known or process_a == process_b:
        raise ValueError(f"工艺取值非法：{process_a!r} vs {process_b!r}，可选 {sorted(known)}")

    pairs = paired_sheets(project, process_a, process_b)
    pairs_by_stratum: dict[tuple[str, str], list[PairedSheet]] = {}
    for pair in pairs:
        pairs_by_stratum.setdefault((pair.lot, pair.pattern), []).append(pair)

    stats = all_run_statistics(project)
    strata: list[StratumComparison] = []
    strata_keys = sorted({(r.lot, r.pattern) for r in project.runs.values()})
    for lot, pattern in strata_keys:
        cell_runs = [s for s in stats if s.lot == lot and s.pattern == pattern]
        processes_here = tuple(sorted({s.process for s in cell_runs}))
        independent_counts = {
            proc: len({s.run_id for s in cell_runs if s.process == proc})
            for proc in processes_here
        }
        routine = tuple(s for s in cell_runs if s.assignment == ROUTINE_EVIDENCE)
        cell_pairs = tuple(pairs_by_stratum.get((lot, pattern), []))
        if cell_pairs:
            reason = "同片分样配对支持"
            supported = True
            diff_mean: float | None = mean(p.difference for p in cell_pairs)
        else:
            supported = False
            diff_mean = None
            if len(processes_here) == 1:
                reason = (
                    f"材料只在{processes_here[0]}工艺出现，无共同材料对照，"
                    "差异不可归因于工艺"
                )
            else:
                reason = "两种工艺未在同片材料上分样配对，仅为跨片比较，不支持工艺归因"
        strata.append(
            StratumComparison(
                lot=lot,
                pattern=pattern,
                supported=supported,
                reason=reason,
                processes=processes_here,
                paired_sheets=cell_pairs,
                paired_difference_mean=diff_mean,
                independent_run_counts=independent_counts,
                routine_runs=routine,
            )
        )

    supported_keys = tuple((s.lot, s.pattern) for s in strata if s.supported)
    unsupported_keys = tuple((s.lot, s.pattern) for s in strata if not s.supported)
    return ProcessReport(
        processes=(process_a, process_b),
        strata=tuple(strata),
        supported_strata=supported_keys,
        unsupported_strata=unsupported_keys,
    )


def evidence_breakdown(project: Project) -> dict[str, list[RunStat]]:
    """按证据类型分开汇总：分样试制与正常试产不混成一种证据。"""
    out: dict[str, list[RunStat]] = {SPLIT_EVIDENCE: [], ROUTINE_EVIDENCE: []}
    for stat in all_run_statistics(project):
        out.setdefault(stat.assignment, []).append(stat)
    return out


def samples_per_sheet(project: Project) -> dict[str, int]:
    """每片材料切出的样品数，用于核对“同片多件≠多次独立开机”。"""
    out: dict[str, int] = {}
    for sample in project.samples.values():  # type: Iterable[Sample]
        run = project.runs[sample.run]
        out[run.sheet] = out.get(run.sheet, 0) + 1
    return dict(sorted(out.items()))
