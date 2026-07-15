"""线上与评测共用的对话应用服务。"""
import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from agents.agent_orchestrator import AgentOrchestrator, Request
from core.intent_recognizer import IntentCategory, IntentRecognizer
from mcp.tool_manager import MCPToolManager

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
    ):
        self._orchestrator = orchestrator
        self._memory = memory
        self._tool_manager = tool_manager
        self._recognizer = recognizer
        self._knowledge_tool = knowledge_tool
        self._admission_tool = admission_tool
        self._knowledge_top_k = knowledge_top_k

    async def handle(
        self,
        message: str,
        user_id: str,
        conv_id: Optional[str] = None,
        *,
        persist_memory: bool = True,
        update_profile: bool = True,
    ) -> ChatResult:
        started_at = time.monotonic()
        conv_id = conv_id or str(uuid.uuid4())
        memory_context = await self._get_memory_context(user_id, conv_id, message)
        history = [
            {"role": item.role.value, "content": item.content}
            for item in memory_context.recent_messages[-5:]
        ] if memory_context else None

        recognized = await self._recognizer.recognize(message, history=history) if self._recognizer else None
        entities = dict(recognized.entities) if recognized else {}

        intent = recognized.intent if recognized else None
        knowledge_text, knowledge_used, knowledge_citations = await self._build_knowledge_context(message, intent)
        admission_text, admission_used, admission_citations, resolved = await self._build_admission_context(
            message,
            intent,
            entities,
            history,
            source_offset=len(knowledge_citations),
        )
        self._merge_resolved_entities(entities, resolved)
        citations = knowledge_citations + admission_citations
        context_parts = [
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
        result = await self._orchestrator.run(request)
        response = (
            self._knowledge_unavailable_response()
            if intent in _OFFICIAL_FACT_INTENTS and not knowledge_used
            else self._sanitize_citations(result.response, citations)
        )

        if persist_memory and self._memory is not None:
            from memory.conversation_memory import MsgRole

            await self._memory.add_message(user_id, conv_id, MsgRole.USER, message)
            await self._memory.add_message(user_id, conv_id, MsgRole.ASSISTANT, response)
            if update_profile:
                asyncio.create_task(self._memory.update_profile(user_id, conv_id))

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
            entities=getattr(result, "entities", {}),
        )

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
    ) -> tuple[str, bool, List[Dict[str, Any]], Dict[str, Any]]:
        if (
            intent != IntentCategory.SCORE_RISK
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
