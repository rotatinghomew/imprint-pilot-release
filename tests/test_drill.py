"""核查练习数据的原始事实，不计算分析结果。"""
from pathlib import Path
import json
import unittest


class DrillDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / 'data' / 'drill.json'
        cls.data = json.loads(source.read_text(encoding='utf-8'))

    def test_schema(self):
        expected = {'runs': ('id', 'lot', 'sheet', 'pattern', 'process', 'assignment', 'on'), 'samples': ('id', 'run', 'reserved'), 'readings': ('sample', 'operation', 'repetition', 'contrast', 'process', 'lot'), 'aging': ('sample', 'due', 'inspected_on', 'result'), 'permission': ('id', 'sku', 'pattern', 'process', 'sample', 'evidence', 'sent_to'), 'recheck': ('id', 'lot', 'discovered_on', 'category'), 'test_hours': ('date', 'capacity'), 'assays': ('id', 'sample', 'purpose', 'sku', 'hours', 'consumes'), 'committed': ('assay', 'date', 'confirmed_on')}
        self.assertEqual(set(self.data), set(expected))
        for name, fields in expected.items():
            with self.subTest(table=name):
                self.assertTrue(self.data[name])
                self.assertTrue(all(set(row) == set(fields) for row in self.data[name]))

    def test_shared_evidence_and_competing_destructive_tests(self):
        cases = [row for row in self.data['permission'] if row['sample'] == 'R6-0']
        self.assertEqual(len(cases), 2)
        self.assertEqual({row['sent_to'] is None for row in cases}, {True, False})
        facts = [row for row in self.data['readings'] if row['sample'] == 'R6-0']
        self.assertEqual({row['lot'] for row in facts}, {'L1', 'L2'})
        self.assertTrue(any(row['result'] == '发虚' for row in self.data['aging']))
        options = [row for row in self.data['assays'] if row['sample'] == 'R8-0']
        self.assertTrue(all(row['consumes'] for row in options))
        self.assertEqual(len({row['purpose'] for row in options}), 2)
