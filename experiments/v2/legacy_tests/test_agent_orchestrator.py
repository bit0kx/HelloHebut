import unittest

from agents.agent_orchestrator import (
    AgentOrchestrator,
    AgentResponse,
    AgentType,
    Request,
)
from core.intent_recognizer import IntentCategory, UrgencyLevel


class AgentOrchestratorPresentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_agent_labels_are_not_exposed_in_response(self):
        orchestrator = AgentOrchestrator(api_key="test-key")
        contents = {
            AgentType.POLICY: "政策说明正文",
            AgentType.RISK: "风险查询正文",
        }

        async def execute(_request, agent_type):
            return AgentResponse(
                agent_type=agent_type,
                content=contents[agent_type],
                success=True,
                latency_ms=1.0,
            )

        orchestrator._execute = execute
        request = Request(
            message="招生计划",
            user_id="test-user",
            conv_id="test-conversation",
            intent=IntentCategory.ADMISSION_POLICY,
            urgency=UrgencyLevel.LOW,
        )

        result = await orchestrator.run_parallel(
            request,
            [AgentType.POLICY, AgentType.RISK],
        )

        self.assertEqual(result.response, "政策说明正文\n\n风险查询正文")
        self.assertNotIn("[policy]", result.response)
        self.assertNotIn("[risk]", result.response)
        self.assertEqual(result.agent_type, AgentType.POLICY)


if __name__ == "__main__":
    unittest.main()
