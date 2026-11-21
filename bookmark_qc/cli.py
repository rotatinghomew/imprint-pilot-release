"""工坊统一入口的命令行界面（仅标准库）。

子命令：

* ``process``  工艺对比（共同材料+纹路支持才参与归因）
* ``rework``   同件原样返工复查判读
* ``aging``    老化看板（未到期/失联/成熟不合格分别显示）
* ``release``  批次异常后的放行追溯与保留范围
* ``planning`` 机时与留样受限下的方案对比
* ``ingest``   新复查结果入口，输出判断变化与通知名单
* ``board``    一页式总览
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .aging import STATUS_ORDER, aging_board
from .ingest import ingest_results
from .model import ProjectError, load_project, parse_iso_date
from .planning import build_planning_report
from .process import compare_processes
from .release import release_report
from .rework import rework_records


def _print_json(payload) -> None:
    if hasattr(payload, "to_dict"):
        payload = payload.to_dict()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))


def _emit(args, payload) -> None:
    _print_text(args.command, payload)


def _print_text(command: str, payload) -> None:
    printers = {
        "process": _print_process,
        "rework": _print_rework,
        "aging": _print_aging,
        "release": _print_release,
        "planning": _print_planning,
        "ingest": _print_ingest,
        "board": _print_board,
    }
    printers[command](payload)


# --------------------------------------------------------------------
# 文本视图
# --------------------------------------------------------------------
def _print_process(report) -> None:
    print(f"工艺对比：{report.processes[0]} vs {report.processes[1]}")
    print("（独立单元是开机；同片多件样品不重复计数，重复测量先在样品内取均值）")
    for stratum in report.strata:
        head = f"[{stratum.lot} × {stratum.pattern}] "
        if stratum.supported:
            print(
                f"{head}有支持（{len(stratum.paired_sheets)} 个同片配对），"
                f"配对差均值 {stratum.paired_difference_mean:+.2f}，"
                f"独立开机数 {stratum.independent_run_counts}"
            )
            for pair in stratum.paired_sheets:
                print(
                    f"    {pair.sheet}: {pair.run_a}={pair.mean_a:.1f} / "
                    f"{pair.run_b}={pair.mean_b:.1f}，差 {pair.difference:+.1f}"
                )
        else:
            print(f"{head}无支持：{stratum.reason}")
            print(f"    独立开机数 {stratum.independent_run_counts}")
        if stratum.routine_runs:
            print(
                "    正常试产证据（与分样试制分开，不混算）："
                + ", ".join(r.run_id for r in stratum.routine_runs)
            )
    print(f"\n可归因格子：{[list(k) for k in report.supported_strata]}")
    print(f"不可归因格子：{[list(k) for k in report.unsupported_strata]}")


def _print_rework(records) -> None:
    print("同件原样返工/换料复查判读：")
    if not records:
        print("  （暂无 operation>1 的复查记录）")
    for r in records:
        print(
            f"  {r.sample} op{r.baseline_operation}→op{r.operation}："
            f"{r.baseline_process}/{r.baseline_lot} → {r.rework_process}/{r.rework_lot}"
        )
        print(
            f"    变更范围={r.change_scope}，对比度 {r.baseline_mean:.1f}→"
            f"{r.rework_mean:.1f}（{r.delta:+.1f}，{r.direction}）"
        )
        print(f"    判读：{r.verdict}（老化结论：{r.aging_result or '未取得'}）")


def _print_aging(board) -> None:
    print(f"老化看板（看板日 {board.as_of}）")
    print("  " + "，".join(f"{s} {board.overall_counts.get(s, 0)}" for s in STATUS_ORDER))
    for group in board.by_lot_pattern:
        print(
            f"  [{group.key[0]} × {group.key[1]}] 共 {group.total}："
            + "，".join(f"{s} {group.counts.get(s, 0)}" for s in STATUS_ORDER)
        )
        if group.unresolved_samples:
            print(f"    未决留样：{', '.join(group.unresolved_samples)}")
        if group.bad_samples:
            print(f"    成熟不合格：{', '.join(group.bad_samples)}")
    print(f"\n结论适用格子：{[list(k) for k in board.applicable_scope]}")
    print(f"仍有待决格子：{[list(k) for k in board.pending_scope]}")


def _print_release(report) -> None:
    print("批次级异常后的放行追溯：")
    for chain in report.evidence_chains:
        print(
            f"  留样 {chain.sample}（{chain.lot}/{chain.pattern}/{chain.process}，"
            f"{chain.evidence}）→ 放行单 {', '.join(chain.used_by_permissions)}"
            + ("【共享】" if chain.shared else "【独立】")
        )
    for impact in report.impacts:
        print(
            f"\n  异常 {impact.recheck_id}（{impact.lot}，{impact.category}，"
            f"{impact.discovered_on}）"
        )
        for item in impact.holds:
            print(f"    暂停新放行：{item.permission_id} {item.sku} —— {'; '.join(item.reasons)}")
        for item in impact.trace_scope:
            print(
                f"    追查范围（已交付 {item.sent_to}，不回填）："
                f"{item.permission_id} {item.sku} —— {'; '.join(item.reasons)}"
            )
        for item in impact.unaffected:
            print(f"    不受影响：{item.permission_id} {item.sku} —— {item.action}")
        print(f"    暂停款：{list(impact.paused_skus)}；追查款：{list(impact.trace_skus)}；"
              f"不受影响款：{list(impact.unaffected_skus)}")
    if report.applicability_warnings:
        print("\n适用范围警告：")
        for warning in report.applicability_warnings:
            print(f"  - {warning}")
    print(f"\n{report.scope_note}")


def _print_planning(report) -> None:
    print(f"机时/留样规划（窗口 {report.window[0]} 至 {report.window[1]}）")
    print("  机时容量：" + ", ".join(f"{d}={h}h" for d, h in report.capacity_by_day.items()))
    if report.out_of_window_capacity:
        print(
            "  窗口外机时（不计入一周方案）："
            + ", ".join(f"{d}={h}h" for d, h in report.out_of_window_capacity.items())
        )
    print("  已确认占用：" + ", ".join(f"{d}={h}h" for d, h in report.committed_hours_by_day.items()))
    print("\n试验选项：")
    for option in report.options:
        state = "可选" if option.eligible else "不可选：" + "；".join(option.blockers)
        print(
            f"  {option.assay.id}（{option.route}，留样 {option.assay.sample}，"
            f"{option.assay.hours}h，消耗={option.assay.consumes}）→ {state}"
        )
        for lift in option.lifts:
            mark = "纹路匹配" if lift.pattern_matched else "纹路不匹配"
            print(
                f"      可解除 {lift.hold.permission_id}（{lift.hold.sku}，"
                f"{lift.hold.action}）：{lift.lift_kind}，{mark}"
            )
        for warning in option.scope_warnings:
            print(f"      范围警告：{warning}")
    print("\n可行方案：")
    if not report.plans:
        print("  （窗口内无可行方案）")
    for plan in report.plans:
        print(
            f"  方案 {list(plan.assay_ids)}（{plan.route}，共 {plan.total_hours}h）"
        )
        print(f"      排期：{plan.schedule}；消耗留样：{list(plan.consumed_samples)}")
        print(
            "      解除保留："
            + (", ".join(plan.lifted_hold_ids) or "无")
            + "；仍保留："
            + ", ".join(h.permission_id for h in plan.remaining_holds)
        )
        print(f"      仍无法推断能否销售的款：{list(plan.uninferrable_skus)}")
        if plan.conflicts:
            print(f"      互斥试验（同件消耗性留样）：{list(plan.conflicts)}")
    if report.consumed_blocked:
        print(f"\n已确认消耗而关闭的选项：{list(report.consumed_blocked)}")
    print(f"\n{report.scope_note}")


def _print_ingest(result) -> None:
    new_project, report = result
    print(f"新复查结果入口（看板日 {report.as_of}）")
    if not report.changes:
        print("  本次增量没有改变任何判断。")
    for change in report.changes:
        print(
            f"  [{change.change_type}] {change.subject}："
            f"{change.before or '无记录'} → {change.after}"
        )
        print(f"      {change.judgment}")
        if change.permission_ids:
            print(f"      关联放行单：{list(change.permission_ids)}")
    print(f"\n需要通知的旧放行单：{list(report.notify_permissions)}")
    print(f"  其中已交付（追查不回填）：{list(report.sent_permissions)}")
    print(f"  其中未交付（暂停新放行）：{list(report.held_permissions)}")
    print(f"  仅通知、放行状态不变（如独立复验）：{list(report.notice_only_permissions)}")
    print(f"仍不能从本次结果推断能否销售的款：{list(report.uninferrable_skus)}")
    _ = new_project


def _print_board(payload) -> None:
    project, as_of = payload
    print("=" * 64)
    print("印纹试产项目一页总览")
    print("=" * 64)
    _print_process(compare_processes(project, "改压", "标准"))
    print("\n" + "-" * 64)
    _print_rework(rework_records(project))
    print("\n" + "-" * 64)
    _print_aging(aging_board(project, as_of))
    print("\n" + "-" * 64)
    _print_release(release_report(project))
    print("\n" + "-" * 64)
    _print_planning(build_planning_report(project, as_of=as_of))


# --------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="印纹试产留样与放行统一入口")
    parser.add_argument("--data", default="data/drill.json", help="来源 JSON 路径")
    parser.add_argument("--as-of", dest="as_of", help="看板日 YYYY-MM-DD（默认取最近异常发现日）")
    parser.add_argument("--json", dest="json_output", action="store_true", help="输出 JSON 而非文本视图")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        # 同一参数在子命令后也可写；缺省值取全局命名空间。
        p.add_argument("--as-of", dest="as_of", default=argparse.SUPPRESS,
                       help="看板日 YYYY-MM-DD（默认取最近异常发现日）")
        p.add_argument("--json", dest="json_output", action="store_true",
                       default=argparse.SUPPRESS, help="输出 JSON 而非文本视图")

    p = sub.add_parser("process", help="工艺对比")
    p.add_argument("--process-a", default="改压")
    p.add_argument("--process-b", default="标准")
    add_common(p)

    for name, help_text in (
        ("rework", "返工复查判读"),
        ("aging", "老化看板"),
        ("release", "放行追溯"),
        ("planning", "机时/留样规划"),
        ("board", "一页式总览"),
    ):
        add_common(sub.add_parser(name, help=help_text))

    p = sub.add_parser("ingest", help="并入新复查结果")
    p.add_argument("--updates", help="增量 JSON 文件，含 aging/recheck/readings 数组")
    add_common(p)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    as_of = parse_iso_date(args.as_of) if getattr(args, "as_of", None) else None
    json_output = getattr(args, "json_output", False)
    try:
        project = load_project(Path(args.data))
        if args.command == "process":
            payload = compare_processes(project, args.process_a, args.process_b)
        elif args.command == "rework":
            records = rework_records(project)
            payload = [r.to_dict() for r in records] if json_output else records
        elif args.command == "aging":
            payload = aging_board(project, as_of)
        elif args.command == "release":
            payload = release_report(project)
        elif args.command == "planning":
            payload = build_planning_report(project, as_of=as_of)
        elif args.command == "ingest":
            updates = {"aging": [], "recheck": [], "readings": []}
            if args.updates:
                updates.update(json.loads(Path(args.updates).read_text(encoding="utf-8")))
            new_project, report = ingest_results(
                project,
                aging_updates=updates.get("aging"),
                new_rechecks=updates.get("recheck"),
                new_readings=updates.get("readings"),
                as_of=as_of,
            )
            payload = report.to_dict() if json_output else (new_project, report)
        else:  # board
            if json_output:
                payload = {
                    "process": compare_processes(project, "改压", "标准").to_dict(),
                    "rework": [r.to_dict() for r in rework_records(project)],
                    "aging": aging_board(project, as_of).to_dict(),
                    "release": release_report(project).to_dict(),
                    "planning": build_planning_report(project, as_of=as_of).to_dict(),
                }
            else:
                payload = (project, as_of)

        if json_output:
            _print_json(payload)
        elif args.command == "rework":
            _print_rework(payload)
        elif args.command == "ingest":
            _print_ingest(payload)
        elif args.command == "board":
            _print_board(payload)
        else:
            _emit(args, payload)
        return 0
    except (ProjectError, ValueError, FileNotFoundError, KeyError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
