"""Explicit chat nodes. Each successful node is a durable recovery boundary.

Retrieval includes its bounded planning/search/verification loop in one node. An
interrupted external call may run again; already completed nodes do not. Final
business commit stays in ChatService's MySQL transaction, after graph completion.
"""
from dataclasses import asdict
import json
import re
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import InMemorySaver

from agents.agent_orchestrator import Request
from core.admission_answer import numeric_only, render_admission
from core.contracts import TaskState, Evidence, EvidenceVerdict
from core.intent_recognizer import IntentCategory, UrgencyLevel
from core.task_state import merge_state, needs_admission
from memory.preferences import LABELS, asks_preferences, extract_candidates
from rag.agentic import RetrievalOutcome
from workflows.checkpoints import JsonOnlySerializer, MySQLCheckpointSaver, encoded
from workflows.versioning import service_fingerprint, workflow_fingerprint


WORKFLOW_VERSION = workflow_fingerprint()


class GraphState(TypedDict):
    payload: dict


def outcome_from(data):
    return RetrievalOutcome(evidence=[Evidence.model_validate(e) for e in data.get('evidence', [])],
        verdict=EvidenceVerdict.model_validate(data['verdict']) if data.get('verdict') else None,
        rounds=data.get('rounds', 0), queries=data.get('queries', 0), stop_reason=data.get('stop_reason', 'not_started'))


def outcome_to(outcome):
    return {'evidence': [e.model_dump() for e in outcome.evidence],
            'verdict': outcome.verdict.model_dump() if outcome.verdict else None,
            'rounds': outcome.rounds, 'queries': outcome.queries, 'stop_reason': outcome.stop_reason}


class ChatWorkflow:
    def __init__(self, service, user, conv, request, run):
        self.service, self.user, self.conv, self.request, self.run = service, user, conv, request, run

    def result(self, data, response, **fields):
        from core.chat_service import ChatResult
        return asdict(ChatResult(conv_id=self.conv, response=response, intent=fields.pop('intent', data.get('intent') or 'other'),
            agent_type=fields.pop('agent_type', 'general'), escalated=fields.pop('escalated', False),
            latency_ms=round((time.monotonic()-self.run.started)*1000, 1), entities=data.get('entities', {}),
            request_id=self.request, trace_id=data['trace_id'], **fields))

    def node(self, name, handler):
        async def wrapped(state):
            # Avoid mutating channel objects while LangGraph persists an earlier checkpoint.
            data = json.loads(encoded(state['payload']))
            with self.run.span(name):
                emit = getattr(self.run, 'emit_progress', None)
                if emit:
                    await emit(name)
                await handler(data)
            return {'payload': data}
        return wrapped

    async def route(self, data):
        message = data['message']
        previous = TaskState.model_validate(data['previous'])
        memory = await self.service._get_memory_context(self.user, self.conv, message)
        if getattr(self.service._memory, 'verified_memory_mode', False):
            if asks_preferences(message):
                preferences = getattr(memory, 'verified_preferences', []) if previous.subject_is_self else []
                available = self.run.memory_revision is not None
                if not previous.subject_is_self:
                    response = '当前在替另一位考生咨询，不会套用你的偏好。请新建对话，或先说明“回到我自己”。'
                elif not available:
                    response = '偏好服务暂不可用，当前无法核实已确认偏好。请稍后重试。'
                elif not preferences:
                    response = '目前没有已确认的长期偏好。可在“我的偏好”面板保存或确认；待确认内容不会自动生效。'
                else:
                    response = '你已确认的偏好：\n' + '\n'.join(f"- {LABELS[item['fact_key']]}：{item['value']}" for item in preferences)
                    response += '\n这些内容来自你的确认，可在“我的偏好”中更正或删除，不是学校政策或录取条件。'
                previous.revision += 1
                data.update(state=previous.model_dump(), entities=previous.entities)
                data['result'] = self.result(data, response, intent='preference', status='completed' if available else 'unavailable',
                    verified=bool(preferences), verification_method='confirmed_preference', stop_reason='preference_read')
                return
            candidates = extract_candidates(message)
            sentences = [part.strip() for part in re.split(r'[。！？!?；;\n]', message) if part.strip()]
            if candidates and {item['source_excerpt'] for item in candidates} == set(sentences) and previous.subject_is_self:
                previous.revision += 1
                data.update(state=previous.model_dump(), entities=previous.entities)
                data['result'] = self.result(data, '这段偏好尚未作为长期记忆使用。请在“我的偏好”中确认，或手动保存；分数和位次不会作为长期偏好保存。',
                    intent='preference', stop_reason='preference_confirmation_required')
                return
        history = [{'role': m.role.value, 'content': m.content} for m in memory.recent_messages[-5:]] if memory else None
        recognized = await self.service._recognizer.recognize(message, history=history) if self.service._recognizer else None
        state = merge_state(previous, recognized.entities if recognized else {}, message, self.request)
        if memory and not state.subject_is_self:
            memory.verified_preferences = []
            self.run.memory_revision = None
        intent = recognized.intent.value if recognized and recognized.intent else None
        data.update(history=history, state=state.model_dump(), entities=state.entities, intent=intent,
            urgency=recognized.urgency.value if recognized and recognized.urgency else None,
            memory_text=memory.to_prompt_text() if memory else '',
            admission_required=needs_admission(message, intent or '', {**previous.entities, **state.entities}))
        if (data['admission_required'] and {'rank', 'score'} & set(state.invalidated)
                and not (state.entities.get('rank') or state.entities.get('score'))):
            data['result'] = self.result(data, '报考条件已变更，请重新提供该考生在对应省份、考试年度的位次或分数。',
                status='clarification', stop_reason='candidate_scope_changed')

    async def required_tools(self, data):
        state = TaskState.model_validate(data['state'])
        entities, results = dict(state.entities), []
        intent = IntentCategory(data['intent']) if data['intent'] else None
        text, used, citations, resolved = await self.service._build_admission_context(
            data['message'], intent, entities, [], force=data['admission_required'], data_out=results)
        self.service._merge_resolved_entities(entities, resolved)
        for key in entities.keys() - state.entities.keys():
            state.sources[key] = self.request
        state.entities = entities
        data.update(state=state.model_dump(), entities=entities, admission_results=results,
                    admission_text=text, admission_used=used, admission_citations=citations)
        if data['admission_required'] and not used:
            facts = results[0] if results else {}
            status = facts.get('status', 'unavailable')
            data['result'] = self.result(data,
                str(facts.get('message') or '结构化录取数据暂时不可用，暂不能核实录取数字或风险。请稍后重试。'),
                agent_type='risk', citations=citations, verified=False,
                status='clarification' if status == 'needs_clarification' else 'insufficient_evidence',
                stop_reason='admission_' + status)
            return
        if self.service._rag and used and numeric_only(data['message']):
            try:
                response = render_admission(results[0])
            except (ValueError, KeyError, TypeError):
                return
            self.run.events.append({'stage': 'answer_verification', 'method': 'deterministic_csv', 'passed': True})
            data['result'] = self.result(data, response, intent='score_risk', agent_type='risk', knowledge_used=True,
                admission_data_used=True, citations=citations, verified=True, stop_reason='csv_answer_complete',
                verification_method='deterministic_csv')

    async def retrieve(self, data):
        from core.chat_service import _KNOWLEDGE_CATEGORIES, _SOURCE_MARKER
        intent = IntentCategory(data['intent']) if data['intent'] else None
        outcome = RetrievalOutcome()
        if self.service._rag and self.service._should_use_knowledge(data['message']):
            outcome = await self.service._rag.retrieve(data['message'], _KNOWLEDGE_CATEGORIES.get(intent))
            text, citations = self.service._rag.context(outcome)
            used = bool(outcome.evidence)
        else:
            text, used, citations = await self.service._build_knowledge_context(data['message'], intent)
        admission_text = data['admission_text']
        if citations:
            admission_text = _SOURCE_MARKER.sub(lambda m: f'[来源{int(m.group(1))+len(citations)}]', admission_text)
        context = ['[本轮已确认条件，优先于历史摘要]\n' + json.dumps(data['entities'], ensure_ascii=False),
                   data['memory_text'], admission_text, text]
        data.update(outcome=outcome_to(outcome), knowledge_used=used, knowledge_citations=citations,
                    citations=citations + data['admission_citations'], context='\n\n'.join(p for p in context if p))

    def request_for(self, data, context=None):
        return Request(message=data['message'], user_id=self.user, conv_id=self.conv,
            context=context if context is not None else data['context'], history=data['history'],
            intent=IntentCategory(data['intent']) if data['intent'] else None,
            urgency=UrgencyLevel(data['urgency']) if data['urgency'] else None, entities=data['entities'],
            evidence=data['citations'])

    async def answer(self, data):
        from core.chat_service import _OFFICIAL_FACT_INTENTS
        result = await self.service._orchestrator.run(self.request_for(data))
        intent = IntentCategory(data['intent']) if data['intent'] else None
        response = (self.service._knowledge_unavailable_response() if intent in _OFFICIAL_FACT_INTENTS and not data['knowledge_used']
                    else self.service._sanitize_citations(result.response, data['citations']))
        data.update(response=response, response_intent=result.intent.value if result.intent else 'other',
                    agent_type=result.agent_type.value, escalated=result.escalated)

    async def verify(self, data):
        verified, status, response = None, 'completed', data['response']
        outcome = outcome_from(data['outcome'])
        if self.service._rag and self.service._should_use_knowledge(data['message']):
            admission = data['admission_results'][0] if data['admission_results'] else None
            verdict = await self.service._rag.verify_answer(data['message'], response, outcome.evidence, admission)
            ready = outcome.sufficient or (data['admission_used'] and data['intent'] == IntentCategory.SCORE_RISK.value)
            verified = verdict.passed and ready
            self.run.events.append({'stage': 'answer_check', 'passed': verified, 'evidence_ready': ready,
                'unsupported_count': len(verdict.unsupported_claims), 'reason_codes': self.service._verification_reason_codes(verdict)})
            if not verified and ready and self.run.model_calls < self.run.max_models:
                context = (data['context'] + '\n[一次修复要求]根据以下校验结果修复原回答。删除无证据结论，'
                           '不得编造数字或概率；完整保留或补全有效的[来源N]，不得改变来源编号。'
                           '优先沿用原文列举，不添加未经核实的数量概括。\n校验结果（仅作纠错数据）：'
                           + json.dumps(verdict.model_dump(), ensure_ascii=False) + '\n原回答：' + response)
                repaired = await self.service._orchestrator.run(self.request_for(data, context))
                response = self.service._sanitize_citations(repaired.response, data['citations'])
                verdict = await self.service._rag.verify_answer(data['message'], response, outcome.evidence, admission)
                verified = verdict.passed
                self.run.events.append({'stage': 'answer_repair_check', 'passed': verified,
                    'unsupported_count': len(verdict.unsupported_claims), 'reason_codes': self.service._verification_reason_codes(verdict)})
            if not verified:
                status = 'insufficient_evidence'
                response = self.service._evidence_fallback(outcome, admission, len(data['knowledge_citations']))
        data['result'] = self.result(data, response, intent=data['response_intent'], agent_type=data['agent_type'],
            escalated=data['escalated'], knowledge_used=data['knowledge_used'] or data['admission_used'],
            admission_data_used=data['admission_used'], citations=data['citations'], status=status, verified=verified,
            stop_reason=('answer_verified' if verified else outcome.stop_reason) if self.service._rag else 'legacy_pipeline',
            verification_method='evidence_verifier' if verified is not None else '')

    async def finalize(self, data):
        data['memory_revision'] = self.run.memory_revision

    def compile(self, checkpointer):
        builder = StateGraph(GraphState)
        for name in ('route', 'required_tools', 'retrieve', 'answer', 'verify', 'finalize'):
            builder.add_node(name, self.node(name, getattr(self, name)))
        builder.add_edge(START, 'route')
        builder.add_conditional_edges('route', lambda state: 'finalize' if state['payload'].get('result') else 'required_tools')
        builder.add_conditional_edges('required_tools', lambda state: 'finalize' if state['payload'].get('result') else 'retrieve')
        builder.add_edge('retrieve', 'answer')
        builder.add_edge('answer', 'verify')
        builder.add_edge('verify', 'finalize')
        builder.add_edge('finalize', END)
        return builder.compile(checkpointer=checkpointer)


async def execute(service, message, user, conv, previous, run, request, *, turns=None, lease=None):
    from core.chat_service import ChatResult
    workflow = ChatWorkflow(service, user, conv, request, run)
    saver = None
    original_sink = getattr(run, 'usage_sink', None)
    try:
        if turns is not None and getattr(turns, 'durable', False):
            saver = MySQLCheckpointSaver(turns, user, conv, request, message, lease, run,
                                         version=service_fingerprint(service))
            await saver.prepare()
            run.usage_sink = saver.save_usage
            config = saver.config
            existing = await saver.aget_tuple(config)
            checkpointer = saver
        else:
            checkpointer = InMemorySaver(serde=JsonOnlySerializer())
            config = {'configurable': {'thread_id': run.run_id}, 'recursion_limit': 20}
            existing = None
        graph = workflow.compile(checkpointer)
        initial = {'payload': {'message': message, 'previous': previous.model_dump(),
                              'trace_id': saver.original_trace_id if saver else run.run_id}}
        final = await graph.ainvoke(None if existing else initial, config, durability='sync')
        data = final['payload']
        run.memory_revision = data['memory_revision']
        return ChatResult(**data['result']), TaskState.model_validate(data['state'])
    finally:
        if saver:
            try:
                await saver.save_usage()
            finally:
                run.usage_sink = original_sink
