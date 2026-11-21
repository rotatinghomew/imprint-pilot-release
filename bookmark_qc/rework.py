"""同件原样返工/换料复查的判读。

读数表里 ``operation=1`` 是原加工，后续序号是同一件留样的返工或换料
复查。判读时把每次复查与该样品的上一道工序比较：

* 只换工艺、批次不变且对比度回升 → **工艺改善**；
* 只换批次（料）、工艺不变且对比度回升 → **换料掩盖问题**；
* 工艺与批次同时变化 → **仍无法区分**，改善不能单独归因；
* 两者都没变 → 无实质变更，只是重复测量，不构成新证据。

对比度（contrast）越高表示印纹越实；改善方向以上一道工序的均值为基线，
同时给出数值差，负责人可自行结合仪表阈值判断。
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

from .model import ORIGINAL_OPERATION, Project, Reading

PROCESS_ONLY = "仅工艺变化"
MATERIAL_ONLY = "仅换料"
BOTH_CHANGED = "工艺与换料同时变化"
NO_CHANGE = "工艺与批次均未变化"

IMPROVED = "改善"
FLAT = "持平"
WORSENED = "恶化"

VERDICT_PROCESS = "工艺改善"
VERDICT_MATERIAL = "换料掩盖问题"
VERDICT_AMBIGUOUS = "仍无法区分"
VERDICT_NO_CHANGE = "无实质变更，不构成新证据"


@dataclass(frozen=True)
class ReworkRecord:
    sample: str
    operation: int
    baseline_operation: int
    baseline_process: str
    baseline_lot: str
    rework_process: str
    rework_lot: str
    change_scope: str
    baseline_mean: float
    rework_mean: float
    delta: float
    direction: str
    verdict: str
    aging_result: str | None

    def to_dict(self) -> dict:
        return {
            "sample": self.sample,
            "operation": self.operation,
            "baseline_operation": self.baseline_operation,
            "baseline_process": self.baseline_process,
            "baseline_lot": self.baseline_lot,
            "rework_process": self.rework_process,
            "rework_lot": self.rework_lot,
            "change_scope": self.change_scope,
            "baseline_mean": round(self.baseline_mean, 4),
            "rework_mean": round(self.rework_mean, 4),
            "delta": round(self.delta, 4),
            "direction": self.direction,
            "verdict": self.verdict,
            "aging_result": self.aging_result,
        }


def _operation_mean(rows: list[Reading]) -> tuple[float, str, str]:
    processes = {r.process for r in rows}
    lots = {r.lot for r in rows}
    if len(processes) != 1 or len(lots) != 1:
        raise ValueError(
            f"样品 {rows[0].sample} 工序 {rows[0].operation} 内工艺/批次不一致"
        )
    return mean(r.contrast for r in rows), next(iter(processes)), next(iter(lots))


def _scope(process_changed: bool, lot_changed: bool) -> str:
    if process_changed and lot_changed:
        return BOTH_CHANGED
    if process_changed:
        return PROCESS_ONLY
    if lot_changed:
        return MATERIAL_ONLY
    return NO_CHANGE


def _verdict(scope: str, direction: str) -> str:
    if scope == NO_CHANGE:
        return VERDICT_NO_CHANGE
    if scope == BOTH_CHANGED:
        return VERDICT_AMBIGUOUS
    if scope == PROCESS_ONLY:
        return VERDICT_PROCESS if direction == IMPROVED else f"工艺调整但{direction}，未证实改善"
    # 仅换料
    if direction == IMPROVED:
        return VERDICT_MATERIAL
    return f"换料后{direction}，问题未被掩盖也未解决"


def rework_records(project: Project) -> list[ReworkRecord]:
    """全部返工/换料复查记录，按样品与工序号排序。"""
    out: list[ReworkRecord] = []
    sample_ids = sorted({r.sample for r in project.readings if r.operation > ORIGINAL_OPERATION})
    for sample_id in sample_ids:
        ops = sorted({r.operation for r in project.readings_of(sample_id)})
        for operation in ops:
            if operation == ORIGINAL_OPERATION:
                continue
            baseline_op = operation - 1
            base_rows = project.readings_of(sample_id, baseline_op)
            new_rows = project.readings_of(sample_id, operation)
            base_mean, base_process, base_lot = _operation_mean(base_rows)
            new_mean, new_process, new_lot = _operation_mean(new_rows)
            process_changed = new_process != base_process
            lot_changed = new_lot != base_lot
            scope = _scope(process_changed, lot_changed)
            delta = new_mean - base_mean
            direction = IMPROVED if delta > 0 else WORSENED if delta < 0 else FLAT
            aging = project.aging.get(sample_id)
            out.append(
                ReworkRecord(
                    sample=sample_id,
                    operation=operation,
                    baseline_operation=baseline_op,
                    baseline_process=base_process,
                    baseline_lot=base_lot,
                    rework_process=new_process,
                    rework_lot=new_lot,
                    change_scope=scope,
                    baseline_mean=base_mean,
                    rework_mean=new_mean,
                    delta=delta,
                    direction=direction,
                    verdict=_verdict(scope, direction),
                    aging_result=aging.result if aging else None,
                )
            )
    return out
