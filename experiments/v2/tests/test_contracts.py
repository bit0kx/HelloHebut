import unittest
from unittest.mock import AsyncMock

from core.contracts import TaskState, EvidenceVerdict
from core.task_state import merge_state, needs_admission
from core.runtime import CURRENT_RUN, RunContext, BudgetExceeded
from mcp.tool_manager import MCPToolManager, Tool
from mcp.knowledge_indexing import split_text
from mcp.knowledge_base import KnowledgeBase


class StateTests(unittest.TestCase):
    def test_scope_change_invalidates_old_rank(self):
        previous = TaskState(entities={'province': ['河北'], 'rank': ['12000'], 'subject_combination': ['物理']})
        result = merge_state(previous, {'province': ['天津']}, '换天津考生呢', 'r2')
        self.assertNotIn('rank', result.entities)
        self.assertNotIn('subject_combination', result.entities)
        self.assertEqual(previous.entities['rank'], ['12000'])
        self.assertEqual(result.sources['province'], 'r2')

    def test_explicit_new_rank_survives_scope_change(self):
        previous = TaskState(entities={'province': ['河北'], 'rank': ['12000']})
        result = merge_state(previous, {'province': ['天津'], 'rank': ['8000']}, '天津位次8000', 'r2')
        self.assertEqual(result.entities['rank'], ['8000'])

    def test_followup_retains_conditions_but_new_candidate_does_not(self):
        previous = TaskState(entities={'province': ['河北'], 'rank': ['12000'], 'major': ['计算机']})
        result = merge_state(previous, {'major': ['电气']}, '电气呢', 'r2')
        self.assertEqual(result.entities['rank'], ['12000'])
        self.assertEqual(result.entities['major'], ['电气'])
        self.assertFalse(merge_state(previous, {}, '帮另一个人问', 'r3').entities)

    def test_numeric_comparison_requires_csv(self):
        self.assertTrue(needs_admission('位次12000，计算机和电气怎么选', 'comparison', {}))
        self.assertFalse(needs_admission('计算机学什么', 'major_info', {}))

    def test_verdict_boolean_is_not_coerced(self):
        with self.assertRaises(ValueError):
            EvidenceVerdict.model_validate({'sufficient': 'true', 'decision': 'answer'})

    def test_long_sentence_has_hard_cap_and_preserves_tail(self):
        chunks = split_text('甲' * 1700 + '尾', 500)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        self.assertTrue(chunks[-1].endswith('尾'))

    def test_year_filter_is_combined_with_category(self):
        self.assertEqual(KnowledgeBase._category_where(['tuition'], 2025),
                         {'$and': [{'category': 'tuition'}, {'effective_year': 2025}]})


class ToolContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = MCPToolManager('test-key')
        self.handler = AsyncMock(return_value={'ok': True})
        self.manager.register(Tool('lookup', 'test', self.handler, {
            'type': 'object', 'properties': {'items': {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 1}},
            'required': ['items'], 'additionalProperties': False}, allowed_agents=('risk',)))

    async def asyncTearDown(self):
        await self.manager._client.close()

    async def test_nested_invalid_argument_never_executes(self):
        for params in ({'items': ['1']}, {'items': [True]}, {'items': [], 'extra': 1}):
            result = await self.manager.call('lookup', params, {'agent_type': 'risk'})
            self.assertFalse(result.success)
        self.handler.assert_not_awaited()

    async def test_role_permissions_precede_execution(self):
        result = await self.manager.call('lookup', {'items': [1]}, {'agent_type': 'general'})
        self.assertFalse(result.success)
        self.handler.assert_not_awaited()

    async def test_request_tool_budget_cannot_be_bypassed(self):
        token = CURRENT_RUN.set(RunContext(max_tools=1))
        try:
            await self.manager.call('lookup', {'items': [1]}, {'agent_type': 'risk'})
            with self.assertRaises(BudgetExceeded):
                await self.manager.call('lookup', {'items': [1]}, {'agent_type': 'risk'})
        finally:
            CURRENT_RUN.reset(token)
