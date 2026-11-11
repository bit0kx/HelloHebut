import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from agents.agent_orchestrator import AgentType
from core.chat_service import ChatService
from core.contracts import AnswerVerdict, Evidence, EvidenceVerdict, SupportedClaim, TaskState
from core.intent_recognizer import IntentCategory, IntentResult, UrgencyLevel
from core.runtime import RunContext
from rag.agentic import AgenticRAG, RetrievalOutcome
from workflows.chat_graph import execute
from workflows.checkpoints import JsonOnlySerializer
from workflows.versioning import service_fingerprint, workflow_fingerprint
from test_chat_integration import Recognizer, Tools


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def service(self, *, rag=None, intent=IntentCategory.SCHOOL_INFO):
        answer = SimpleNamespace(response='学校设北辰校区。[来源1]', intent=intent,
                                 agent_type=AgentType.GENERAL, escalated=False)
        orchestrator = SimpleNamespace(run=AsyncMock(return_value=answer))
        memory = SimpleNamespace(get_context=AsyncMock(return_value=None))
        recognizer = SimpleNamespace(recognize=AsyncMock(return_value=IntentResult(
            intent, 1, UrgencyLevel.LOW, {}, 'fixture', 0)))
        return ChatService(orchestrator, memory, None, recognizer=recognizer, agentic_rag=rag, graph_enabled=True)

    def rag(self, verdicts=None):
        evidence = Evidence(evidence_id='e1', content='学校设北辰校区。', content_hash='h', title='校区', year=2026)
        outcome = RetrievalOutcome(evidence=[evidence], verdict=EvidenceVerdict(sufficient=True, decision='answer',
            supported_claims=[SupportedClaim(claim='校区', evidence_ids=['e1'])]), stop_reason='sufficient')
        return SimpleNamespace(retrieve=AsyncMock(return_value=outcome), context=AgenticRAG.context,
            verify_answer=AsyncMock(side_effect=verdicts or [AnswerVerdict(passed=True)]))

    async def test_real_graph_preserves_verification_and_stage_order(self):
        service, run = self.service(rag=self.rag()), RunContext()
        result, state = await execute(service, '学校有哪些校区？', 'u', 'c', TaskState(), run, 'r')
        self.assertTrue(result.verified)
        self.assertEqual(state.revision, 1)
        self.assertEqual([e['stage'] for e in run.events if 'duration_ms' in e],
                         ['route', 'required_tools', 'retrieve', 'answer', 'verify', 'finalize'])
        self.assertEqual(service._orchestrator.run.call_args.args[0].evidence, result.citations)

    async def test_graph_csv_short_path_uses_real_numeric_source(self):
        tools = Tools()
        service = self.service(rag=self.rag())
        service._recognizer, service._tool_manager = Recognizer(), tools
        result = await service.handle('河北物理类位次12000，计算机录取风险如何？', 'u', persist_memory=False)
        self.assertTrue(result.verified)
        self.assertEqual(result.verification_method, 'deterministic_csv')
        self.assertIn('13587', result.response)
        service._orchestrator.run.assert_not_awaited()
        service._rag.retrieve.assert_not_awaited()

    async def test_failed_repair_never_releases_unverified_claim(self):
        rag = self.rag([AnswerVerdict(passed=False, unsupported_claims=['回答包含证据之外的数字：999']),
                        AnswerVerdict(passed=False, unsupported_claims=['仍不受支持'])])
        service = self.service(rag=rag)
        service._orchestrator.run.return_value.response = '学校有999个校区。[来源1]'
        result = await service.handle('学校有哪些校区？', 'u', persist_memory=False)
        self.assertFalse(result.verified)
        self.assertEqual(result.status, 'insufficient_evidence')
        self.assertNotIn('999', result.response)
        self.assertEqual(service._orchestrator.run.await_count, 2)

    async def test_shadow_graph_never_opens_real_ledger(self):
        service = self.service(intent=IntentCategory.OTHER)
        service._memory.turns = SimpleNamespace(receipt=AsyncMock(side_effect=AssertionError('shadow wrote ledger')))
        service._memory.get_context.side_effect = AssertionError('shadow memory read can initialize real state')
        result = await service.handle('你好', 'u', persist_memory=False)
        self.assertEqual(result.status, 'completed')
        service._memory.turns.receipt.assert_not_awaited()
        service._memory.get_context.assert_not_awaited()

    def test_checkpoint_serializer_rejects_objects_and_foreign_formats(self):
        serializer = JsonOnlySerializer()
        payload = {'facts': [{'value': '机器人'}], 'count': 1}
        self.assertEqual(serializer.loads_typed(serializer.dumps_typed(payload)), payload)
        with self.assertRaises(TypeError):
            serializer.dumps_typed(object())
        with self.assertRaises(ValueError):
            serializer.loads_typed(('pickle', b'not-executed'))
        self.assertEqual(json.loads(serializer.dumps_typed(payload)[1]), payload)

    def test_workflow_version_tracks_rules_skills_and_csv_without_absolute_paths(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            roots = [Path(first), Path(second)]
            files = {'core/chat_service.py': 'source', 'core/task_state.py': 'merge',
                     'agents/prompts.py': 'prompt', 'workflows/chat_graph.py': 'graph',
                     'skills/risk/SKILL.md': 'skill', 'skills/risk/references/rules.txt': 'reference',
                     'data/hebut_admission.csv': 'province,year\n河北,2025', 'requirements.txt': 'langgraph==1.2.20'}
            for root in roots:
                for relative, content in files.items():
                    path = root/relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding='utf-8')
            expected = workflow_fingerprint(roots[0])
            self.assertEqual(expected, workflow_fingerprint(roots[1]))
            for relative, content in files.items():
                with self.subTest(dependency=relative):
                    target = roots[0]/relative
                    target.write_text(content+'changed', encoding='utf-8')
                    self.assertNotEqual(expected, workflow_fingerprint(roots[0]))
                    target.write_text(content, encoding='utf-8')
            self.assertNotEqual(expected, workflow_fingerprint(roots[0], settings={'role_mode': 'composed'}))
            (roots[0]/'skills/risk/references/rules.txt').unlink()
            self.assertNotEqual(expected, workflow_fingerprint(roots[0]))

    def test_service_version_tracks_loaded_runtime_rules(self):
        service = self.service()
        initial = service_fingerprint(service)
        service._orchestrator.role_mode = 'composed'
        self.assertNotEqual(initial, service_fingerprint(service))
