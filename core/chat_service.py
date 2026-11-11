"""线上与评测共用的对话应用服务。"""
import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from agents.agent_orchestrator import AgentOrchestrator, Request
from core.intent_recognizer import IntentCategory, IntentRecognizer
from mcp.tool_manager import MCPToolManager
from core.contracts import TaskState
from core.task_state import merge_state, needs_admission
from core.runtime import CURRENT_RUN, RunContext
from rag.agentic import AgenticRAG, RetrievalOutcome

if TYPE_CHECKING:
    from memory.conversation_memory import MemoryManager

logger = logging.getLogger(__name__)
_SOURCE_MARKER = re.compile(r"\[来源(\d+)\]")
_KNOWLEDGE_CATEGORIES = {
    IntentCategory.SCHOOL_INFO: ["school_info"],
    IntentCategory.MAJOR_INFO: ["major_info"],
    IntentCategory.ADMISSION_POLICY: ["admission_policy"],
    IntentCategory.SCORE_RISK: ["score_risk"],
    IntentCategory.TUITION: ["tuition"],
    IntentCategory.CAMPUS_LIFE: ["campus_life"],
    IntentCategory.CAREER: ["major_info"],
    IntentCategory.COMPARISON: ["major_info", "school_info"],
    IntentCategory.ESCALATION: ["escalation"],
}
_OFFICIAL_FACT_INTENTS = {
    IntentCategory.SCHOOL_INFO,
    IntentCategory.ADMISSION_POLICY,
    IntentCategory.TUITION,
    IntentCategory.CAMPUS_LIFE,
    IntentCategory.ESCALATION,
}


@dataclass
class ChatResult:
    conv_id: str
    response: str
    intent: str
    agent_type: str
    escalated: bool
    latency_ms: float
    knowledge_used: bool = False
    admission_data_used: bool = False
    citations: List[Dict[str, Any]] = field(default_factory=list)
    entities: Dict[str, List[str]] = field(default_factory=dict)
    request_id: str = ''
    trace_id: str = ''
    status: str = 'completed'
    verified: Optional[bool] = None
    stop_reason: str = ''


class ChatService:
    """封装记忆、RAG、Agent 编排和记忆写回，供所有入口复用。"""

    def __init__(
        self,
        orchestrator: AgentOrchestrator,
        memory: Optional["MemoryManager"],
        tool_manager: Optional[MCPToolManager],
        recognizer: Optional[IntentRecognizer] = None,
        knowledge_tool: str = "knowledge_search",
        admission_tool: str = "admission_data_query",
        knowledge_top_k: int = 3,
        agentic_rag: Optional[AgenticRAG] = None,
        trace_dir: Optional[str] = None,
        timeout_s: float = 50.0,
    ):
        self._orchestrator = orchestrator
        self._memory = memory
        self._tool_manager = tool_manager
        self._recognizer = recognizer
        self._knowledge_tool = knowledge_tool
        self._admission_tool = admission_tool
        self._knowledge_top_k = knowledge_top_k
        self._rag = agentic_rag
        self._trace_dir = trace_dir
        self._timeout_s = timeout_s

    async def handle(
        self,
        message: str,
        user_id: str,
        conv_id: Optional[str] = None,
        *,
        persist_memory: bool = True,
        update_profile: bool = True,
        client_request_id: Optional[str] = None,
    ) -> ChatResult:
        request_id = client_request_id or uuid.uuid4().hex
        conv_id = conv_id or str(uuid.uuid5(uuid.NAMESPACE_URL, user_id + ':' + request_id))
        run = RunContext(timeout_s=self._timeout_s)
        context_token = CURRENT_RUN.set(run)
        status = 'failed'
        turns = getattr(self._memory, 'turns', None) if persist_memory else None
        try:
            async with asyncio.timeout(self._timeout_s):
                if turns is None:
                    result, _ = await self._handle_turn(message, user_id, conv_id, TaskState(), run, request_id)
                else:
                    receipt = await turns.receipt(user_id, conv_id, request_id, message)
                    if receipt:
                        status = 'replayed'
                        return ChatResult(**receipt)
                    async with turns.guard(user_id, conv_id, lease_s=self._timeout_s + 10) as lease:
                        receipt = await turns.receipt(user_id, conv_id, request_id, message)
                        if receipt:
                            status = 'replayed'
                            return ChatResult(**receipt)
                        previous = await turns.state(user_id, conv_id)
                        result, state = await self._handle_turn(message, user_id, conv_id, previous, run, request_id)
                        with run.span('commit'):
                            await turns.commit(user_id, conv_id, request_id, message, asdict(result), state, lease)
                    if update_profile:
                        queue = getattr(self._memory, 'maintenance', None)
                        if queue:
                            queue.submit(user_id, conv_id)
                status = result.status
                return result
        finally:
            CURRENT_RUN.reset(context_token)
            try:
                run.save(self._trace_dir, status)
            except OSError:
                logger.warning('Trace 写入失败: %s', run.run_id)

    async def _handle_turn(self, message, user_id, conv_id, previous, run, request_id):
        started_at = time.monotonic()
        with run.span('memory_read'):
            memory_context = await self._get_memory_context(user_id, conv_id, message)
        history = [
            {"role": item.role.value, "content": item.content}
            for item in memory_context.recent_messages[-5:]
        ] if memory_context else None

        with run.span('recognize'):
            recognized = await self._recognizer.recognize(message, history=history) if self._recognizer else None
        state = merge_state(previous, recognized.entities if recognized else {}, message, request_id)
        entities = dict(state.entities)

        intent = recognized.intent if recognized else None
        admission_required = needs_admission(message, intent.value if intent else '', {**previous.entities, **entities})
        if (admission_required and {'rank', 'score'} & set(state.invalidated)
                and not (entities.get('rank') or entities.get('score'))):
            return ChatResult(conv_id=conv_id, response='报考条件已变更，请重新提供该考生在对应省份、考试年度的位次或分数。',
                              intent=intent.value if intent else 'score_risk', agent_type='general', escalated=False,
                              latency_ms=round((time.monotonic()-started_at)*1000, 1), entities=entities,
                              request_id=request_id, trace_id=run.run_id, status='clarification',
                              stop_reason='candidate_scope_changed'), state
        # Required numeric facts run first, before optional retrieval spends the budget.
        admission_results = []
        with run.span('admission_query'):
            admission_text, admission_used, admission_citations, resolved = await self._build_admission_context(
                message, intent, entities, [], force=admission_required, data_out=admission_results)
        self._merge_resolved_entities(entities, resolved)
        for key in entities.keys() - state.entities.keys():
            state.sources[key] = request_id
        state.entities = entities
        if admission_required and not admission_used:
            data = admission_results[0] if admission_results else {}
            tool_status = data.get('status', 'unavailable')
            return ChatResult(
                conv_id=conv_id,
                response=str(data.get('message') or '结构化录取数据暂时不可用，暂不能核实录取数字或风险。请稍后重试。'),
                intent=intent.value if intent else 'score_risk', agent_type='risk', escalated=False,
                latency_ms=round((time.monotonic()-started_at)*1000, 1), entities=entities,
                citations=admission_citations, request_id=request_id, trace_id=run.run_id,
                status='clarification' if tool_status == 'needs_clarification' else 'insufficient_evidence',
                verified=False, stop_reason='admission_' + tool_status), state
        outcome = RetrievalOutcome()
        with run.span('retrieval'):
            if self._rag and self._should_use_knowledge(message):
                outcome = await self._rag.retrieve(message, _KNOWLEDGE_CATEGORIES.get(intent))
                knowledge_text, knowledge_citations = self._rag.context(outcome)
                knowledge_used = bool(outcome.evidence)
            else:
                knowledge_text, knowledge_used, knowledge_citations = await self._build_knowledge_context(message, intent)
        if knowledge_citations:
            admission_text = _SOURCE_MARKER.sub(lambda m: f'[来源{int(m.group(1)) + len(knowledge_citations)}]', admission_text)
        citations = knowledge_citations + admission_citations
        context_parts = [
            '[本轮已确认条件，优先于历史摘要]\n' + json.dumps(entities, ensure_ascii=False),
            memory_context.to_prompt_text() if memory_context else "",
            admission_text,
            knowledge_text,
        ]
        request = Request(
            message=message,
            user_id=user_id,
            conv_id=conv_id,
            context="\n\n".join(part for part in context_parts if part),
            history=history,
            intent=recognized.intent if recognized else None,
            urgency=recognized.urgency if recognized else None,
            entities=entities,
        )
        with run.span('answer'):
            result = await self._orchestrator.run(request)
        response = (
            self._knowledge_unavailable_response()
            if intent in _OFFICIAL_FACT_INTENTS and not knowledge_used
            else self._sanitize_citations(result.response, citations)
        )

        verified, final_status = None, 'completed'
        if self._rag and self._should_use_knowledge(message):
            admission_data = admission_results[0] if admission_results else None
            with run.span('answer_verification'):
                verdict = await self._rag.verify_answer(message, response, outcome.evidence, admission_data)
                # A numerically answered risk question can succeed without unrelated policy evidence.
                evidence_ready = outcome.sufficient or (admission_used and intent == IntentCategory.SCORE_RISK)
                verified = verdict.passed and evidence_ready
                if not verified and evidence_ready and run.model_calls < run.max_models:
                    request.context += '\n[修复要求]删除无证据结论，不得编造数字或概率。原回答：' + response
                    repaired = await self._orchestrator.run(request)
                    response = self._sanitize_citations(repaired.response, citations)
                    verdict = await self._rag.verify_answer(message, response, outcome.evidence, admission_data)
                    verified = verdict.passed
                if not verified:
                    final_status = 'insufficient_evidence'
                    response = self._evidence_fallback(outcome, admission_data, len(knowledge_citations))

        return ChatResult(
            conv_id=conv_id,
            response=response,
            intent=result.intent.value if result.intent else "other",
            agent_type=result.agent_type.value,
            escalated=result.escalated,
            latency_ms=round((time.monotonic() - started_at) * 1000, 1),
            knowledge_used=knowledge_used or admission_used,
            admission_data_used=admission_used,
            citations=citations,
            entities=entities,
            request_id=request_id, trace_id=run.run_id, status=final_status,
            verified=verified, stop_reason=outcome.stop_reason if self._rag else 'legacy_pipeline',
        ), state

    @staticmethod
    def _evidence_fallback(outcome, admission_data, offset):
        parts = ['当前证据不足以核实完整回答，以下仅列出已查询到的原始资料。']
        if admission_data and admission_data.get('status') == 'ok':
            for analysis in admission_data.get('analyses', []):
                parts.append(str(analysis.get('major', '')))
                for row in analysis.get('records', []):
                    parts.append(f"{row['year']}年：最低分 {row.get('min_score')}，最低位次 {row.get('min_rank')}。")
                assessment = analysis.get('assessment') or {}
                if assessment.get('risk_level'):
                    parts.append('历史比较：' + assessment['risk_level'] + '。')
            for candidate in admission_data.get('candidates', []):
                parts.append(
                    f"{candidate['major']}：历史中位分 {candidate.get('historical_median_score')}，"
                    f"历史中位位次 {candidate.get('historical_median_rank')}；"
                    f"历史比较：{candidate.get('risk_level')}。"
                )
            if admission_data.get('sources'):
                parts.append(f'[来源{offset + 1}] 历史数据不代表未来录取概率。')
            parts.extend(str(item) for item in admission_data.get('caveats', []))
        elif admission_data and admission_data.get('message'):
            parts.append(str(admission_data['message']))
        for number, evidence in enumerate(outcome.evidence[:4], 1):
            parts.append(f'[来源{number}] {evidence.title}（{evidence.year or "年份未标注"}）：\n> ' + evidence.content[:350].replace('\n', '\n> '))
        if outcome.verdict and outcome.verdict.missing_facts:
            parts.append('尚缺：' + '；'.join(outcome.verdict.missing_facts))
        return '\n\n'.join(parts)

    async def clear_conversation(self, user_id: str, conv_id: str) -> None:
        if self._memory is not None:
            await self._memory.clear_conversation(user_id, conv_id)

    async def _get_memory_context(self, user_id: str, conv_id: str, message: str):
        if self._memory is None:
            return None
        return await self._memory.get_context(user_id, conv_id, query=message)

    async def _build_knowledge_context(
        self,
        message: str,
        intent: Optional[IntentCategory] = None,
    ) -> tuple[str, bool, List[Dict[str, Any]]]:
        if self._tool_manager is None or not self._should_use_knowledge(message):
            return "", False, []
        try:
            result = await self._tool_manager.search_with_rewrite(
                self._knowledge_tool,
                message,
                top_k=self._knowledge_top_k,
                categories=_KNOWLEDGE_CATEGORIES.get(intent),
            )
            if not result.success or not isinstance(result.data, list):
                return "", False, []

            parts = ["[河北工业大学官方知识检索结果]"]
            citations: List[Dict[str, Any]] = []
            source_indexes: Dict[tuple[str, ...], int] = {}
            source_excerpts: List[List[str]] = []
            for item in result.data[:self._knowledge_top_k]:
                if not isinstance(item, dict) or item.get("fallback"):
                    continue
                content = str(item.get("content", "")).strip()
                if not content:
                    continue
                citation = {
                    "title": str(item.get("title", "未命名来源")),
                    "url": str(item.get("source_url", "")),
                    "published_at": item.get("published_at", ""),
                    "effective_year": item.get("effective_year", ""),
                    "score": item.get("rerank_score", item.get("score", "")),
                }
                source_key = self._citation_key(citation)
                source_index = source_indexes.get(source_key)
                if source_index is None:
                    source_index = len(citations)
                    source_indexes[source_key] = source_index
                    citations.append(citation)
                    source_excerpts.append([])
                if content not in source_excerpts[source_index]:
                    source_excerpts[source_index].append(content)

            for source_no, (citation, excerpts) in enumerate(
                zip(citations, source_excerpts),
                start=1,
            ):
                excerpt_text = "\n".join(
                    f"内容片段{index}: {content[:900]}"
                    for index, content in enumerate(excerpts, start=1)
                )
                parts.append(
                    f"[来源{source_no}] {citation['title']}\n"
                    f"URL: {citation['url'] or '未提供'}\n"
                    f"有效年份: {citation['effective_year'] or '未标注'}\n"
                    f"{excerpt_text}"
                )

            if not citations:
                return "", False, []
            parts.append(
                "回答动态事实时只能使用以上有依据的内容并标注[来源N]；"
                "同一来源的多个内容片段共享一个编号；"
                "资料不足时说明缺少什么，不得用常识补造招生数据。"
            )
            return "\n\n".join(parts), True, citations
        except Exception as ex:
            logger.warning("构建招生知识上下文失败: %s", ex)
            return "", False, []

    @staticmethod
    def _sanitize_citations(response: str, citations: List[Dict[str, Any]]) -> str:
        return _SOURCE_MARKER.sub(
            lambda match: match.group(0) if 1 <= int(match.group(1)) <= len(citations) else "",
            str(response or ""),
        )

    @staticmethod
    def _knowledge_unavailable_response() -> str:
        return (
            "当前官方知识库没有检索到足以回答该问题的资料，因此我不能提供未经核实的具体数字或学校现状。"
            "请以河北工业大学本科招生网、新生入学须知或招生办公室最新发布为准。"
        )

    async def _build_admission_context(
        self,
        message: str,
        intent: Optional[IntentCategory],
        entities: Dict[str, List[str]],
        history: Optional[List[Dict[str, str]]],
        *,
        source_offset: int = 0,
        force: bool = False,
        data_out: Optional[list] = None,
    ) -> tuple[str, bool, List[Dict[str, Any]], Dict[str, Any]]:
        if (
            (intent != IntentCategory.SCORE_RISK and not force)
            or self._tool_manager is None
            or not self._admission_tool
        ):
            return "", False, [], {}
        try:
            result = await self._tool_manager.call(
                self._admission_tool,
                {
                    "query": message,
                    "entities": entities,
                    "history": history or [],
                },
                use_cache=True,
            )
            if not result.success or not isinstance(result.data, dict):
                return "", False, [], {}

            data = result.data
            if data_out is not None:
                data_out.append(data)
            citations = []
            source_lines = []
            for index, source in enumerate(data.get("sources") or [], start=source_offset + 1):
                if not isinstance(source, dict):
                    continue
                citation = {
                    "title": str(source.get("title", "河北工业大学结构化录取数据")),
                    "url": str(source.get("source_url", "")),
                    "source_file": str(source.get("source_file", "")),
                    "effective_year": source.get("effective_year", ""),
                    "data_version": str(source.get("data_version", "")),
                    "type": "structured_admission_data",
                }
                citations.append(citation)
                source_lines.append(
                    f"[来源{index}] {citation['title']}；文件: {citation['source_file'] or '未标注'}；"
                    f"URL: {citation['url'] or '未提供'}"
                )

            status = str(data.get("status", ""))
            used = status == "ok" and bool(data.get("analyses") or data.get("candidates"))
            if data.get("analyses"):
                usage_rule = (
                    "必须准确列出查询到的年份、最低分和最低位次，优先采用工具给出的"
                    "assessment.risk_level 与依据，不得自行改写数字或生成录取概率。"
                )
            elif data.get("candidates"):
                usage_rule = (
                    "候选专业只能使用工具返回的历史中位值和风险等级，不得补造逐年数据；"
                    "需要逐年数据时应让用户指定专业。"
                )
            else:
                usage_rule = "不得自行补造未查询到的数据。"
            parts = [
                "[结构化录取数据查询结果]",
                *source_lines,
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                (
                    f"以上数字来自确定性 CSV 查询。{usage_rule}"
                    "status 为 needs_clarification、unsupported 或 not_found 时，按 message 说明边界并补问。"
                ),
            ]
            return "\n".join(part for part in parts if part), used, citations, dict(data.get("resolved") or {})
        except Exception as ex:
            logger.warning("构建结构化录取数据上下文失败: %s", ex)
            return "", False, [], {}

    @staticmethod
    def _citation_key(citation: Dict[str, Any]) -> tuple[str, ...]:
        """生成稳定来源标识：URL 优先，无 URL 时按标题和有效年份去重。"""
        url = str(citation.get("url", "")).strip()
        if url:
            parsed = urlsplit(url)
            normalized = urlunsplit((
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/"),
                parsed.query,
                "",
            ))
            return ("url", normalized)
        title = " ".join(str(citation.get("title", "")).split()).casefold()
        year = str(citation.get("effective_year", "")).strip()
        return ("title_year", title, year)

    @staticmethod
    def _merge_resolved_entities(entities: Dict[str, List[str]], resolved: Dict[str, Any]) -> None:
        mapping = {
            "province": resolved.get("province"),
            "subject_combination": resolved.get("subject_type"),
            "score": resolved.get("candidate_score"),
            "rank": resolved.get("candidate_rank"),
            "admission_year": resolved.get("target_year"),
        }
        for key, value in mapping.items():
            if value not in (None, "") and not entities.get(key):
                entities[key] = [str(value)]
        majors = resolved.get("majors")
        if isinstance(majors, list) and majors and not entities.get("major"):
            entities["major"] = [str(value) for value in majors]

    @staticmethod
    def _should_use_knowledge(message: str) -> bool:
        text = (message or "").strip().lower()
        if not text:
            return False
        small_talk = {"你好", "您好", "嗨", "hi", "hello", "hey", "谢谢", "好的", "再见"}
        return text not in small_talk
