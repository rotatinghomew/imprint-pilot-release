"""资料预检与证据独立性硬规则的测试。"""
import json
import unittest
from pathlib import Path

from bookmark_qc.model import ProjectError, load_project

BASE = json.loads(Path("data/drill.json").read_text(encoding="utf-8"))


def _without(rows, predicate):
    return [dict(row) for row in rows if not predicate(row)]


class PrecheckTest(unittest.TestCase):
    def test_baseline_loads(self):
        project = load_project("data/drill.json")
        self.assertEqual(len(project.runs), 24)
        self.assertEqual(len(project.samples), 72)

    def test_schema_mismatch_rejected(self):
        raw = json.loads(json.dumps(BASE))
        del raw["runs"][0]["lot"]
        with self.assertRaises(ProjectError):
            load_project(raw)

    def test_dangling_reference_rejected(self):
        raw = json.loads(json.dumps(BASE))
        raw["samples"].append({"id": "X-0", "run": "不存在", "reserved": True})
        with self.assertRaises(ProjectError):
            load_project(raw)

    def test_duplicate_independent_reuse_rejected(self):
        # 同一件留样被两张独立复验放行单引用 → 不构成独立证据，预检拒绝
        raw = json.loads(json.dumps(BASE))
        raw["permission"].append(
            {"id": "A4", "sku": "书签戊", "pattern": "疏纹", "process": "标准",
             "sample": "R9-0", "evidence": "独立复验", "sent_to": None}
        )
        with self.assertRaises(ProjectError):
            load_project(raw)

    def test_aging_only_covers_reserved_samples(self):
        raw = json.loads(json.dumps(BASE))
        raw["aging"].append(
            {"sample": "R0-1", "due": "2026-07-18",
             "inspected_on": "2026-07-18", "result": "正常"}
        )
        with self.assertRaises(ProjectError):
            load_project(raw)

    def test_overbooked_machine_hours_rejected(self):
        raw = json.loads(json.dumps(BASE))
        raw["committed"].append(
            {"assay": "T2", "date": "2026-07-27", "confirmed_on": "2026-07-26"}
        )
        with self.assertRaises(ProjectError):
            load_project(raw)

    def test_rework_without_original_operation_rejected(self):
        raw = json.loads(json.dumps(BASE))
        raw["readings"] = _without(
            raw["readings"], lambda r: r["sample"] == "R6-0" and r["operation"] == 1
        )
        with self.assertRaises(ProjectError):
            load_project(raw)

    def test_lost_sample_cannot_have_inspection_date(self):
        raw = json.loads(json.dumps(BASE))
        row = next(r for r in raw["aging"] if r["sample"] == "R4-0")
        row["inspected_on"] = "2026-07-20"
        with self.assertRaises(ProjectError):
            load_project(raw)


if __name__ == "__main__":
    unittest.main()
