import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from core.contracts import TaskState
from core.runtime import CURRENT_RUN, RunContext
from core.task_state import merge_state
from memory.conversation_memory import MemoryManager
from memory.preferences import PreferenceSave, PreferenceValue, asks_preferences, extract_candidates
from memory.turn_store import TurnStore


class PreferenceParsingTests(unittest.TestCase):
    def test_source_exact_and_pending_candidate(self):
        message = '我偏好的专业是计算机科学与技术。我的费用偏好是优先低学费。'
        rows = extract_candidates(message)
        self.assertEqual({r['key'] for r in rows}, {'major_preference', 'budget_preference'})
        for row in rows:
            self.assertIn(row['source_excerpt'], message)
            self.assertIn(row['value'], row['source_excerpt'])
        self.assertEqual(rows[0]['value'], '计算机科学与技术')

    def test_no_third_person_negation_quotation_or_hypothetical_capture(self):
        for message in ['帮我朋友问，他喜欢计算机', '我不喜欢计算机', '不要记住我喜欢计算机',
                        '如果我喜欢计算机呢', '他说“我喜欢计算机”', '我喜欢计算机还是电气？']:
            with self.subTest(message=message):
                self.assertEqual(extract_candidates(message), [])

    def test_conditions_credentials_and_blank_are_rejected(self):
        for value in ['位次12000', '分数600', '手机号13800138000', '12000', '  ', 'a\nb']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                PreferenceValue(key='interests', value=value)
        with self.assertRaises(ValueError):
            PreferenceValue(key='rank', value='12000')

    def test_client_cannot_supply_owner_or_fake_source(self):
        for field in ('user_id', 'source_message_id', 'verification_status'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                PreferenceSave(key='interests', value='机器人', expected_revision=0, operation_id='save', **{field: 'forged'})

    def test_preference_read_does_not_capture_policy_question(self):
        self.assertTrue(asks_preferences('我的专业偏好是什么？'))
        self.assertFalse(asks_preferences('计算机专业学费是多少？'))

    def test_other_candidate_scope_persists_until_explicit_return(self):
        state = merge_state(TaskState(entities={'rank': ['12000']}), {}, '帮我朋友问', 'a')
        self.assertFalse(state.subject_is_self)
        self.assertNotIn('rank', state.entities)
        self.assertFalse(merge_state(state, {}, '那电气呢', 'b').subject_is_self)
        self.assertTrue(merge_state(state, {}, '回到我自己', 'c').subject_is_self)


class VerifiedMemoryContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory = MemoryManager.__new__(MemoryManager)
        self.memory.verified_memory_mode = True
        self.memory._get_working_memory = AsyncMock(return_value=[])
        self.memory._redis = SimpleNamespace(get=lambda _: '')
        self.memory._search_episodic = AsyncMock(side_effect=AssertionError('legacy episodic read'))
        self.memory._get_profile = AsyncMock(side_effect=AssertionError('legacy profile read'))
        self.active = {'memory_id': 'm', 'fact_key': 'interests', 'value': '机器人', 'version': 1,
                       'source_message_id': 'source', 'valid_until': '2027-01-01', 'status': 'active'}
        self.memory.facts = SimpleNamespace(snapshot=AsyncMock(return_value={
            'revision': 2, 'items': [self.active, {**self.active, 'status': 'pending', 'value': '飞行器'}]}), capture=AsyncMock())

    async def test_only_confirmed_preferences_are_recalled_with_source(self):
        run = RunContext()
        token = CURRENT_RUN.set(run)
        try:
            ctx = await self.memory.get_context('owner', 'new-conversation', '专业怎么选')
            self.assertEqual(run.memory_revision, 2)
            self.assertEqual([r['value'] for r in ctx.verified_preferences], ['机器人'])
            self.assertEqual(ctx.verified_preferences[0]['source_message_id'], 'source')
            self.assertEqual(ctx.user_profile, {})
            self.assertIn('不是学校事实', ctx.to_prompt_text())
        finally:
            CURRENT_RUN.reset(token)

    async def test_pg_failure_never_falls_back_to_unverified_profiles(self):
        self.memory.facts.snapshot.side_effect = RuntimeError('injected')
        ctx = await self.memory.get_context('owner', 'new', 'hello')
        self.assertEqual(ctx.verified_preferences, [])
        self.assertEqual(ctx.relevant_history, [])
        self.memory._get_profile.assert_not_awaited()

    async def test_other_person_never_receives_own_preferences(self):
        ctx = await self.memory.get_context('owner', 'new', '帮朋友问专业')
        self.assertEqual(ctx.verified_preferences, [])

    async def test_shadow_evaluation_does_not_read_real_preferences(self):
        token = CURRENT_RUN.set(RunContext(allow_long_term=False))
        try:
            ctx = await self.memory.get_context('owner', 'eval', 'hello')
            self.assertEqual(ctx.verified_preferences, [])
            self.memory.facts.snapshot.assert_not_awaited()
        finally:
            CURRENT_RUN.reset(token)

    async def test_candidate_requires_matching_committed_source(self):
        message = '我喜欢计算机'
        client = SimpleNamespace(get=AsyncMock(return_value=None))
        self.memory.turns = SimpleNamespace(client=client, receipt_key=lambda *args: 'receipt', message_hash=TurnStore.message_hash)
        await self.memory.capture_preferences('u', 'c', 'r', message)
        client.get.return_value = json.dumps({'message_hash': 'wrong'})
        await self.memory.capture_preferences('u', 'c', 'r', message)
        self.memory.facts.capture.assert_not_awaited()
        client.get.return_value = json.dumps({'message_hash': TurnStore.message_hash(message), 'memory_revision': 3,
                                              'result': {'trace_id': 'run'}})
        await self.memory.capture_preferences('u', 'c', 'r', message)
        self.memory.facts.capture.assert_awaited_once_with('u', 'c', 'r', 'run', message, 3)

    async def test_pg_mode_never_runs_old_profile_extraction(self):
        self.memory._get_working_memory.side_effect = AssertionError('must not extract')
        await self.memory.update_profile('u', 'c')
        self.memory._get_working_memory.assert_not_awaited()
