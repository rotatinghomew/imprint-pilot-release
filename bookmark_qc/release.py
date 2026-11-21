"""批次级异常后的放行追溯与保留范围。

证据链按放行单逐条还原：

* **共享试产证据**：同一件留样被多张放行单引用时，它们共享同一条物理
  证据链，一旦该证据落在异常批次上，所有引用它的放行单都进入保留；
* **独立复验证据**：必须来自未被共享使用的独立留样。批次级异常不自动
  推翻独立复验，但独立留样自身出现成熟不合格时仍然失效。

已经交给柜台（``sent_to`` 非空）的放行单只列出追查范围，状态保持
“已放行”，绝不回填成从未放行；尚未交付的则暂停新放行。
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import Project, Permission, Run

SHARED = "共享试产"
INDEPENDENT = "独立复验"

HOLD_NEW = "暂停新放行"
TRACE_SENT = "已交付，列入追查范围"
UNAFFECTED = "独立复验支持，不受本次批次异常影响"
INVALID_OWN_DEFECT = "独立留样自身成熟不合格，证据失效"

WARNING_SCOPE = "放行单纹路/工艺与引用留样的开机事实不一致，证据适用范围存疑"


@dataclass(frozen=True)
class EvidenceChain:
    sample: str
    run: str
    lot: str
    sheet: str
    pattern: str
    process: str
    evidence: str
    used_by_permissions: tuple[str, ...]
    shared: bool
    independent: bool
    aging_status: str | None

    def to_dict(self) -> dict:
        return {
            "sample": self.sample,
            "run": self.run,
            "lot": self.lot,
            "sheet": self.sheet,
            "pattern": self.pattern,
            "process": self.process,
            "evidence": self.evidence,
            "used_by_permissions": list(self.used_by_permissions),
            "shared": self.shared,
            "independent": self.independent,
            "aging_status": self.aging_status,
        }


@dataclass(frozen=True)
class PermissionImpact:
    permission_id: str
    sku: str
    pattern: str
    process: str
    sample: str
    evidence: str
    sent_to: str | None
    delivered: bool
    action: str
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "permission_id": self.permission_id,
            "sku": self.sku,
            "pattern": self.pattern,
            "process": self.process,
            "sample": self.sample,
            "evidence": self.evidence,
            "sent_to": self.sent_to,
            "delivered": self.delivered,
            "action": self.action,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class RecheckImpact:
    recheck_id: str
    lot: str
    category: str
    discovered_on: str
    holds: tuple[PermissionImpact, ...]
    unaffected: tuple[PermissionImpact, ...]
    trace_scope: tuple[PermissionImpact, ...]
    paused_skus: tuple[str, ...]
    unaffected_skus: tuple[str, ...]
    trace_skus: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "recheck_id": self.recheck_id,
            "lot": self.lot,
            "category": self.category,
            "discovered_on": self.discovered_on,
            "holds": [p.to_dict() for p in self.holds],
            "unaffected": [p.to_dict() for p in self.unaffected],
            "trace_scope": [p.to_dict() for p in self.trace_scope],
            "paused_skus": list(self.paused_skus),
            "unaffected_skus": list(self.unaffected_skus),
            "trace_skus": list(self.trace_skus),
        }


@dataclass(frozen=True)
class ReleaseReport:
    impacts: tuple[RecheckImpact, ...]
    evidence_chains: tuple[EvidenceChain, ...]
    applicability_warnings: tuple[str, ...]
    scope_note: str

    def to_dict(self) -> dict:
        return {
            "impacts": [i.to_dict() for i in self.impacts],
            "evidence_chains": [c.to_dict() for c in self.evidence_chains],
            "applicability_warnings": list(self.applicability_warnings),
            "scope_note": self.scope_note,
        }


def _run_of_permission(project: Project, permit: Permission) -> Run:
    return project.run_of_sample(permit.sample)


def evidence_chains(project: Project) -> list[EvidenceChain]:
    """还原每张放行单引用留样的物理证据链与共享关系。"""
    usage: dict[str, list[str]] = {}
    for permit in project.permission.values():
        usage.setdefault(permit.sample, []).append(permit.id)
    chains: list[EvidenceChain] = []
    seen: set[str] = set()
    for permit in project.permission.values():
        sample_id = permit.sample
        if sample_id in seen:
            continue
        seen.add(sample_id)
        run = _run_of_permission(project, permit)
        aging = project.aging.get(sample_id)
        chains.append(
            EvidenceChain(
                sample=sample_id,
                run=run.id,
                lot=run.lot,
                sheet=run.sheet,
                pattern=run.pattern,
                process=run.process,
                evidence=permit.evidence,
                used_by_permissions=tuple(sorted(usage[sample_id])),
                shared=len(usage[sample_id]) > 1 or permit.evidence == SHARED,
                independent=permit.evidence == INDEPENDENT,
                aging_status=aging.result if aging else None,
            )
        )
    return sorted(chains, key=lambda c: c.sample)


def _scope_warnings(project: Project, permit: Permission) -> list[str]:
    run = _run_of_permission(project, permit)
    warnings: list[str] = []
    if permit.pattern != run.pattern:
        warnings.append(
            f"放行单 {permit.id} 声明纹路「{permit.pattern}」与留样 {permit.sample} "
            f"实际纹路「{run.pattern}」不一致"
        )
    if permit.process != run.process:
        warnings.append(
            f"放行单 {permit.id} 声明工艺「{permit.process}」与留样 {permit.sample} "
            f"实际工艺「{run.process}」不一致"
        )
    return warnings


def _impact_for(
    project: Project, permit: Permission, affected_lots: set[str]
) -> PermissionImpact:
    run = _run_of_permission(project, permit)
    aging = project.aging.get(permit.sample)
    aging_bad = aging is not None and aging.result not in (None, "正常", "失联")
    warnings = _scope_warnings(project, permit)
    if warnings:
        warnings.append(WARNING_SCOPE)
    reasons: list[str] = []

    if permit.evidence == INDEPENDENT:
        if aging_bad:
            action = INVALID_OWN_DEFECT
            reasons.append(f"独立留样 {permit.sample} 老化结论为「{aging.result}」")
        else:
            action = UNAFFECTED
            reasons.append(
                f"独立复验留样来自批次 {run.lot}，与异常批次 "
                f"{sorted(affected_lots)} 无共享证据链"
            )
        return PermissionImpact(
            permission_id=permit.id,
            sku=permit.sku,
            pattern=permit.pattern,
            process=permit.process,
            sample=permit.sample,
            evidence=permit.evidence,
            sent_to=permit.sent_to,
            delivered=permit.sent_to is not None,
            action=action,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
        )

    # 共享试产证据：证据链落在异常批次即进入保留
    if run.lot in affected_lots:
        reasons.append(
            f"共享试产留样 {permit.sample} 来自异常批次 {run.lot}，"
            "同件留样不能支持互相独立的复验证明"
        )
        if permit.sent_to is not None:
            action = TRACE_SENT
            reasons.append(f"样品已交付柜台/渠道：{permit.sent_to}，仅追查不回填")
        else:
            action = HOLD_NEW
            reasons.append("放行单尚未交付，暂停新放行")
    else:
        action = UNAFFECTED
        reasons.append(f"共享证据来自批次 {run.lot}，不在异常批次 {sorted(affected_lots)} 内")
    return PermissionImpact(
        permission_id=permit.id,
        sku=permit.sku,
        pattern=permit.pattern,
        process=permit.process,
        sample=permit.sample,
        evidence=permit.evidence,
        sent_to=permit.sent_to,
        delivered=permit.sent_to is not None,
        action=action,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
    )


def release_report(project: Project) -> ReleaseReport:
    """汇总全部批次级异常对放行单的影响与追查范围。"""
    impacts: list[RecheckImpact] = []
    for recheck in sorted(project.recheck.values(), key=lambda c: c.discovered_on):
        affected = {recheck.lot}
        perms = [
            _impact_for(project, permit, affected)
            for permit in project.permission.values()
        ]
        holds = tuple(p for p in perms if p.action == HOLD_NEW)
        traces = tuple(p for p in perms if p.action == TRACE_SENT)
        unaffected = tuple(
            p for p in perms if p.action in (UNAFFECTED, INVALID_OWN_DEFECT)
        )
        impacts.append(
            RecheckImpact(
                recheck_id=recheck.id,
                lot=recheck.lot,
                category=recheck.category,
                discovered_on=recheck.discovered_on.isoformat(),
                holds=holds,
                unaffected=unaffected,
                trace_scope=traces,
                paused_skus=tuple(sorted({p.sku for p in holds})),
                unaffected_skus=tuple(sorted({p.sku for p in unaffected})),
                trace_skus=tuple(sorted({p.sku for p in traces})),
            )
        )

    warnings: list[str] = []
    for permit in project.permission.values():
        warnings.extend(_scope_warnings(project, permit))

    scope = (
        "结论仅按各放行单引用留样所属批次与纹路适用；纹路/工艺不一致的放行单"
        "见 applicability_warnings，不能据其推断其他款。"
    )
    return ReleaseReport(
        impacts=tuple(impacts),
        evidence_chains=tuple(evidence_chains(project)),
        applicability_warnings=tuple(sorted(warnings)),
        scope_note=scope,
    )
