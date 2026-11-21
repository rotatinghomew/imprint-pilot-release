"""机时/留样规划与新结果入口的业务规则测试。"""
import unittest
from datetime import date

from bookmark_qc.aging import MATURE_BAD, OVERDUE, PENDING
from bookmark_qc.ingest import AGING_CHANGE, RECHECK_CHANGE, REWORK_CHANGE, ingest_results
from bookmark_qc.model import ProjectError, load_project
from bookmark_qc.planning import (
    DIAGNOSE,
    RESUME_SALES,
    active_holds,
    build_planning_report,
)

AS_OF = date(2026, 7, 26)


class PlanningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = load_project("data/drill.json")
        cls.report = build_planning_report(cls.project, as_of=AS_OF)

    def test_active_holds_cover_pause_and_trace(self):
        holds = {h.permission_id: h for h in active_holds(self.project)}
        self.assertEqual(set(holds), {"A1", "A2"})
        self.assertTrue(holds["A1"].delivered)
        self.assertFalse(holds["A2"].delivered)

    def test_consumable_assays_are_mutually_exclusive(self):
        options = {o.assay.id: o for o in self.report.options}
        # T1 已确认排期；T2 与 T1 消耗同一件留样 R8-0，必须关闭
        self.assertFalse(options["T1"].eligible)
        self.assertFalse(options["T2"].eligible)
        self.assertTrue(
            any("已被已确认的消耗性试验用掉" in b for b in options["T2"].blockers)
        )
        self.assertEqual(options["T2"].lifts[0].via_assay, "T2")
        # T3 不与已确认试验冲突，仍可选
        self.assertTrue(options["T3"].eligible)

    def test_no_plan_can_double_count_same_reserved_sample(self):
        for plan in self.report.plans:
            self.assertEqual(
                len(plan.consumed_samples),
                len(set(plan.consumed_samples)),
            )
        # T3 与任何试验都不共享留样，方案级互斥名单为空
        t3_plan = next(p for p in self.report.plans if p.assay_ids == ("T3",))
        self.assertEqual(t3_plan.conflicts, ())
        # 选项级：T1/T2 消耗同件留样 R8-0，互斥关系显式可查（含已确认项）
        from bookmark_qc.planning import _conflicts
        self.assertEqual(set(_conflicts(self.project, self.project.assays["T2"])), {"T1"})
        self.assertEqual(set(_conflicts(self.project, self.project.assays["T1"])), {"T2"})

    def test_plans_respect_machine_hours_window(self):
        # 一周自然截止日为 8/2；窗口内实际只有 7/27（已满）与 7/29（3h）；8/4 在窗口外
        self.assertEqual(self.report.window_deadline, date(2026, 8, 2))
        self.assertEqual(
            self.report.window,
            (AS_OF, date(2026, 7, 29)),
        )
        self.assertEqual(self.report.capacity_by_day,
                         {"2026-07-27": 3, "2026-07-29": 3})
        self.assertEqual(self.report.out_of_window_capacity, {"2026-08-04": 5})
        for plan in self.report.plans:
            for day, ids in plan.schedule.items():
                self.assertIn(day, self.report.capacity_by_day)

    def test_routes_and_lifts_are_distinguished(self):
        options = {o.assay.id: o for o in self.report.options}
        self.assertEqual(options["T1"].route, DIAGNOSE)
        self.assertEqual(options["T2"].route, RESUME_SALES)
        # 纹路不匹配的复验不能真正解除保留
        t3 = options["T3"]
        self.assertFalse(t3.lifts[0].pattern_matched)
        plan = next(p for p in self.report.plans if p.assay_ids == ("T3",))
        # 解除列表按规则仍记录该保留，但 uninferrable 保留书签乙
        self.assertEqual(plan.lifted_hold_ids, ("A2",))
        self.assertIn("书签乙", plan.uninferrable_skus)
        self.assertIn("书签甲", plan.uninferrable_skus)

    def test_synthetic_window_with_both_routes_feasible(self):
        """构造一个 T1 未确认、两条路线都可选的项目，验证混合方案枚举。"""
        project = load_project("data/drill.json")
        raw = {
            "runs": [
                {"id": "R0", "lot": "L1", "sheet": "板0", "pattern": "密纹",
                 "process": "改压", "assignment": "分样试制", "on": "2026-07-01"},
                {"id": "R1", "lot": "L2", "sheet": "板1", "pattern": "疏纹",
                 "process": "标准", "assignment": "分样试制", "on": "2026-07-02"},
            ],
            "samples": [
                {"id": "R0-0", "run": "R0", "reserved": True},
                {"id": "R1-0", "run": "R1", "reserved": True},
            ],
            "readings": [
                {"sample": "R0-0", "operation": 1, "repetition": 1,
                 "contrast": 70, "process": "改压", "lot": "L1"},
                {"sample": "R1-0", "operation": 1, "repetition": 1,
                 "contrast": 80, "process": "标准", "lot": "L2"},
            ],
            "aging": [
                {"sample": "R0-0", "due": "2026-07-20",
                 "inspected_on": "2026-07-20", "result": "发虚"},
                {"sample": "R1-0", "due": "2026-07-20",
                 "inspected_on": "2026-07-20", "result": "正常"},
            ],
            "permission": [
                {"id": "A1", "sku": "书签甲", "pattern": "密纹", "process": "改压",
                 "sample": "R0-0", "evidence": "共享试产", "sent_to": "馆内"},
                {"id": "A2", "sku": "书签乙", "pattern": "疏纹", "process": "标准",
                 "sample": "R0-0", "evidence": "共享试产", "sent_to": None},
            ],
            "recheck": [
                {"id": "C1", "lot": "L1", "discovered_on": "2026-07-26",
                 "category": "印纹发虚"},
            ],
            "test_hours": [
                {"date": "2026-07-27", "capacity": 5},
                {"date": "2026-07-29", "capacity": 5},
            ],
            "assays": [
                {"id": "T1", "sample": "R0-0", "purpose": "批次归因",
                 "sku": None, "hours": 3, "consumes": True},
                {"id": "T2", "sample": "R1-0", "purpose": "复验放行",
                 "sku": "书签乙", "hours": 2, "consumes": True},
            ],
            "committed": [],
        }
        synth = load_project(raw)
        report = build_planning_report(synth, as_of=AS_OF)
        ids = {p.assay_ids for p in report.plans}
        self.assertIn(("T1",), ids)
        self.assertIn(("T2",), ids)
        self.assertIn(("T1", "T2"), ids)  # 混合路线可排下
        combo = next(p for p in report.plans if p.assay_ids == ("T1", "T2"))
        self.assertEqual(combo.total_hours, 5)
        self.assertEqual(set(combo.consumed_samples), {"R0-0", "R1-0"})


class IngestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = load_project("data/drill.json")

    def test_aging_update_changes_judgment_and_notifies(self):
        # R13-0 从“逾期未检”补登为成熟不合格
        new_project, report = ingest_results(
            self.project,
            aging_updates=[
                {"sample": "R13-0", "due": "2026-07-24",
                 "inspected_on": "2026-07-27", "result": "发虚"},
            ],
            as_of=AS_OF,
        )
        change = next(c for c in report.changes if c.subject == "R13-0")
        self.assertEqual(change.change_type, AGING_CHANGE)
        self.assertEqual(change.before, OVERDUE)
        self.assertEqual(change.after, MATURE_BAD)
        # R13-0 没有放行单引用，故无通知
        self.assertEqual(change.permission_ids, ())
        # 原项目不被就地修改
        self.assertIsNone(self.project.aging["R13-0"].inspected_on)

    def test_new_recheck_traces_old_permissions(self):
        # 对 L2 新增批次异常：A3（独立复验）被关联通知但不受自动保留
        _, report = ingest_results(
            self.project,
            new_rechecks=[
                {"id": "C2", "lot": "L2", "discovered_on": "2026-07-27",
                 "category": "印纹发虚"},
            ],
            as_of=date(2026, 7, 27),
        )
        change = next(c for c in report.changes if c.change_type == RECHECK_CHANGE)
        self.assertEqual(change.subject, "C2")
        self.assertIn("A3", change.permission_ids)
        self.assertEqual(report.notify_permissions, ("A3",))
        self.assertEqual(report.notice_only_permissions, ("A3",))
        self.assertEqual(report.held_permissions, ())
        self.assertEqual(report.sent_permissions, ())
        # A1/A2 的证据链在 L1，与本次 L2 结果无关，仍是不可推断的款
        self.assertIn("书签甲", report.uninferrable_skus)
        self.assertIn("书签乙", report.uninferrable_skus)

    def test_new_rework_reading_produces_rework_change(self):
        _, report = ingest_results(
            self.project,
            new_readings=[
                {"sample": "R8-0", "operation": 2, "repetition": 1,
                 "contrast": 96, "process": "改压", "lot": "L1"},
            ],
            as_of=AS_OF,
        )
        change = next(c for c in report.changes if c.change_type == REWORK_CHANGE)
        self.assertIn("R8-0", change.subject)

    def test_pending_to_mature_ok_path(self):
        # R23-0 到期日 7/29；看板日仍在 7/28（未到期）时提前补登合格结论，
        # 状态走 未到期→成熟合格
        _, report = ingest_results(
            self.project,
            aging_updates=[
                {"sample": "R23-0", "due": "2026-07-29",
                 "inspected_on": "2026-07-28", "result": "正常"},
            ],
            as_of=date(2026, 7, 28),
        )
        change = next(c for c in report.changes if c.subject == "R23-0")
        self.assertEqual(change.before, PENDING)
        self.assertEqual(change.after, "成熟合格")

    def test_new_recheck_on_evidence_lot_classifies_sent_and_held(self):
        # 再次对 L1 登记异常（新编号）：A1 已交付→追查，A2 未交付→暂停
        _, report = ingest_results(
            self.project,
            new_rechecks=[
                {"id": "C3", "lot": "L1", "discovered_on": "2026-07-27",
                 "category": "印纹发虚"},
            ],
            as_of=date(2026, 7, 27),
        )
        self.assertEqual(sorted(report.notify_permissions), ["A1", "A2"])
        self.assertEqual(report.sent_permissions, ("A1",))
        self.assertEqual(report.held_permissions, ("A2",))
        self.assertEqual(report.notice_only_permissions, ())

    def test_ingest_still_runs_full_validation(self):
        bad_update = [
            {"sample": "R13-0", "due": "2026-07-24",
             "inspected_on": "2026-07-27", "result": "不存在的结论"},
        ]
        with self.assertRaises(ProjectError):
            ingest_results(self.project, aging_updates=bad_update, as_of=AS_OF)


if __name__ == "__main__":
    unittest.main()
