import unittest
from types import SimpleNamespace

from evaluation.evaluator import EndToEndEvaluator, QualityScores


def chat_result(**overrides):
    values = {
        "intent": "other",
        "agent_type": "general",
        "escalated": False,
        "citations": [],
        "admission_data_used": False,
        "knowledge_used": False,
        "entities": {},
        "latency_ms": 1.0,
        "response": "2025年最低分621分，未承诺录取。",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class EvaluatorAssertionTests(unittest.TestCase):
    def test_soft_routing_and_wording_do_not_fail_hard_assertions(self):
        checks = EndToEndEvaluator._deterministic_checks(chat_result(), {
            "expected_intent": "score_risk",
            "expected_agent": "risk",
            "required_terms": ["621"],
            "soft_required_terms": ["相对匹配"],
            "forbidden_terms": ["保证录取"],
        })
        summary = EndToEndEvaluator._assertion_summary(checks)

        self.assertTrue(summary["hard_passed"])
        self.assertEqual(summary["soft_passed"], 0)
        self.assertEqual(summary["soft_total"], 3)

    def test_hard_required_term_still_blocks_the_case(self):
        checks = EndToEndEvaluator._deterministic_checks(chat_result(), {
            "required_terms": ["13587"],
        })

        self.assertFalse(EndToEndEvaluator._assertion_summary(checks)["hard_passed"])

    def test_routing_can_be_promoted_to_hard_for_targeted_cases(self):
        checks = EndToEndEvaluator._deterministic_checks(chat_result(), {
            "expected_intent": "score_risk",
            "expected_agent": "risk",
            "routing_assertions_hard": True,
        })
        summary = EndToEndEvaluator._assertion_summary(checks)

        self.assertFalse(summary["hard_passed"])
        self.assertEqual(summary["hard_failed"], 2)
        self.assertEqual(summary["soft_total"], 0)

class EvaluatorAssertionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_soft_failures_do_not_veto_an_otherwise_passing_case(self):
        class ChatServiceStub:
            async def clear_conversation(self, _user_id, _conv_id):
                return None

            async def handle(self, *_args, **_kwargs):
                return chat_result()

        class JudgeStub:
            async def judge(self, *_args, **_kwargs):
                return QualityScores(
                    0.8, 0.8, 0.8, 0.8,
                    reasons={"accuracy": "事实一致"},
                    raw_output="原始Judge输出",
                    raw_outputs=["原始Judge输出"],
                    attempts=1,
                )

        evaluator = EndToEndEvaluator.__new__(EndToEndEvaluator)
        evaluator._chat_service = ChatServiceStub()
        evaluator._judge = JudgeStub()
        result = (await evaluator._evaluate_dialog_case({
            "question": "测试问题",
            "expected_intent": "score_risk",
            "expected_agent": "risk",
            "soft_required_terms": ["相对匹配"],
            "forbidden_terms": ["保证录取"],
        }, 0))[0]

        self.assertTrue(result.passed)
        self.assertEqual(result.scores["soft_assertions"], 0.0)
        self.assertTrue(result.metadata["assertion_summary"]["hard_passed"])
        self.assertEqual(result.metadata["judge_raw_output"], "原始Judge输出")
        self.assertEqual(result.metadata["judge_reasons"]["accuracy"], "事实一致")

if __name__ == "__main__":
    unittest.main()
