"""工艺对比与返工判读的业务规则测试。"""
import unittest
from datetime import date

from bookmark_qc.model import load_project
from bookmark_qc.process import (
    ROUTINE_EVIDENCE,
    SPLIT_EVIDENCE,
    compare_processes,
    evidence_breakdown,
    run_statistics,
    samples_per_sheet,
)
from bookmark_qc.rework import (
    BOTH_CHANGED,
    IMPROVED,
    PROCESS_ONLY,
    VERDICT_AMBIGUOUS,
    VERDICT_PROCESS,
    rework_records,
)


class ProcessComparisonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = load_project("data/drill.json")
        cls.report = compare_processes(cls.project, "改压", "标准")

    def test_independent_unit_is_run_not_ten_samples(self):
        # 每片材料在每个工艺上只对应一次开机；同片多件样品不得放大独立样本数。
        counts = samples_per_sheet(self.project)
        self.assertTrue(all(n == 6 for n in counts.values()))  # 每片两工艺各 3 件
        stratum = next(s for s in self.report.strata if (s.lot, s.pattern) == ("L1", "密纹"))
        self.assertEqual(stratum.independent_run_counts, {"改压": 2, "标准": 2})
        # 开机均值由样品均值再平均得到，重复测量不被当成独立件。
        stat = run_statistics(self.project, self.project.runs["R0"])
        self.assertEqual(stat.sample_count, 3)
        self.assertEqual(stat.repetition_count, 6)
        self.assertAlmostEqual(stat.mean_contrast, 71.5)

    def test_only_shared_material_strata_are_attributable(self):
        # L1/L2 各纹路都有同片分样配对；L3 只有改压，不支持工艺归因。
        self.assertEqual(
            set(self.report.supported_strata),
            {("L1", "疏纹"), ("L1", "密纹"), ("L2", "疏纹"), ("L2", "密纹")},
        )
        for key in (("L3", "疏纹"), ("L3", "密纹")):
            stratum = next(s for s in self.report.strata if (s.lot, s.pattern) == key)
            self.assertFalse(stratum.supported)
            self.assertIn("只在改压工艺出现", stratum.reason)
            self.assertEqual(stratum.paired_difference_mean, None)

    def test_paired_difference_uses_within_sheet_contrast(self):
        # 所有配对片上标准工艺均高 1 个对比度单位，跨批次的整体漂移被配对差消掉。
        for stratum in self.report.strata:
            if stratum.supported:
                self.assertAlmostEqual(stratum.paired_difference_mean, 1.0)
                self.assertTrue(all(p.difference == 1.0 for p in stratum.paired_sheets))

    def test_split_and_routine_evidence_are_separate(self):
        breakdown = evidence_breakdown(self.project)
        self.assertTrue(breakdown[SPLIT_EVIDENCE])
        self.assertTrue(breakdown[ROUTINE_EVIDENCE])
        self.assertTrue(
            all(s.assignment == ROUTINE_EVIDENCE for s in breakdown[ROUTINE_EVIDENCE])
        )
        # L3 的正常试产开机不进入任何同片配对。
        routine_ids = {s.run_id for s in breakdown[ROUTINE_EVIDENCE]}
        self.assertEqual(routine_ids, {f"R{i}" for i in range(16, 24)})

    def test_rework_distinguishes_process_gain_from_material_masking(self):
        records = {(r.sample, r.operation): r for r in rework_records(self.project)}
        ambiguous = records[("R6-0", 2)]
        self.assertEqual(ambiguous.change_scope, BOTH_CHANGED)
        self.assertEqual(ambiguous.direction, IMPROVED)
        self.assertEqual(ambiguous.verdict, VERDICT_AMBIGUOUS)
        self.assertEqual(ambiguous.baseline_lot, "L1")
        self.assertEqual(ambiguous.rework_lot, "L2")

        process_gain = records[("R7-0", 2)]
        self.assertEqual(process_gain.change_scope, PROCESS_ONLY)
        self.assertEqual(process_gain.verdict, VERDICT_PROCESS)
        self.assertEqual(process_gain.rework_lot, process_gain.baseline_lot)

    def test_illegal_process_pair_rejected(self):
        with self.assertRaises(ValueError):
            compare_processes(self.project, "改压", "改压")
        with self.assertRaises(ValueError):
            compare_processes(self.project, "改压", "不存在的工艺")


if __name__ == "__main__":
    unittest.main()
