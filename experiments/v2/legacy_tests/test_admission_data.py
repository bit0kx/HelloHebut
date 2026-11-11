import unittest
from pathlib import Path

from mcp.admission_data import AdmissionDataStore


class AdmissionDataStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        csv_path = Path(__file__).resolve().parents[3] / "data" / "hebut_admission.csv"
        cls.store = AdmissionDataStore(str(csv_path))

    def test_dataset_stats_and_gb18030_detection(self):
        self.assertEqual(self.store.stats["total_rows"], 507)
        self.assertEqual(self.store.stats["valid_rows"], 447)
        self.assertEqual(self.store.stats["incomplete_rows"], 60)
        self.assertEqual(self.store.stats["encoding"], "gb18030")
        self.assertEqual(self.store.stats["provinces"], ["天津", "河北"])

    def test_hebei_rank_risk_uses_three_year_records(self):
        result = self.store.query({
            "query": "我是2026年河北物理类考生，620分、位次12000，想报计算机专业",
        })
        self.assertEqual(result["status"], "ok")
        analysis = result["analyses"][0]
        self.assertEqual(analysis["major"], "计算机科学与技术")
        self.assertEqual([row["min_rank"] for row in analysis["records"]], [12287, 12449, 13587])
        self.assertEqual(analysis["assessment"]["risk_level"], "相对匹配")
        self.assertEqual(analysis["assessment"]["better_or_equal_years"], 3)

    def test_tianjin_defaults_to_comprehensive_reform(self):
        result = self.store.query({"query": "天津考生630分、6500名报计算机稳吗？"})
        analysis = result["analyses"][0]
        self.assertEqual(analysis["subject_type"], "综合改革")
        self.assertEqual([row["min_rank"] for row in analysis["records"]], [5948, 5851, 6375])
        self.assertEqual(analysis["assessment"]["risk_level"], "偏冲")

    def test_exact_year_lookup(self):
        result = self.store.query({"query": "河北2025年计算机科学与技术最低分和最低位次是多少？"})
        record = result["analyses"][0]["records"][0]
        self.assertEqual(record["year"], 2025)
        self.assertEqual(record["min_score"], 621)
        self.assertEqual(record["min_rank"], 13587)

    def test_missing_province_requests_clarification(self):
        result = self.store.query({"query": "去年计算机专业最低位次是多少？"})
        self.assertEqual(result["status"], "needs_clarification")
        self.assertIn("province", result["missing_fields"])

    def test_unsupported_province_reports_coverage_without_querying_rows(self):
        result = self.store.query({"query": "山东考生位次12000报计算机稳吗？"})
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["supported_provinces"], ["河北", "天津"])

    def test_candidate_recommendations_are_deterministic(self):
        result = self.store.query({"query": "河北物理类位次15000能报哪些专业？"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["candidates"][0]["major"], "物联网工程")
        self.assertLessEqual(len(result["candidates"]), 12)

    def test_one_year_program_is_marked_data_insufficient(self):
        result = self.store.query({"query": "河北物理类位次15000报光电信息科学与工程稳吗？"})
        assessment = result["analyses"][0]["assessment"]
        self.assertEqual(assessment["available_years"], 1)
        self.assertEqual(assessment["risk_level"], "数据不足")

    def test_follow_up_inherits_candidate_context(self):
        result = self.store.query({
            "query": "那电气专业呢？",
            "entities": {"major": ["电气"]},
            "history": [{
                "role": "user",
                "content": "我是河北物理类考生，620分、位次12000，想报计算机",
            }],
        })
        analysis = result["analyses"][0]
        self.assertEqual(result["resolved"]["province"], "河北")
        self.assertEqual(result["resolved"]["candidate_rank"], 12000)
        self.assertEqual(analysis["major"], "电气工程及其自动化")
        self.assertEqual([row["min_rank"] for row in analysis["records"]], [11883, 12000, 12091])


if __name__ == "__main__":
    unittest.main()
