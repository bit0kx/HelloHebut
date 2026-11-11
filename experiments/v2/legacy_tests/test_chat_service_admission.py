import unittest
from pathlib import Path
from types import SimpleNamespace

from agents.agent_orchestrator import AgentType
from core.chat_service import ChatService
from core.intent_recognizer import IntentCategory, IntentResult, UrgencyLevel
from mcp.admission_data import AdmissionDataStore


class FakeRecognizer:
    async def recognize(self, message, history=None):
        return IntentResult(
            intent=IntentCategory.SCORE_RISK,
            confidence=1.0,
            urgency=UrgencyLevel.LOW,
            entities={
                "province": ["河北"],
                "subject_combination": ["物理"],
                "score": ["620"],
                "rank": ["12000"],
                "major": ["计算机科学与技术"],
            },
            reasoning="test",
            latency_ms=1.0,
        )


class FakeToolManager:
    def __init__(self, store):
        self.store = store
        self.admission_calls = 0

    async def search_with_rewrite(self, *args, **kwargs):
        return SimpleNamespace(success=True, data=[])

    async def call(self, name, params, **kwargs):
        self.admission_calls += 1
        return SimpleNamespace(success=True, data=self.store.query(params))


class FakeDuplicateKnowledgeToolManager:
    async def search_with_rewrite(self, *args, **kwargs):
        return SimpleNamespace(success=True, data=[
            {
                "title": "河北工业大学历年录取成绩官方查询入口",
                "content": "入口说明片段一",
                "source_url": "https://zs.hebut.edu.cn/score/",
                "effective_year": 2026,
                "score": 0.9,
            },
            {
                "title": "河北工业大学历年录取成绩官方查询入口",
                "content": "入口说明片段二",
                "source_url": "https://zs.hebut.edu.cn/score",
                "effective_year": 2026,
                "score": 0.8,
            },
            {
                "title": "河北工业大学历年录取成绩官方查询入口",
                "content": "入口说明片段一",
                "source_url": "https://zs.hebut.edu.cn/score#history",
                "effective_year": 2026,
                "score": 0.7,
            },
        ])


class FakeOrchestrator:
    def __init__(self):
        self.request = None

    async def run(self, request):
        self.request = request
        return SimpleNamespace(
            response="结构化数据回答",
            intent=request.intent,
            agent_type=AgentType.RISK,
            escalated=False,
            entities=request.entities,
        )


class FakeCampusRecognizer:
    async def recognize(self, message, history=None):
        return IntentResult(
            intent=IntentCategory.CAMPUS_LIFE,
            confidence=1.0,
            urgency=UrgencyLevel.LOW,
            entities={},
            reasoning="test",
            latency_ms=1.0,
        )


class FakeEmptyKnowledgeToolManager:
    def __init__(self):
        self.categories = None

    async def search_with_rewrite(self, *args, **kwargs):
        self.categories = kwargs.get("categories")
        return SimpleNamespace(success=True, data=[])


class FakeHallucinatingOrchestrator:
    async def run(self, request):
        return SimpleNamespace(
            response="北辰校区有湖泊和花园，宿舍条件很好[来源1]。",
            intent=request.intent,
            agent_type=AgentType.GENERAL,
            escalated=False,
            entities=request.entities,
        )


class ChatServiceAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_official_retrieval_blocks_specific_campus_claims(self):
        tools = FakeEmptyKnowledgeToolManager()
        service = ChatService(
            FakeHallucinatingOrchestrator(),
            memory=None,
            tool_manager=tools,
            recognizer=FakeCampusRecognizer(),
        )

        result = await service.handle("北辰校区宿舍、食堂和交通怎么样？", "test-user")

        self.assertEqual(tools.categories, ["campus_life"])
        self.assertFalse(result.knowledge_used)
        self.assertIn("没有检索到足以回答", result.response)
        self.assertNotIn("湖泊", result.response)
        self.assertNotIn("[来源1]", result.response)

    async def test_duplicate_rag_sources_share_one_citation_number(self):
        service = ChatService(
            FakeOrchestrator(),
            memory=None,
            tool_manager=FakeDuplicateKnowledgeToolManager(),
            recognizer=None,
        )

        context, used, citations = await service._build_knowledge_context("历年录取成绩")

        self.assertTrue(used)
        self.assertEqual(len(citations), 1)
        self.assertEqual(context.count("[来源1]"), 1)
        self.assertNotIn("[来源2]", context)
        self.assertIn("入口说明片段一", context)
        self.assertIn("入口说明片段二", context)
        self.assertNotIn("来源列表", context)

    async def test_structured_data_is_added_before_risk_agent(self):
        csv_path = Path(__file__).resolve().parents[3] / "data" / "hebut_admission.csv"
        store = AdmissionDataStore(str(csv_path))
        tools = FakeToolManager(store)
        orchestrator = FakeOrchestrator()
        service = ChatService(
            orchestrator,
            memory=None,
            tool_manager=tools,
            recognizer=FakeRecognizer(),
        )

        result = await service.handle(
            "我是2026年河北物理类考生，620分、位次12000，报计算机风险如何？",
            "test-user",
            persist_memory=False,
        )

        self.assertEqual(tools.admission_calls, 1)
        self.assertTrue(result.admission_data_used)
        self.assertTrue(result.knowledge_used)
        self.assertEqual(result.agent_type, "risk")
        self.assertIn("12287", orchestrator.request.context)
        self.assertIn("相对匹配", orchestrator.request.context)
        self.assertEqual(result.citations[-1]["type"], "structured_admission_data")


if __name__ == "__main__":
    unittest.main()
