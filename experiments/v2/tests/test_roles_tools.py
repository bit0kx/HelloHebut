import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agents.agent_orchestrator import AgentOrchestrator, AgentType, AgentResponse, GeneralAgent, Request
from agents.composer import Claim, RoleResult, ResponseComposer, validate_role_result
from agents.read_tools import register_read_tools
from core.context_budget import ContextBudgetExceeded, bounded_json, fit_context, check_tool_transcript
from core.intent_recognizer import IntentCategory, UrgencyLevel
from core.runtime import CURRENT_RUN, RunContext, BudgetExceeded
from tooling.tool_manager import MCPToolManager, Tool


def reply(*blocks, stop='end_turn'):
    return SimpleNamespace(content=list(blocks), stop_reason=stop)


class ComposerTests(unittest.TestCase):
    def test_duplicate_claims_merge_sources_and_conflicting_facts_are_removed(self):
        a = RoleResult(status='completed', claims=[
            Claim(statement='校区位于天津。', evidence_ids=['source:1'], fact_key='campus:city'),
            Claim(statement='费用为100。', evidence_ids=['source:1'], fact_key='fee')])
        b = RoleResult(status='completed', claims=[
            Claim(statement='校区位于天津', evidence_ids=['source:2'], fact_key='campus:city'),
            Claim(statement='费用为200。', evidence_ids=['source:2'], fact_key='fee')])
        text, meta = ResponseComposer().compose([a,b])
        self.assertEqual(text.count('校区位于天津'), 1)
        self.assertIn('[来源1][来源2]', text)
        self.assertNotIn('费用为100', text)
        self.assertNotIn('费用为200', text)
        self.assertEqual(meta['status'], 'conflict')

    def test_unknown_reference_and_fact_disguised_as_advice_are_rejected(self):
        for claim in [Claim(statement='学校事实', evidence_ids=['source:9'], fact_key='x'),
                      Claim(statement='学校事实', fact_key='x'),
                      Claim(statement='学费1000', kind='advice')]:
            with self.assertRaises(ValueError):
                validate_role_result(RoleResult(status='completed', claims=[claim]), [{}])

    def test_decimal_values_are_not_deduplicated(self):
        claims = [Claim(statement='费用1.5', evidence_ids=['source:1'], fact_key='a'),
                  Claim(statement='费用15', evidence_ids=['source:1'], fact_key='b')]
        text,_ = ResponseComposer().compose([RoleResult(status='completed', claims=claims)])
        self.assertIn('费用1.5',text)
        self.assertIn('费用15',text)


class ContextBudgetTests(unittest.TestCase):
    def test_required_csv_never_truncates_and_large_input_is_explicit(self):
        with self.assertRaises(ContextBudgetExceeded):
            fit_context('rules', 'question', '{"records":' + '1234567890'*50 + '}', max_input_tokens=200)
        raw = bounded_json({'records': list(range(1000))}, 100)
        self.assertLessEqual(len(raw.encode('utf-8')),100)
        self.assertEqual(json.loads(raw)['error'], 'tool_result_budget_exceeded')
        self.assertTrue(json.loads(raw)['truncated'])

    def test_history_is_trimmed_as_complete_turns(self):
        history=[{'role':'user','content':'old'*30},{'role':'assistant','content':'answer'*30},
                 {'role':'user','content':'new'},{'role':'assistant','content':'last'},
                 {'role':'user','content':'unmatched'}]
        _,messages,meta=fit_context('rules','current',history=history,max_input_tokens=260)
        self.assertEqual([m['content'] for m in messages],['new','last','current'])
        self.assertEqual(meta['history_turns_dropped'],1)

    def test_tool_transcript_is_not_partially_trimmed(self):
        transcript=[{'role':'assistant','content':[{'type':'tool_use','id':'t','input':{}}]},
                    {'role':'user','content':[{'type':'tool_result','tool_use_id':'t','content':'x'*400}]}]
        with self.assertRaises(ContextBudgetExceeded):
            check_tool_transcript('rules',transcript,[],100)
        self.assertEqual(len(transcript),2)


class RoleToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager=MCPToolManager('fixture')
        register_read_tools(self.manager)
        self.agent=GeneralAgent(object(),'fixture')
        self.agent._tool_manager=self.manager
        self.agent._tool_loop_enabled=True
        self.agent._max_input_tokens=12000

    async def asyncTearDown(self):
        await self.manager._client.close()

    async def test_tool_exchange_is_paired_and_only_reads_current_evidence(self):
        responses=[reply({'type':'tool_use','id':'read1','name':'request_evidence_read','input':{'sources':[1]}},stop='tool_use'),
                   reply({'type':'text','text':'天津[来源1]'})]
        snapshots=[]
        async def call(client,**kwargs):
            snapshots.append(json.loads(json.dumps(kwargs)))
            return responses.pop(0)
        req=Request('哪里','u','c',evidence=[{'source_excerpt':'天津'}])
        run=RunContext()
        token=CURRENT_RUN.set(run)
        try:
            with patch('agents.agent_orchestrator.create_message',side_effect=call):
                self.assertEqual(await self.agent._call_llm(req),'天津[来源1]')
            messages=snapshots[1]['messages']
            self.assertEqual(messages[-2]['content'][0]['id'],'read1')
            self.assertEqual(messages[-1]['content'][0]['tool_use_id'],'read1')
            self.assertIn('天津',messages[-1]['content'][0]['content'])
            self.assertEqual(run.tool_calls,1)
        finally:
            CURRENT_RUN.reset(token)

    async def test_signed_thinking_is_transported_only_inside_the_tool_exchange(self):
        snapshots=[]
        thinking={'type':'thinking','thinking':'opaque-provider-block','signature':'test-signature'}
        responses=[reply(thinking,{'type':'tool_use','id':'t','name':'request_evidence_read','input':{'sources':[1]}},stop='tool_use'),
                   reply({'type':'text','text':'final text'})]
        async def call(client,**kwargs):
            snapshots.append(json.loads(json.dumps(kwargs)))
            return responses.pop(0)
        with patch('agents.agent_orchestrator.create_message',side_effect=call):
            result=await self.agent._call_llm(Request('测试','u','c',evidence=[{'content':'data'}]))
        self.assertEqual(result,'final text')
        self.assertEqual(snapshots[1]['messages'][-2]['content'][0],thinking)

    async def test_private_tools_do_not_cache_across_users(self):
        first=await self.manager.call_read_only('request_evidence_read',{'sources':[1]},'general',
                                              context={'evidence':[{'content':'alice'}]})
        second=await self.manager.call_read_only('request_evidence_read',{'sources':[1]},'general',
                                               context={'evidence':[{'content':'bob'}]})
        self.assertEqual(first.data['evidence'][0]['content'],'alice')
        self.assertEqual(second.data['evidence'][0]['content'],'bob')
        self.assertFalse(first.cached or second.cached)

    async def test_model_cannot_select_other_registered_tool(self):
        dangerous=AsyncMock(return_value={'ok':True})
        self.manager.register(Tool('dangerous','no',dangerous,{'type':'object'},read_only=True))
        responses=[reply({'type':'tool_use','id':'t','name':'dangerous','input':{}},stop='tool_use'),
                   reply({'type':'text','text':'没有执行'})]
        with patch('agents.agent_orchestrator.create_message',AsyncMock(side_effect=responses)):
            await self.agent._call_llm(Request('测试','u','c'))
        dangerous.assert_not_awaited()

    async def test_three_model_rounds_bound_even_repeated_tool_requests(self):
        responses=[reply({'type':'tool_use','id':str(i),'name':'request_evidence_read','input':{'sources':[1]}},stop='tool_use') for i in range(3)]
        call=AsyncMock(side_effect=responses)
        with patch('agents.agent_orchestrator.create_message',call), self.assertRaises(ValueError):
            await self.agent._call_llm(Request('测试','u','c',evidence=[{'content':'evidence'}]))
        self.assertEqual(call.await_count,3)

    async def test_disabled_tool_loop_uses_fixed_generation_path(self):
        self.agent._tool_loop_enabled=False
        call=AsyncMock(return_value=reply({'type':'text','text':'fixed'}))
        with patch('agents.agent_orchestrator.create_message',call):
            await self.agent._call_llm(Request('hello','u','c'))
        self.assertNotIn('tools',call.call_args.kwargs)

    async def test_composed_mode_caps_roles_and_uses_structured_results(self):
        orchestrator=AgentOrchestrator('fixture',role_mode='composed')
        calls=[]
        async def execute(req,role):
            calls.append((role,req.structured_output,req.role_task))
            return AgentResponse(role,'unused',True,role_result=RoleResult(status='completed',claims=[
                Claim(statement='共同事实',fact_key='same',evidence_ids=['source:1'])]))
        orchestrator._execute=execute
        result=await orchestrator.run(Request('位次和学费、就业对比','u','c',intent=IntentCategory.COMPARISON,
            urgency=UrgencyLevel.LOW,evidence=[{'content':'共同事实'}]))
        self.assertEqual(len(calls),2)
        self.assertTrue(all(c[1] and c[2] for c in calls))
        self.assertEqual(result.response.count('共同事实'),1)
        self.assertEqual(result.collaboration_status,'completed')

    async def test_roles_share_the_same_request_model_budget(self):
        orchestrator=AgentOrchestrator('fixture',role_mode='composed')
        original_clients={id(agent._client):agent._client for pool in orchestrator._pool.values() for agent in pool}
        original_clients[id(orchestrator._intent_recognizer.client)]=orchestrator._intent_recognizer.client
        fake=SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=reply(
            {'type':'text','text':'{"status":"partial","missing_facts":["资料不足"]}'}))))
        for pool in orchestrator._pool.values():
            for agent in pool:
                agent._client=fake
        run=RunContext(max_models=1)
        token=CURRENT_RUN.set(run)
        try:
            with self.assertRaises(BudgetExceeded):
                await orchestrator.run(Request('位次和就业规划','u','c',intent=IntentCategory.SCORE_RISK,urgency=UrgencyLevel.LOW))
            self.assertEqual(run.model_calls,1)
            self.assertEqual(fake.messages.create.await_count,1)
        finally:
            CURRENT_RUN.reset(token)
            for client in original_clients.values():
                await client.close()
