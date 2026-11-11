"""
河北工业大学普通本科报考咨询 — FastAPI 入口

启动时打印小熊饼干图案。
所有核心组件在 lifespan 中初始化，通过环境变量配置。
"""
import asyncio
import logging
import os
import pathlib
import sys
import uuid
from dataclasses import asdict
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# 将项目根目录加入 sys.path，确保无论从哪里执行都能找到 agents/core/memory 等模块
# 这一行必须在所有项目内部 import 之前执行
_ROOT = str(pathlib.Path(__file__).parent.parent.resolve())
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BANNER = r"""
    ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ
   ╔════════════════════════════╗
   ║  Hebut Admissions  v2.0   ║
   ║  河北工业大学本科报考咨询  ║
   ╚════════════════════════════╝
    ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ
"""

# ── 全局组件（lifespan 中初始化）─────────────────────────────────────────────
_orchestrator = None
_memory       = None
_tool_manager = None
_monitor      = None
_evaluator    = None
_skill_manager = None
_chat_service = None
_admission_store = None
_tool_managers = []
_job_store = None


def _anthropic_cfg() -> Dict[str, Any]:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("未设置 ANTHROPIC_API_KEY")
    cfg: Dict[str, Any] = {
        "api_key":  key,
        "model":    os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"),
    }
    base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
    if base_url:
        cfg["base_url"] = base_url
    return cfg


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _orchestrator, _memory, _tool_manager, _monitor, _evaluator, _skill_manager, _chat_service, _admission_store
    global _job_store

    print(BANNER, flush=True)

    from agents.agent_orchestrator import AgentOrchestrator, Request
    from core.intent_recognizer import IntentRecognizer
    from core.chat_service import ChatService
    from evaluation.evaluator import EndToEndEvaluator
    from tooling.admission_data import AdmissionDataStore
    from tooling.knowledge_base import KnowledgeBase
    from tooling.tool_manager import MCPToolManager, Tool
    from memory.conversation_memory import MemoryManager
    from monitor.performance_monitor import PerformanceMonitor
    from core.skill_loader import SkillManager
    from core.contracts import AdmissionQuery
    from rag.agentic import AgenticRAG

    cfg = _anthropic_cfg()
    logger.info(f"模型: {cfg['model']}  base_url: {cfg.get('base_url', '(官方)')}")

    # 意图识别器（Orchestrator 内部也会创建，这里单独暴露给 Evaluator）
    recognizer = IntentRecognizer(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )

    # Skills：启动时从目录加载业务能力说明，并在 Agent 调用 LLM 时动态注入。
    skills_dir = os.getenv("HELLOHEBUT_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills"))
    _skill_manager = SkillManager(
        root_dir=skills_dir,
        max_prompt_chars=int(os.getenv("HELLOHEBUT_SKILLS_MAX_PROMPT_CHARS", "5000")),
    )
    _skill_manager.load()

    # Agent 编排器
    _orchestrator = AgentOrchestrator(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        skill_manager=_skill_manager,
        role_mode=os.getenv('HELLOHEBUT_ROLE_MODE', 'single'),
        tool_loop=os.getenv('HELLOHEBUT_TOOL_LOOP', 'false').lower() == 'true',
    )

    # MySQL owns both preferences and atomic chat turns; Redis still provides sessions.
    from memory.fact_store import FactStore
    from memory.mysql_database import MySQLDatabase
    from memory.mysql_turn_store import MySQLTurnStore
    verified_memory = os.getenv('HELLOHEBUT_VERIFIED_MEMORY', 'false').lower() == 'true'
    facts = FactStore(os.getenv('V2_FACT_DATABASE_URL', '')) if verified_memory else None
    if facts:
        await facts.initialize()
    turns = MySQLTurnStore(os.getenv('REDIS_URL', 'redis://redis:6379/0'),
                          database=facts or MySQLDatabase(os.getenv('V2_FACT_DATABASE_URL', '')))
    await turns.initialize()
    # 记忆管理器（Redis 工作记忆 + 可切换的跨会话记忆适配器）
    _memory = MemoryManager(
        # Disabling preferences does not change the chat ledger's authority.
        fact_store=facts, verified_memory_mode=True, turn_store=turns,
        redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
        chroma_host=os.getenv("CHROMA_HOST", "chromadb"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/app/data/chroma"),
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )

    # MCP 工具管理器 + RAG 知识库（基于 ChromaDB 的真实检索）
    kb = KnowledgeBase(
        lexical_mode=os.getenv('RAG_LEXICAL_MODE', 'filtered_legacy'),
        fusion_mode=os.getenv('RAG_FUSION_MODE', 'balanced'),
        redis_url=os.getenv('REDIS_URL', 'redis://redis:6379/0'),
        chroma_host=os.getenv("CHROMA_HOST", "chromadb"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/app/data/chroma"),
        collection_name=os.getenv("V2_KNOWLEDGE_COLLECTION", "hello_hebut_v2"),
        seed_dir=os.getenv(
            "KNOWLEDGE_SEED_DIR",
            str(pathlib.Path(_ROOT) / "data" / "demo_docs"),
        ).strip() or None,
    )
    logger.info("知识库已加载: %s 个文档片段；自动导入: %s", kb.doc_count, kb.last_seed_report)

    # 分数、位次属于结构化数据，使用确定性 CSV 查询，不进入向量知识库。
    _admission_store = AdmissionDataStore(
        os.getenv(
            "ADMISSION_DATA_PATH",
            str(pathlib.Path(_ROOT) / "data" / "hebut_admission.csv"),
        )
    )
    logger.info("结构化录取数据已加载: %s", _admission_store.stats)

    def knowledge_fallback(params: Dict[str, Any], context: Optional[Dict[str, Any]], error: str):
        query = params.get("query", "")
        return [{
            "title": "招生知识库降级结果",
            "content": f"知识库暂时不可用，未能完成对“{query}”的检索。请稍后重试或通过本科招生网确认。",
            "score": 0.0,
            "fallback": True,
            "error": error,
        }]

    def admission_fallback(params: Dict[str, Any], context: Optional[Dict[str, Any]], error: str):
        return {
            "status": "unavailable",
            "message": "结构化录取数据暂时不可用，请稍后重试或通过本科招生网查询。",
            "error": error,
            "resolved": {},
        }

    def build_tool_manager() -> MCPToolManager:
        manager = MCPToolManager(
            api_key=cfg["api_key"],
            base_url=cfg.get("base_url"),
            model=cfg["model"],
            rerank_min_score=float(os.getenv("RAG_RERANK_MIN_SCORE", "5.0")),
        )
        manager.register(Tool(
            name="knowledge_search",
            description="搜索河北工业大学官方本科招生知识库",
            handler=kb.search_handler,
            schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 8000},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 100},
                    "categories": {"type": "array", "items": {"type": "string"}},
                    "year": {"type": "integer", "minimum": 2000, "maximum": 2100},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            cache_ttl=300.0,
            cache_scope=kb.retrieval_cache_scope,
            supports_rerank=True,
            fallback=knowledge_fallback,
        ))
        manager.register(Tool(
            name="admission_data_query",
            description="确定性查询河北工业大学河北、天津2023—2025年分专业最低分和最低位次，并生成可解释风险等级",
            handler=_admission_store.query_handler,
            schema=AdmissionQuery.model_json_schema(),
            cache_ttl=600.0,
            fallback=admission_fallback,
        ))
        return manager

    _tool_manager = build_tool_manager()

    agentic_enabled = os.getenv('HELLOHEBUT_AGENTIC_RAG', 'true').lower() == 'true'
    _chat_service = ChatService(
        _orchestrator, _memory, _tool_manager, recognizer=recognizer,
        agentic_rag=AgenticRAG(_tool_manager) if agentic_enabled else None,
        trace_dir=os.getenv('V2_TRACE_DIR', str(pathlib.Path(_ROOT) / 'experiments/v2/traces/chat')),
        timeout_s=float(os.getenv('V2_CHAT_TIMEOUT_S', '50')),
        graph_enabled=os.getenv('HELLOHEBUT_CHAT_GRAPH', 'true').lower() == 'true',
    )
    _orchestrator.bind_tools(_tool_manager)

    # 性能监控（可选启动 Prometheus）
    prom_port = int(os.getenv("PROMETHEUS_PORT", "0")) or None
    _monitor = PerformanceMonitor(
        orchestrator=_orchestrator,
        tool_manager=_tool_manager,
        interval_s=float(os.getenv("MONITOR_INTERVAL", "10")),
        webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
        prometheus_port=prom_port,
    )
    await _monitor.start()

    # 评测器
    # 评测使用独立编排器，避免评测流量污染线上 Agent 统计；其余链路复用 ChatService。
    eval_orchestrator = AgentOrchestrator(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        skill_manager=_skill_manager,
        role_mode=os.getenv('HELLOHEBUT_ROLE_MODE', 'single'),
        tool_loop=os.getenv('HELLOHEBUT_TOOL_LOOP', 'false').lower() == 'true',
    )
    eval_tool_manager = build_tool_manager()
    eval_orchestrator.bind_tools(eval_tool_manager)
    import copy
    eval_memory = copy.copy(_memory)
    eval_memory.facts = None
    eval_memory.turns = MySQLTurnStore(os.getenv('REDIS_URL', 'redis://redis:6379/0'), database=MySQLDatabase(
        os.getenv('V2_FACT_DATABASE_URL', ''), namespace='hellohebut_evaluation_v2'))
    await eval_memory.turns.initialize()
    eval_memory.maintenance = None
    _tool_managers[:] = [_tool_manager, eval_tool_manager]
    eval_chat_service = ChatService(
        eval_orchestrator, eval_memory, eval_tool_manager, recognizer=recognizer,
        agentic_rag=AgenticRAG(eval_tool_manager) if agentic_enabled else None,
        trace_dir=str(pathlib.Path(_ROOT) / 'experiments/v2/traces/evaluation'),
        graph_enabled=os.getenv('HELLOHEBUT_CHAT_GRAPH', 'true').lower() == 'true',
    )
    _evaluator = EndToEndEvaluator(
        chat_service=eval_chat_service,
        recognizer=recognizer,
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=os.getenv("EVAL_JUDGE_MODEL", cfg["model"]),
        baseline_path=os.getenv("V2_EVAL_BASELINE_PATH", str(pathlib.Path(_ROOT) / 'experiments/v2/reports/live/baseline.json')),
    )

    from memory.job_store import JobStore, DurableWorkers
    from monitor.tracing import TraceExporter
    from core.runtime import PROGRESS_SINK
    _job_store = JobStore(turns.db)
    _job_store.turns = turns
    await _job_store.initialize()
    turns.maintenance_outbox = _job_store

    class OutboxMaintenance:
        durable = True
        def submit(self, *args, **kwargs):
            return True  # Already written atomically by MySQLTurnStore.commit.
        async def close(self):
            pass
    _memory.maintenance = OutboxMaintenance()

    async def execute_chat(job):
        async def progress(stage):
            await _job_store.event(job, stage)
        token = PROGRESS_SINK.set(progress)
        try:
            payload = job['payload']
            result = await _chat_service.handle(payload['message'], payload['user_id'], payload['conv_id'],
                                                client_request_id=payload['client_request_id'])
            return asdict(result)
        finally:
            PROGRESS_SINK.reset(token)

    async def execute_maintenance(job):
        payload = job['payload']
        # Serialize provenance check + candidate creation with clear. Summary uses
        # its existing epoch/prefix CAS and can run after releasing this short lease.
        async with turns.guard(payload['user_id'], payload['conv_id'], lease_s=20):
            async with asyncio.timeout(10):
                await _job_store.renew(job)
                receipt = await turns.receipt(payload['user_id'], payload['conv_id'], payload['request_id'], payload['message'])
                if not receipt:
                    return {'maintained': False, 'reason': 'source_cleared'}
                await _memory.capture_preferences(payload['user_id'], payload['conv_id'], payload['request_id'], payload['message'])
        await _memory.maintain(payload['user_id'], payload['conv_id'])
        return {'maintained': True}

    async def execute_evaluation(job):
        return await run_eval(EvalRunInput.model_validate(job['payload']))

    workers = DurableWorkers(_job_store, {'chat': execute_chat, 'maintenance': execute_maintenance,
                                        'evaluation': execute_evaluation}, workers=int(os.getenv('V2_WORKERS', '2')))
    workers.start()
    exporter = TraceExporter()
    exporter.start()
    logger.info("河北工业大学本科报考咨询服务已就绪")
    yield

    await workers.close()
    await exporter.close()
    await eval_memory.turns.close()
    await _monitor.stop()
    await _memory.close()
    if kb._catalog:
        kb._catalog.close()
    logger.info("河北工业大学本科报考咨询服务已关闭")


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="河北工业大学本科报考咨询",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
)

from api.memory_routes import build_memory_router
app.include_router(build_memory_router(lambda: _memory))

_cors_origins = [
    item.strip()
    for item in os.getenv(
        "CORS_ALLOW_ORIGINS",
        "http://localhost,http://127.0.0.1,http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if item.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 请求/响应模型 ─────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message:     str = Field(min_length=1, max_length=8000)
    user_id:     Optional[str] = None
    conv_id:     Optional[str] = None
    client_request_id: Optional[str] = Field(default=None, min_length=1, max_length=128)


class ChatResponse(BaseModel):
    conv_id:     str
    response:    str
    intent:      str
    agent_type:  str
    escalated:   bool
    latency_ms:  float
    knowledge_used: bool = False
    admission_data_used: bool = False
    citations:   List[Dict[str, Any]] = Field(default_factory=list)
    entities:    Dict[str, List[str]] = Field(default_factory=dict)
    request_id: str = ''
    trace_id: str = ''
    status: str = 'completed'
    verified: Optional[bool] = None
    stop_reason: str = ''
    verification_method: str = ''


def require_admin(x_admin_token: Optional[str] = Header(default=None)) -> None:
    """配置 ADMIN_API_TOKEN 后保护知识写入、Skill 重载和评测入口。"""
    expected = os.getenv("ADMIN_API_TOKEN", "").strip()
    if not expected:
        raise HTTPException(503, '管理接口未启用，请配置 ADMIN_API_TOKEN')
    if x_admin_token != expected:
        raise HTTPException(401, "缺少或使用了无效的管理令牌")


# ── 路由 ──────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    return {"status": "ok", "agents": _orchestrator.get_stats()}


@app.get('/live')
async def live():
    return {'status': 'alive'}


@app.get('/ready')
async def ready():
    if not _memory or not _tool_manager or not _job_store:
        raise HTTPException(503, '服务正在初始化')
    try:
        async with asyncio.timeout(4):
            await _memory.turns.client.ping()
            async with _memory.turns.db.connection() as conn:
                await conn.execute('SELECT 1')
            kb = _tool_manager._tools['knowledge_search'].handler.__self__
            await asyncio.to_thread(kb._collection.count)
    except Exception as exc:
        raise HTTPException(503, '依赖服务尚未就绪') from exc
    return {'status': 'ready', 'dependencies': ['mysql','redis','chroma']}


@app.get("/skills", tags=["Skills"])
async def skills_summary():
    """查看当前已加载的 Skills，便于确认热加载结果和排查解析错误。"""
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    return _skill_manager.summary()


@app.post("/skills/reload", tags=["Skills"])
async def reload_skills(_: None = Depends(require_admin)):
    """运行时重新扫描 Skill 目录，不需要重启服务。"""
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    _skill_manager.reload()
    if _orchestrator is not None:
        _orchestrator.set_skill_manager(_skill_manager)
    return _skill_manager.summary()


@app.post('/session', tags=['会话'])
async def create_session():
    """Create an anonymous bearer identity. User-supplied IDs are never authorization."""
    if _memory is None:
        raise HTTPException(503, '服务未就绪')
    return await _memory.turns.create_session()


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, authorization: Optional[str] = Header(default=None)):
    """
    主对话接口。完整流程：
      记忆读取 → 意图识别 → 结构化录取数据/官方知识检索 → Agent 路由 → 执行 → 记忆写入
    """
    if _chat_service is None:
        raise HTTPException(503, "服务未就绪")
    from core.runtime import RequestConflict, ConversationBusy, LeaseLost, BudgetExceeded
    token = authorization[7:] if authorization and authorization.startswith('Bearer ') else ''
    user_id = await _memory.turns.session_user(token) if _memory else None
    if not user_id:
        raise HTTPException(401, '请先调用 POST /session 获取会话令牌')
    if req.user_id and req.user_id != user_id:
        raise HTTPException(403, 'user_id 与会话令牌不匹配')
    try:
        result = await _chat_service.handle(req.message, user_id, req.conv_id,
                                            client_request_id=req.client_request_id)
    except RequestConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ConversationBusy as exc:
        raise HTTPException(409, str(exc), headers={'Retry-After': '3'}) from exc
    except BudgetExceeded as exc:
        raise HTTPException(422, '本轮调用预算已耗尽，请缩小问题范围并新建请求') from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(504, '本轮超时，可用相同 client_request_id 恢复；已消耗调用额度不会重置') from exc
    except LeaseLost as exc:
        raise HTTPException(409, str(exc)) from exc
    return ChatResponse(**asdict(result))


@app.delete('/conversations/{conv_id}', tags=['会话'])
async def clear_conversation(conv_id: str, authorization: Optional[str] = Header(default=None)):
    from core.runtime import ConversationBusy, LeaseLost
    if _memory is None:
        raise HTTPException(503, '服务未就绪')
    token = authorization[7:] if authorization and authorization.startswith('Bearer ') else ''
    user = await _memory.turns.session_user(token)
    if not user:
        raise HTTPException(401, '请先获取有效会话令牌')
    try:
        await _memory.clear_conversation(user, conv_id)
    except (ConversationBusy, LeaseLost) as exc:
        raise HTTPException(409, '会话正在处理，请稍后重试清空', headers={'Retry-After': '3'}) from exc
    return {'cleared': True, 'conv_id': conv_id}


@app.get("/monitor")
async def monitor_summary():
    """实时监控摘要：Agent 成功率、工具统计、告警、优化建议。"""
    if _monitor is None:
        raise HTTPException(503, "服务未就绪")
    return _monitor.summary()


@app.get("/admission/stats", tags=["录取数据"])
async def admission_stats():
    """查看结构化录取数据的覆盖范围、有效行数和版本。"""
    if _admission_store is None:
        raise HTTPException(503, "结构化录取数据未初始化")
    return _admission_store.stats


@app.get("/admission/query", tags=["录取数据"])
async def admission_query(query: str):
    """直接验证录取数据的确定性查询结果，不经过 LLM。"""
    if _tool_manager is None:
        raise HTTPException(503, "服务未就绪")
    result = await _tool_manager.call("admission_data_query", {"query": query})
    if not result.success:
        raise HTTPException(503, result.error or "录取数据查询失败")
    return result.data


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus 指标入口。"""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/search")
async def search(query: str, top_k: int = 5):
    """
    演示检索优化链路：查询改写 → 关键词/向量并行召回 → 重排 → Top-K。
    展示 MCP 工具调用的核心亮点。
    """
    if _tool_manager is None:
        raise HTTPException(503, "服务未就绪")
    result = await _tool_manager.search_with_rewrite("knowledge_search", query, top_k=top_k)
    return {"query": query, "results": result.data, "reranked": result.reranked}


class DocInput(BaseModel):
    """单篇文档输入。"""
    title:   str
    content: str
    source_url: str
    category: Optional[str] = None
    published_at: Optional[str] = None
    fetched_at: Optional[str] = None
    effective_year: Optional[int] = None
    document_version: Optional[str] = None
    document_id: Optional[str] = Field(default=None, min_length=1, max_length=256)
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    college: Optional[str] = None
    major: Optional[str] = None
    section: Optional[str] = None


class BatchDocInput(BaseModel):
    """批量文档导入请求体。"""
    documents: List[DocInput]


def validate_official_source(source_url: str) -> None:
    """知识写入接口仅接受河北工业大学官方域名。"""
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or (host != "hebut.edu.cn" and not host.endswith(".hebut.edu.cn")):
        raise HTTPException(400, f"非河北工业大学官方来源: {source_url}")


class EvalIntentInput(BaseModel):
    """意图识别评测用例。"""
    message: str
    expected_intent: str
    context: Optional[Dict[str, Any]] = None


class EvalDialogInput(BaseModel):
    """对话质量用例；required/forbidden 为硬断言，soft_* 只记录不否决。"""
    question: Optional[str] = None
    turns: Optional[List[str]] = None
    user_id: Optional[str] = None
    conv_id: Optional[str] = None
    expected_intent: Optional[str] = None
    expected_agent: Optional[str] = None
    routing_assertions_hard: Optional[bool] = None
    required_terms: Optional[List[str]] = None
    soft_required_terms: Optional[List[str]] = None
    forbidden_terms: Optional[List[str]] = None
    soft_forbidden_terms: Optional[List[str]] = None
    should_escalate: Optional[bool] = None
    reference: Optional[str] = None
    require_citations: Optional[bool] = None
    require_admission_data: Optional[bool] = None
    expectations: Optional[List[Dict[str, Any]]] = None


class EvalRunInput(BaseModel):
    """评测请求。为空时使用内置默认用例。"""
    intent_cases: Optional[List[EvalIntentInput]] = None
    dialog_cases: Optional[List[EvalDialogInput]] = None


@app.post("/knowledge/add", tags=["知识库"])
async def add_knowledge(body: BatchDocInput, _: None = Depends(require_admin)):
    """
    批量导入文档到知识库。

    文档会自动切片（每片 500 字）并存入 ChromaDB，ChromaDB 内置 Embedding 模型自动向量化。

    示例请求体：
    ```json
    {
      "documents": [
        {"title": "2026年招生政策补充", "content": "政策正文...", "source_url": "https://zs.hebut.edu.cn/..."}
      ]
    }
    ```
    """
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__
    for document in body.documents:
        validate_official_source(document.source_url)
    count = await publish_knowledge(kb, [d.model_dump(exclude_none=True) for d in body.documents])
    for manager in _tool_managers:
        manager.invalidate_cache()
    return {"message": f"成功导入 {count} 个文档片段", "added_chunks": count, "total_chunks": kb.doc_count}


@app.post("/knowledge/upload", tags=["知识库"])
async def upload_knowledge(
    file: UploadFile = File(...),
    source_url: str = Form(...),
    category: str = Form("official"),
    _: None = Depends(require_admin),
):
    """
    上传文件导入知识库。

    支持格式：
    - `.txt` / `.md`：整个文件作为一篇文档，文件名作为标题
    - `.json`：JSON 数组格式 `[{"title": "...", "content": "..."}, ...]`

    文件大小限制：10MB
    """
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__
    validate_official_source(source_url)

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "文件大小超过 10MB 限制")

    text = content.decode("utf-8", errors="ignore")
    filename = file.filename or "unknown"

    if filename.endswith(".json"):
        import json as _json
        try:
            docs = _json.loads(text)
            if not isinstance(docs, list):
                raise HTTPException(400, "JSON 文件应为数组格式: [{title, content}, ...]")
            for doc in docs:
                if not isinstance(doc, dict):
                    raise HTTPException(400, "JSON 数组中的每一项都必须是对象")
                doc.setdefault("source_url", source_url)
                doc.setdefault("category", category)
                validate_official_source(str(doc["source_url"]))
        except _json.JSONDecodeError as e:
            raise HTTPException(400, f"JSON 解析失败: {e}")
    else:
        # txt / md：整个文件作为一篇文档
        title = filename.rsplit(".", 1)[0] if "." in filename else filename
        docs = [{"title": title, "content": text, "source_url": source_url, "category": category}]

    count = await publish_knowledge(kb, docs)
    for manager in _tool_managers:
        manager.invalidate_cache()
    return {
        "message": f"文件 {filename} 导入成功",
        "added_chunks": count,
        "total_chunks": kb.doc_count,
    }


@app.get("/knowledge/stats", tags=["知识库"])
async def knowledge_stats():
    """查看知识库统计信息（文档片段总数）。"""
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__
    return {
        "total_chunks": kb.doc_count,
        "seed_import": kb.last_seed_report,
        "publication": await asyncio.to_thread(kb.publication_status),
    }


async def publish_knowledge(kb, documents):
    from tooling.knowledge_catalog import PublicationConflict
    try:
        normalized = [DocInput.model_validate(item).model_dump(exclude_none=True) for item in documents]
        if not normalized:
            raise ValueError('发布文档不能为空')
        return await asyncio.to_thread(kb.add_documents, normalized)
    except PublicationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class RollbackInput(BaseModel):
    generation: int = Field(ge=0)


@app.post('/knowledge/rollback', tags=['知识库'])
async def rollback_knowledge(body: RollbackInput, _: None = Depends(require_admin)):
    tool = _tool_manager._tools.get('knowledge_search') if _tool_manager else None
    if tool is None:
        raise HTTPException(503, '知识库未初始化')
    from tooling.knowledge_catalog import PublicationConflict
    try:
        generation = await asyncio.to_thread(tool.handler.__self__.rollback, body.generation)
    except PublicationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    for manager in _tool_managers:
        manager.invalidate_cache()
    return {'generation': generation}


@app.post("/eval/run")
async def run_eval(body: Optional[EvalRunInput] = None, _: None = Depends(require_admin)):
    """运行内置评测用例，返回评测报告。"""
    if _evaluator is None:
        raise HTTPException(503, "服务未就绪")
    from evaluation.evaluator import DEFAULT_DIALOG_CASES, DEFAULT_INTENT_CASES, IntentTestCase

    if body and body.intent_cases is not None:
        intent_cases = [
            IntentTestCase(
                message=c.message,
                expected_intent=c.expected_intent,
                context=c.context,
            )
            for c in body.intent_cases
        ]
    else:
        intent_cases = DEFAULT_INTENT_CASES

    if body and body.dialog_cases is not None:
        dialog_cases = [
            c.model_dump(exclude_none=True)
            for c in body.dialog_cases
        ]
    else:
        dialog_cases = DEFAULT_DIALOG_CASES

    from core.runtime import RUN_MODE
    from evaluation.manifest import benchmark_manifest
    async def knowledge_manifest():
        tool = _tool_manager._tools.get('knowledge_search') if _tool_manager else None
        backend = getattr(getattr(tool, 'handler', None), '__self__', None)
        if not backend or not hasattr(backend, 'publication_status'):
            return None
        return await asyncio.to_thread(backend.publication_status)
    knowledge_before = await knowledge_manifest()
    mode_token = RUN_MODE.set('evaluation')
    try:
        report = await _evaluator.run(intent_cases=intent_cases, dialog_cases=dialog_cases)
    finally:
        RUN_MODE.reset(mode_token)
    return {
        **benchmark_manifest(intent_cases, dialog_cases, knowledge_before=knowledge_before,
                             knowledge_after=await knowledge_manifest()),
        "suite_version":   report.suite_version,
        "pass_rate":       report.pass_rate,
        "total":           report.total,
        "passed":          report.passed,
        "avg_scores":      report.avg_scores,
        "judge_stats":     report.judge_stats,
        "regressions":     report.regressions,
        "recommendations": report.recommendations,
        "results": [
            {
                "test_id": r.test_id,
                "passed": r.passed,
                "scores": r.scores,
                "detail": r.detail,
                "metadata": r.metadata,
            }
            for r in report.results
        ],
    }


from api.task_routes import build_task_router
app.include_router(build_task_router(lambda: (_memory, _job_store), require_admin, EvalRunInput))

# ── 交互式 CLI ────────────────────────────────────────────────────────────────
async def _cli():
    print(BANNER)
    print("河北工业大学本科报考咨询 CLI — 输入 quit 退出\n")

    async with lifespan(app):
        user_id, conv_id = "cli_" + uuid.uuid4().hex, str(uuid.uuid4())
        while True:
            try:
                msg = (await asyncio.to_thread(input, "You: ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not msg or msg.lower() in ("quit", "exit"):
                break
            result = await _chat_service.handle(
                msg, user_id=user_id, conv_id=conv_id, client_request_id=uuid.uuid4().hex)
            print(f"\n[{result.agent_type}]: {result.response}\n")


if __name__ == "__main__":
    if "--cli" in sys.argv:
        asyncio.run(_cli())
    else:
        uvicorn.run(
            "api.main:app",
            host=os.getenv("API_HOST", "0.0.0.0"),
            port=int(os.getenv("API_PORT", "8000")),
            reload=os.getenv("APP_ENV") == "development",
        )
