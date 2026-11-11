import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from evaluation.evaluator import EndToEndEvaluator, EvalResult, LLMJudge


def judge_json(score=0.8):
    return json.dumps({
        name: {"score": score, "reason": f"{name}理由"}
        for name in LLMJudge.DIMENSIONS
    }, ensure_ascii=False)


class LLMJudgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.judge = LLMJudge(SimpleNamespace(), "judge-model")

    def test_missing_dimension_is_invalid_instead_of_zero(self):
        payload = json.loads(judge_json())
        del payload["accuracy"]

        with self.assertRaisesRegex(ValueError, "缺少字段: accuracy"):
            self.judge._parse_output(json.dumps(payload, ensure_ascii=False))

    def test_valid_output_preserves_scores_and_reasons(self):
        values, reasons = self.judge._parse_output(judge_json(0.75))

        self.assertEqual(values["accuracy"], 0.75)
        self.assertEqual(reasons["accuracy"], "accuracy理由")

    def test_missing_reason_is_invalid(self):
        payload = json.loads(judge_json())
        del payload["accuracy"]["reason"]

        with self.assertRaisesRegex(ValueError, "accuracy.*score 和 reason"):
            self.judge._parse_output(json.dumps(payload, ensure_ascii=False))

    async def test_invalid_output_is_retried_once_and_raw_outputs_are_kept(self):
        invalid = '{"relevance":{"score":0.8,"reason":"相关"}}'
        response = SimpleNamespace(content=[])
        with (
            patch("evaluation.evaluator.create_message", AsyncMock(return_value=response)) as create,
            patch("evaluation.evaluator.extract_text", side_effect=[invalid, judge_json(0.9)]),
        ):
            result = await self.judge.judge("问题", "回答")

        self.assertTrue(result.valid)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.raw_outputs, [invalid, judge_json(0.9)])
        self.assertEqual(result.reasons["accuracy"], "accuracy理由")
        self.assertIn("只输出一个合法 JSON", create.await_args_list[0].kwargs["system"])
        self.assertEqual(create.await_args_list[0].kwargs["max_tokens"], 800)

    async def test_repeated_invalid_output_fails_without_zero_scores(self):
        invalid = '{"relevance":{"score":0.8,"reason":"相关"}}'
        response = SimpleNamespace(content=[])
        with (
            patch("evaluation.evaluator.create_message", AsyncMock(return_value=response)),
            patch("evaluation.evaluator.extract_text", return_value=invalid),
        ):
            result = await self.judge.judge("问题", "回答")

        self.assertTrue(result.judge_failed)
        self.assertIsNone(result.accuracy)
        self.assertIsNone(result.overall)
        self.assertEqual(result.raw_output, invalid)
        self.assertEqual(result.raw_outputs, [invalid, invalid])
        self.assertEqual(result.error_type, "invalid_output")


class JudgeAggregationTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_judge_is_excluded_from_quality_average(self):
        evaluator = EndToEndEvaluator.__new__(EndToEndEvaluator)
        evaluator._history = []
        evaluator._baseline = None
        evaluator._baseline_path = None
        evaluator._evaluate_dialog_case = AsyncMock(side_effect=[
            [EvalResult(
                test_id="ok",
                passed=True,
                scores={
                    "relevance": 0.8,
                    "accuracy": 0.8,
                    "completeness": 0.8,
                    "helpfulness": 0.8,
                },
                metadata={"judge_failed": False},
            )],
            [EvalResult(
                test_id="judge_error",
                passed=False,
                scores={"deterministic": 1.0},
                metadata={"judge_failed": True},
            )],
        ])

        report = await evaluator.run(dialog_cases=[{}, {}])

        self.assertEqual(report.avg_scores["accuracy"], 0.8)
        self.assertEqual(report.judge_stats, {
            "total": 2,
            "successful": 1,
            "failed": 1,
            "coverage": 0.5,
        })
        self.assertIn("未计入质量均分", report.recommendations[0])


if __name__ == "__main__":
    unittest.main()
