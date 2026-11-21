"""老化看板与放行追溯的业务规则测试。"""
import unittest
from datetime import date

from bookmark_qc.aging import (
    LOST,
    MATURE_BAD,
    MATURE_OK,
    OVERDUE,
    PENDING,
    aging_board,
    classify,
    default_as_of,
)
from bookmark_qc.model import load_project
from bookmark_qc.release import (
    HOLD_NEW,
    INVALID_OWN_DEFECT,
    TRACE_SENT,
    UNAFFECTED,
    evidence_chains,
    release_report,
)

AS_OF = date(2026, 7, 26)


class AgingBoardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = load_project("data/drill.json")
        cls.board = aging_board(cls.project, AS_OF)

    def test_default_as_of_tracks_latest_recheck(self):
        self.assertEqual(default_as_of(self.project), AS_OF)

    def test_statuses_are_separated_not_collapsed(self):
        self.assertEqual(
            self.board.overall_counts,
            {PENDING: 6, MATURE_OK: 11, MATURE_BAD: 1, LOST: 1, OVERDUE: 5},
        )

    def test_null_result_is_not_treated_as_good(self):
        by_sample = {row.sample: row for row in self.board.rows}
        # 到期日在看板日之前且无结论 → 逾期未检，不是合格
        self.assertEqual(by_sample["R13-0"].status, OVERDUE)
        # 到期日晚于看板日 → 未到期（R18-0 到期 7/27，看板日 7/26）
        self.assertEqual(by_sample["R18-0"].status, PENDING)
        # 失联单列
        self.assertEqual(by_sample["R4-0"].status, LOST)
        # 成熟不合格单列
        self.assertEqual(by_sample["R6-0"].status, MATURE_BAD)

    def test_classify_rules(self):
        due = date(2026, 7, 26)
        self.assertEqual(classify(None, "失联", due, AS_OF), LOST)
        self.assertEqual(classify(None, None, date(2026, 7, 27), AS_OF), PENDING)
        self.assertEqual(classify(due, "正常", due, AS_OF), MATURE_OK)
        self.assertEqual(classify(due, "发虚", due, AS_OF), MATURE_BAD)
        self.assertEqual(classify(None, None, due, AS_OF), OVERDUE)

    def test_applicable_scope_is_by_lot_and_pattern(self):
        # 只有 L1×密纹 全部成熟有结论（含 1 件不合格），可给出完整格子结论
        self.assertEqual(self.board.applicable_scope, (("L1", "密纹"),))
        self.assertIn(("L3", "密纹"), self.board.pending_scope)
        group = next(g for g in self.board.by_lot_pattern if g.key == ("L1", "密纹"))
        self.assertEqual(group.bad_samples, ("R6-0",))


class ReleaseTraceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = load_project("data/drill.json")
        cls.report = release_report(cls.project)

    def test_shared_evidence_chain_is_reconstructed(self):
        chains = {c.sample: c for c in evidence_chains(self.project)}
        shared = chains["R6-0"]
        self.assertTrue(shared.shared)
        self.assertEqual(shared.used_by_permissions, ("A1", "A2"))
        independent = chains["R9-0"]
        self.assertTrue(independent.independent)
        self.assertFalse(independent.shared)

    def test_batch_anomaly_holds_shared_permissions_but_not_independent(self):
        impact = self.report.impacts[0]
        self.assertEqual(impact.lot, "L1")
        self.assertEqual(impact.paused_skus, ("书签乙",))
        self.assertEqual(impact.trace_skus, ("书签甲",))
        self.assertEqual(impact.unaffected_skus, ("书签丙",))
        actions = {p.permission_id: p.action for p in (*impact.holds, *impact.trace_scope, *impact.unaffected)}
        self.assertEqual(actions["A1"], TRACE_SENT)
        self.assertEqual(actions["A2"], HOLD_NEW)
        self.assertEqual(actions["A3"], UNAFFECTED)

    def test_delivered_samples_are_traced_not_rewritten(self):
        impact = self.report.impacts[0]
        trace = next(p for p in impact.trace_scope if p.permission_id == "A1")
        self.assertTrue(trace.delivered)
        self.assertEqual(trace.sent_to, "馆内")
        # 原始放行单的交付事实保持不变
        self.assertEqual(self.project.permission["A1"].sent_to, "馆内")

    def test_pattern_mismatch_is_flagged_as_scope_warning(self):
        # A2 声明疏纹但 R6-0 实为密纹；A3 声明密纹但 R9-0 实为疏纹，都须标出
        warnings = "\n".join(self.report.applicability_warnings)
        self.assertIn("放行单 A2", warnings)
        self.assertIn("放行单 A3", warnings)
        self.assertIn("纹路", warnings)
        # A1 的纹路与工艺与留样事实一致，不应有警告
        self.assertFalse(any("放行单 A1" in w for w in self.report.applicability_warnings))

    def test_independent_evidence_with_own_defect_would_fail(self):
        # 构造独立复验留样自身成熟不合格的情形：独立证据也失效
        raw = {
            "runs": [
                {"id": "R0", "lot": "L1", "sheet": "板0", "pattern": "疏纹",
                 "process": "标准", "assignment": "分样试制", "on": "2026-07-01"},
            ],
            "samples": [{"id": "R0-0", "run": "R0", "reserved": True}],
            "readings": [
                {"sample": "R0-0", "operation": 1, "repetition": 1,
                 "contrast": 80, "process": "标准", "lot": "L1"},
            ],
            "aging": [{"sample": "R0-0", "due": "2026-07-20",
                       "inspected_on": "2026-07-20", "result": "发虚"}],
            "permission": [
                {"id": "A9", "sku": "书签丁", "pattern": "疏纹", "process": "标准",
                 "sample": "R0-0", "evidence": "独立复验", "sent_to": None},
            ],
            "recheck": [
                {"id": "C9", "lot": "L1", "discovered_on": "2026-07-26",
                 "category": "印纹发虚"},
            ],
            "test_hours": [{"date": "2026-07-27", "capacity": 3}],
            "assays": [],
            "committed": [],
        }
        project = load_project(raw)
        report = release_report(project)
        impact = report.impacts[0]
        self.assertEqual(impact.unaffected[0].action, INVALID_OWN_DEFECT)


if __name__ == "__main__":
    unittest.main()
