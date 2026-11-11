import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import httpx

from agents.agent_orchestrator import AgentType
from core.chat_service import ChatService
from core.contracts import EvidenceVerdict
from core.intent_recognizer import IntentCategory, IntentResult, UrgencyLevel
from core.runtime import RequestConflict, BudgetExceeded
from mcp.admission_data import AdmissionDataStore
from memory.turn_store import TurnStore
from rag.agentic import AgenticRAG
from redis_support import RedisProcess

ROOT = Path(__file__).resolve().parents[3]


class Recognizer:
    async def recognize(self, message, history=None):
        entities = {}
        if '河北' in message:
            entities.update(province=['河北'], subject_combination=['物理'])
        if '12000' in message:
            entities['rank'] = ['12000']
        if '天津' in message:
            entities['province'] = ['天津']
        if '计算机' in message:
            entities['major'] = ['计算机科学与技术']
        if '电气' in message:
            entities.setdefault('major', []).append('电气工程及其自动化')
        return IntentResult(IntentCategory.COMPARISON, 1.0, UrgencyLevel.LOW, entities, 'fixture', 0)


class Tools:
    def __init__(self):
        self.store = AdmissionDataStore(str(ROOT / 'data/hebut_admission.csv'))
        self.calls = []
        self.results = []

    async def call(self, name, params, **kwargs):
        self.calls.append((name, params))
        result = self.store.query(params)
        self.results.append(result)
        return SimpleNamespace(success=True, data=result)

    async def search_with_rewrite(self, *args, **kwargs):
        self.calls.append(('rag', {}))
        return SimpleNamespace(success=True, data=[])


class ChatIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = RedisProcess()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    async def asyncSetUp(self):
        self.turns = TurnStore(self.server.url)
        await self.turns.client.flushdb()
        memory = SimpleNamespace(turns=self.turns, get_context=AsyncMock(return_value=None), maintenance=None)
        self.tools = Tools()
        async def answer(request):
            await asyncio.sleep(.01)
            return SimpleNamespace(response='依据已查资料回答', intent=request.intent,
                                   agent_type=AgentType.PLANNING, escalated=False, entities=request.entities)
        self.orchestrator = SimpleNamespace(run=AsyncMock(side_effect=answer))
        self.service = ChatService(self.orchestrator, memory, self.tools, recognizer=Recognizer())

    async def asyncTearDown(self):
        await self.turns.close()

    async def test_compound_question_fetches_both_majors_before_rag(self):
        result = await self.service.handle('河北物理类位次12000，计算机和电气怎么选？', 'u', 'c', client_request_id='r1')
        self.assertTrue(result.admission_data_used)
        self.assertEqual(self.tools.calls[0][0], 'admission_data_query')
        self.assertEqual({a['major'] for a in self.tools.results[0]['analyses']}, {'计算机科学与技术', '电气工程及其自动化'})

    async def test_scope_change_clarifies_without_reusing_old_rank(self):
        await self.service.handle('河北物理类位次12000，计算机怎么选？', 'u', 'c', client_request_id='r1')
        result = await self.service.handle('换天津考生呢？', 'u', 'c', client_request_id='r2')
        self.assertEqual(result.status, 'clarification')
        self.assertNotIn('rank', result.entities)
        self.assertEqual(len(self.tools.results), 1)
        state = await self.turns.state('u', 'c')
        self.assertEqual(state.entities['province'], ['天津'])

    async def test_required_csv_failure_cannot_fall_through_to_free_generation(self):
        self.tools.call = AsyncMock(return_value=SimpleNamespace(success=False, data=None))
        result = await self.service.handle('河北物理类位次12000，计算机风险如何', 'u', 'c')
        self.assertEqual(result.status, 'insufficient_evidence')
        self.orchestrator.run.assert_not_awaited()

    async def test_unverified_answer_falls_back_to_real_csv_candidates(self):
        self.tools.search_once = AsyncMock(return_value=SimpleNamespace(success=True, data=[]))
        async def insufficient(prompt, schema):
            return EvidenceVerdict(sufficient=False, missing_facts=['专业资料不足'], decision='abstain')
        self.service._rag = AgenticRAG(self.tools, model_json=insufficient)
        self.orchestrator.run.side_effect = None
        self.orchestrator.run.return_value = SimpleNamespace(
            response='录取概率99%[来源999]', intent=IntentCategory.COMPARISON,
            agent_type=AgentType.PLANNING, escalated=False)
        result = await self.service.handle('河北物理类位次12000，推荐哪些专业？', 'u', 'c')
        first = self.tools.results[0]['candidates'][0]
        self.assertEqual(result.status, 'insufficient_evidence')
        self.assertFalse(result.verified)
        self.assertNotIn('99%', result.response)
        self.assertNotIn('[来源999]', result.response)
        self.assertIn(first['major'], result.response)
        self.assertIn(str(first['historical_median_rank']), result.response)
        self.assertIn('[来源1]', result.response)

    async def test_duplicate_request_returns_identical_saved_response(self):
        values = await asyncio.gather(*[self.service.handle('计算机学什么', 'u', 'c', client_request_id='same') for _ in range(3)])
        self.assertEqual(asdict(values[0]), asdict(values[1]))
        self.assertEqual(self.orchestrator.run.await_count, 1)
        self.assertEqual(await self.turns.client.llen(self.turns.key('wm', 'u', 'c')), 2)
        with self.assertRaises(RequestConflict):
            await self.service.handle('电气学什么', 'u', 'c', client_request_id='same')

    async def test_new_conversation_retry_has_stable_id(self):
        first = await self.service.handle('你好', 'u', client_request_id='start')
        second = await self.service.handle('你好', 'u', client_request_id='start')
        self.assertEqual(first.conv_id, second.conv_id)
        self.assertEqual(self.orchestrator.run.await_count, 1)

    async def test_timeout_does_not_write_half_a_turn(self):
        self.service._timeout_s = .04
        async def slow(request):
            await asyncio.sleep(.3)
        self.orchestrator.run.side_effect = slow
        with self.assertRaises((TimeoutError, BudgetExceeded)):
            await self.service.handle('计算机学什么', 'u', 'c', client_request_id='slow')
        self.assertEqual(await self.turns.client.llen(self.turns.key('wm', 'u', 'c')), 0)
        self.assertIsNone(await self.turns.receipt('u', 'c', 'slow', '计算机学什么'))

    async def test_trace_does_not_save_user_message_or_identity(self):
        directory = ROOT / 'experiments/v2/tmp'
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as path:
            self.service._trace_dir = path
            result = await self.service.handle('private-message-xyz', 'private-user-xyz', 'c')
            trace = (Path(path) / (result.trace_id + '.json')).read_text(encoding='utf-8')
            self.assertNotIn('private-message-xyz', trace)
            self.assertNotIn('private-user-xyz', trace)
            self.assertIn('commit', trace)

    async def test_http_session_binding_and_conflict(self):
        import api.main as api
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test') as client:
            with patch.object(api, '_chat_service', self.service), patch.object(api, '_memory', self.service._memory):
                self.assertEqual((await client.post('/chat', json={'message': '你好'})).status_code, 401)
                session = (await client.post('/session')).json()
                headers = {'Authorization': 'Bearer ' + session['session_token']}
                forged = await client.post('/chat', headers=headers, json={'message': '你好', 'user_id': 'victim'})
                self.assertEqual(forged.status_code, 403)
                payload = {'message': '你好', 'client_request_id': 'http-1'}
                response = await client.post('/chat', headers=headers, json=payload)
                self.assertEqual(response.status_code, 200)
                replay = await client.post('/chat', headers=headers, json=payload)
                self.assertEqual(response.json(), replay.json())
                conflict = await client.post('/chat', headers=headers, json={**payload, 'message': '不同内容'})
                self.assertEqual(conflict.status_code, 409)
