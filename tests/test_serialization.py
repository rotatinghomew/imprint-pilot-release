"""所有报告对象的 JSON 序列化冒烟测试，保证 CLI --json 不会漏网。"""
import json
import unittest
from datetime import date

from bookmark_qc.aging import aging_board
from bookmark_qc.ingest import ingest_results
from bookmark_qc.model import load_project
from bookmark_qc.planning import build_planning_report
from bookmark_qc.process import compare_processes
from bookmark_qc.release import release_report
from bookmark_qc.rework import rework_records

AS_OF = date(2026, 7, 27)


class SerializationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = load_project("data/drill.json")

    def _roundtrip(self, obj):
        return json.loads(json.dumps(obj.to_dict(), ensure_ascii=False))

    def test_all_reports_serialize(self):
        self._roundtrip(compare_processes(self.project, "改压", "标准"))
        self._roundtrip(aging_board(self.project, AS_OF))
        self._roundtrip(release_report(self.project))
        self._roundtrip(build_planning_report(self.project, as_of=AS_OF))
        for record in rework_records(self.project):
            self._roundtrip(record)

    def test_ingest_report_serializes(self):
        _, report = ingest_results(
            self.project,
            aging_updates=[
                {"sample": "R14-0", "due": "2026-07-25",
                 "inspected_on": "2026-07-27", "result": "发虚"},
            ],
            new_readings=[
                {"sample": "R10-0", "operation": 2, "repetition": 1,
                 "contrast": 90, "process": "标准", "lot": "L2"},
            ],
            as_of=AS_OF,
        )
        payload = self._roundtrip(report)
        subjects = {c["subject"] for c in payload["changes"]}
        self.assertIn("R14-0", subjects)
        self.assertIn("R10-0#op2", subjects)
        self.assertIn("notice_only_permissions", payload)


if __name__ == "__main__":
    unittest.main()
