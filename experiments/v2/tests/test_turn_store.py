import asyncio
from datetime import datetime
import json
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
import redis

from core.contracts import TaskState
from core.runtime import LeaseLost, RequestConflict
from memory.turn_store import TurnStore, MaintenanceQueue
from memory.conversation_memory import MemoryManager
from redis_support import RedisProcess


class TurnStoreTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = RedisProcess()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    async def asyncSetUp(self):
        self.store = TurnStore(self.server.url)
        await self.store.client.flushdb()  # This database belongs only to the process above.

    async def asyncTearDown(self):
        await self.store.close()

    async def test_atomic_turn_order_state_and_receipt(self):
        async with self.store.guard('u', 'c') as lease:
            await self.store.commit('u', 'c', 'r1', '问题', {'response': '回答'}, TaskState(revision=1), lease)
        raws = await self.store.client.lrange(self.store.key('wm', 'u', 'c'), 0, -1)
        self.assertEqual([json.loads(r)['role'] for r in raws], ['assistant', 'user'])
        self.assertEqual((await self.store.state('u', 'c')).revision, 1)
        self.assertEqual(await self.store.receipt('u', 'c', 'r1', '问题'), {'response': '回答'})
        with self.assertRaises(RequestConflict):
            await self.store.receipt('u', 'c', 'r1', '不同问题')

    async def test_expired_lease_cannot_commit(self):
        async with self.store.guard('u', 'c', lease_s=.03) as lease:
            await asyncio.sleep(.06)
            with self.assertRaises(LeaseLost):
                await self.store.commit('u', 'c', 'r1', '问题', {'response': '回答'}, TaskState(), lease)
        self.assertEqual(await self.store.client.llen(self.store.key('wm', 'u', 'c')), 0)

    async def test_legacy_task_requires_explicit_scope_before_preferences(self):
        await self.store.client.set(self.store.key('state', 'u', 'old'), json.dumps(
            {'schema_version': 1, 'revision': 2, 'entities': {'rank': ['12000']}}))
        state = await self.store.state('u', 'old')
        self.assertEqual(state.schema_version, 2)
        self.assertFalse(state.subject_is_self)
        self.assertEqual(state.entities['rank'], ['12000'])
        self.assertTrue((await self.store.state('u', 'new')).subject_is_self)

    async def test_concurrent_duplicate_commits_only_once(self):
        async def submit():
            async with self.store.guard('u', 'c') as lease:
                if not await self.store.receipt('u', 'c', 'r1', '问题'):
                    await asyncio.sleep(.03)
                    await self.store.commit('u', 'c', 'r1', '问题', {'response': '回答'}, TaskState(), lease)
        await asyncio.gather(submit(), submit(), submit())
        self.assertEqual(await self.store.client.llen(self.store.key('wm', 'u', 'c')), 2)

    async def test_ledger_survives_client_restart_and_clear_removes_receipts(self):
        async with self.store.guard('u', 'c') as lease:
            await self.store.commit('u', 'c', 'r1', '问题', {'response': '回答'}, TaskState(), lease)
        other = TurnStore(self.server.url)
        try:
            self.assertIsNotNone(await other.receipt('u', 'c', 'r1', '问题'))
            await other.clear('u', 'c')
            self.assertIsNone(await self.store.receipt('u', 'c', 'r1', '问题'))
        finally:
            await other.close()

    async def test_namespace_prevents_delimiter_collision_and_cross_user_read(self):
        self.assertNotEqual(self.store.key('wm', 'a:b', 'c'), self.store.key('wm', 'a', 'b:c'))
        async with self.store.guard('alice', 'c') as lease:
            await self.store.commit('alice', 'c', 'r1', '问题', {'response': '回答'}, TaskState(), lease)
        self.assertIsNone(await self.store.receipt('bob', 'c', 'r1', '问题'))

    async def test_sessions_are_server_bound(self):
        first, second = await self.store.create_session(), await self.store.create_session()
        self.assertNotEqual(first['user_id'], second['user_id'])
        self.assertEqual(await self.store.session_user(first['session_token']), first['user_id'])
        self.assertIsNone(await self.store.session_user('forged'))

    def memory(self):
        memory = MemoryManager.__new__(MemoryManager)
        memory._redis = redis.from_url(self.server.url, decode_responses=True)
        memory._client, memory._model = object(), 'test'
        memory._store_episodic = AsyncMock()
        return memory

    async def seed(self, memory, turns=8):
        for n in range(turns):
            async with self.store.guard('u', 'c') as lease:
                await self.store.commit('u', 'c', str(n), f'q{n}', {'response': f'a{n}'}, TaskState(), lease)

    async def test_summary_failure_preserves_all_raw_messages(self):
        memory = self.memory()
        try:
            await self.seed(memory)
            before = memory._redis.lrange(memory._wm_key('u', 'c'), 0, -1)
            with patch('memory.conversation_memory.create_message', AsyncMock(side_effect=ValueError('offline'))):
                await memory._compress('u', 'c')
            self.assertEqual(memory._redis.lrange(memory._wm_key('u', 'c'), 0, -1), before)
            self.assertIsNone(memory._redis.get(memory._summary_key('u', 'c')))
        finally:
            memory._redis.close()

    async def test_compression_keeps_new_arrivals_in_correct_order(self):
        memory = self.memory()
        try:
            await self.seed(memory)
            async def summarise(*args, **kwargs):
                async with self.store.guard('u', 'c') as lease:
                    await self.store.commit('u', 'c', 'new', 'q-new', {'response': 'a-new'}, TaskState(), lease)
                return SimpleNamespace(content=[SimpleNamespace(type='text', text='已总结旧对话')])
            with patch('memory.conversation_memory.create_message', summarise):
                await memory._compress('u', 'c')
            messages = await memory._get_working_memory('u', 'c')
            self.assertEqual([m.content for m in messages], ['q5', 'a5', 'q6', 'a6', 'q7', 'a7', 'q-new', 'a-new'])
        finally:
            memory._redis.close()

    async def test_maintenance_queue_is_bounded_and_coalesces(self):
        queue = MaintenanceQueue(AsyncMock(), capacity=1)
        self.assertTrue(queue.submit('u', 'c'))
        self.assertFalse(queue.submit('u', 'c'))
        self.assertFalse(queue.submit('u2', 'c2'))
