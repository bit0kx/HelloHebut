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

    print(BANNER, flush=True)

    from agents.agent_orchestrator import AgentOrchestrator, Request
    from core.intent_recognizer import IntentRecognizer
    from core.chat_service import ChatService
    from evaluation.evaluator import EndToEndEvaluator
    from mcp.admission_data import AdmissionDataStore
    from mcp.knowledge_base import KnowledgeBase
    from mcp.tool_manager import MCPToolManager, Tool
    from memory.conversation_memory import MemoryManager
    from monitor.performance_monitor import PerformanceMonitor
    from core.skill_loader import SkillManager

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
    )

    # 记忆管理器（Redis 工作记忆 + ChromaDB 情景记忆/用户画像）
    _memory = MemoryManager(
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
        chroma_host=os.getenv("CHROMA_HOST", "chromadb"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/app/data/chroma"),
        collection_name=os.getenv("KNOWLEDGE_COLLECTION", "hello_hebut"),
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
                    "query": {"type": "string"},
                    "top_k": {"type": "integer"},
                    "categories": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["query"],
            },
            cache_ttl=300.0,
            supports_rerank=True,
            fallback=knowledge_fallback,
        ))
        manager.register(Tool(
            name="admission_data_query",
            description="确定性查询河北工业大学河北、天津2023—2025年分专业最低分和最低位次，并生成可解释风险等级",
            handler=_admission_store.query_handler,
            schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "entities": {"type": "object"},
                    "history": {"type": "array"},
                },
                "required": ["query"],
            },
            cache_ttl=600.0,
            fallback=admission_fallback,
        ))
        return manager

    _tool_manager = build_tool_manager()

    _chat_service = ChatService(_orchestrator, _memory, _tool_manager, recognizer=recognizer)

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
    )
    eval_tool_manager = build_tool_manager()
    eval_chat_service = ChatService(eval_orchestrator, _memory, eval_tool_manager, recognizer=recognizer)
    _evaluator = EndToEndEvaluator(
        chat_service=eval_chat_service,
        recognizer=recognizer,
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=os.getenv("EVAL_JUDGE_MODEL", cfg["model"]),
        baseline_path=os.getenv("EVAL_BASELINE_PATH", "/app/data/eval/baseline.json"),
    )

    logger.info("河北工业大学本科报考咨询服务已就绪")
    yield

    await _monitor.stop()
    logger.info("河北工业大学本科报考咨询服务已关闭")


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="河北工业大学本科报考咨询",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
)

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
    message:     str
    user_id:     Optional[str] = None
    conv_id:     Optional[str] = None


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


def require_admin(x_admin_token: Optional[str] = Header(default=None)) -> None:
    """配置 ADMIN_API_TOKEN 后保护知识写入、Skill 重载和评测入口。"""
    expected = os.getenv("ADMIN_API_TOKEN", "").strip()
    if expected and x_admin_token != expected:
        raise HTTPException(401, "缺少或使用了无效的管理令牌")


# ── 路由 ──────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    return {"status": "ok", "agents": _orchestrator.get_stats()}


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


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """
    主对话接口。完整流程：
      记忆读取 → 意图识别 → 结构化录取数据/官方知识检索 → Agent 路由 → 执行 → 记忆写入
    """
    if _chat_service is None:
        raise HTTPException(503, "服务未就绪")
    conv_id = req.conv_id or str(uuid.uuid4())
    user_id = req.user_id or f"anon_{conv_id}"
    result = await _chat_service.handle(req.message, user_id, conv_id)

    return ChatResponse(
        conv_id=result.conv_id,
        response=result.response,
        intent=result.intent,
        agent_type=result.agent_type,
        escalated=result.escalated,
        latency_ms=result.latency_ms,
        knowledge_used=result.knowledge_used,
        admission_data_used=result.admission_data_used,
        citations=result.citations,
        entities=result.entities,
    )


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
    count = kb.add_documents([d.model_dump(exclude_none=True) for d in body.documents])
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

    count = kb.add_documents(docs)
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
    }


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

    report = await _evaluator.run(
        intent_cases=intent_cases,
        dialog_cases=dialog_cases,
    )
    return {
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


# ── 交互式 CLI ────────────────────────────────────────────────────────────────
async def _cli():
    print(BANNER)
    print("河北工业大学本科报考咨询 CLI — 输入 quit 退出\n")

    from agents.agent_orchestrator import AgentOrchestrator, Request
    from memory.conversation_memory import MemoryManager, MsgRole
    from core.skill_loader import SkillManager

    cfg = _anthropic_cfg()
    skill_manager = SkillManager(
        root_dir=os.getenv("HELLOHEBUT_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills")),
        max_prompt_chars=int(os.getenv("HELLOHEBUT_SKILLS_MAX_PROMPT_CHARS", "5000")),
    )
    skill_manager.load()
    orch = AgentOrchestrator(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        skill_manager=skill_manager,
    )
    mem  = MemoryManager(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        chroma_host=os.getenv("CHROMA_HOST", "localhost"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/tmp/chroma"),
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )

    user_id, conv_id = "cli_user", str(uuid.uuid4())

    while True:
        try:
            msg = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见 ʕ•ᴥ•ʔ")
            break
        if not msg or msg.lower() in ("quit", "exit", "退出"):
            print("再见 ʕ•ᴥ•ʔ")
            break

        ctx = await mem.get_context(user_id, conv_id, query=msg)
        history = [
            {"role": m.role.value, "content": m.content}
            for m in ctx.recent_messages[-5:]
        ] if ctx.recent_messages else None
        req = Request(message=msg, user_id=user_id, conv_id=conv_id, context=ctx.to_prompt_text(), history=history)
        result = await orch.run(req)

        await mem.add_message(user_id, conv_id, MsgRole.USER, msg)
        await mem.add_message(user_id, conv_id, MsgRole.ASSISTANT, result.response)

        print(f"\n河工大报考助手 [{result.agent_type.value}]: {result.response}\n")


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
