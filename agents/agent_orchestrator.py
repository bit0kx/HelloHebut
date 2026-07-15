"""
亮点：多 Agent 路由与编排

核心问题：多 Agent 情况下如何做 Routing？

路由策略（三层决策）：
  1. 意图路由 —— 根据 IntentCategory 直接映射到专属 Agent
  2. 性能路由 —— 同类 Agent 有多个时，选成功率最高、延迟最低的
  3. 降级路由 —— 专属 Agent 不可用时，自动降级到 GeneralAgent

并行协作：
  - 复杂问题（如"录取风险 + 专业规划"）可同时派发给多个 Agent
  - 结果由 Orchestrator 合并后返回

升级机制：
  - 明确要求人工、紧急个案或 Agent 无法核实 → 标记招生办人工确认
"""
import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic

from core.intent_recognizer import IntentCategory, IntentRecognizer, UrgencyLevel
from core.llm_client import create_message, extract_text

logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────────────────────────────────

class AgentType(Enum):
    GENERAL  = "general"   # 学校事实、校园生活、澄清与兜底
    POLICY   = "policy"    # 招生规则、收费与官方流程
    RISK     = "risk"      # 分数位次、计划与录取风险
    PLANNING = "planning"  # 专业选择、就业升学与志愿规划


@dataclass
class AgentStats:
    """Agent 运行时统计，供 Monitor 和路由决策使用。"""
    total:     int   = 0
    success:   int   = 0
    total_ms:  float = 0.0
    monitor_penalty: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 1.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.total if self.total else 0.0

    def routing_score(self) -> float:
        """路由评分：成功率高、延迟低的 Agent 得分高。"""
        latency_score = 1.0 / (1.0 + self.avg_ms / 1000)
        base_score = self.success_rate * 0.7 + latency_score * 0.3
        return base_score * max(0.0, 1.0 - self.monitor_penalty)


@dataclass
class AgentResponse:
    agent_type:  AgentType
    content:     str
    success:     bool
    confidence:  float = 1.0
    latency_ms:  float = 0.0
    escalate:    bool  = False   # 是否需要升级


@dataclass
class Request:
    message:     str
    user_id:     str
    conv_id:     str
    context:     str = ""        # 来自 MemoryManager 的格式化上下文
    history:     Optional[List[Dict[str, str]]] = None  # 对话历史，传给意图识别
    intent:      Optional[IntentCategory] = None
    urgency:     Optional[UrgencyLevel]   = None
    entities:    Dict[str, List[str]] = field(default_factory=dict)
    request_id:  str = field(default_factory=lambda: str(uuid.uuid4())[:8])


@dataclass
class OrchestratorResult:
    request_id:  str
    response:    str
    agent_type:  AgentType
    intent:      Optional[IntentCategory]
    escalated:   bool  = False
    latency_ms:  float = 0.0
    entities:    Dict[str, List[str]] = field(default_factory=dict)


# ── 基础 Agent ────────────────────────────────────────────────────────────────

class BaseAgent:
    """所有 Agent 的基类，封装 LLM 调用和统计。"""

    agent_type: AgentType
    system_prompt: str

    def __init__(self, client: AsyncAnthropic, model: str, skill_manager: Optional[Any] = None):
        self._client = client
        self._model  = model
        self._skill_manager = skill_manager
        self.stats   = AgentStats()

    async def handle(self, req: Request) -> AgentResponse:
        t0 = time.monotonic()
        self.stats.total += 1
        try:
            content = await self._call_llm(req)
            ms = (time.monotonic() - t0) * 1000
            self.stats.success += 1
            self.stats.total_ms += ms
            escalate = self._needs_escalation(content)
            return AgentResponse(
                agent_type=self.agent_type,
                content=content,
                success=True,
                latency_ms=ms,
                escalate=escalate,
            )
        except Exception as ex:
            ms = (time.monotonic() - t0) * 1000
            self.stats.total_ms += ms
            logger.error(f"{self.agent_type.value} 处理失败: {ex}")
            return AgentResponse(
                agent_type=self.agent_type,
                content="抱歉，处理您的请求时出现问题，请稍后重试。",
                success=False,
                latency_ms=ms,
            )

    async def _call_llm(self, req: Request) -> str:
        def _clean(s: str) -> str:
            return s.encode("utf-8", errors="ignore").decode("utf-8")

        messages = []
        background = req.context
        if req.entities:
            entity_text = json.dumps(req.entities, ensure_ascii=False)
            background = f"{background}\n\n[结构化报考信息]\n{entity_text}".strip()
        if background:
            messages.append({"role": "user", "content": f"[背景信息]\n{_clean(background)}"})
            messages.append({"role": "assistant", "content": "好的，我已了解背景信息。"})
        messages.append({"role": "user", "content": _clean(req.message)})

        resp = await create_message(
            self._client,
            model=self._model,
            max_tokens=1024,
            system=self._build_system_prompt(req),
            messages=messages,
        )
        return extract_text(resp)

    def _build_system_prompt(self, req: Request) -> str:
        """把动态加载的 Skills 拼入 system prompt，让业务规则随请求生效。"""
        if self._skill_manager is None:
            return self.system_prompt
        skill_prompt = self._skill_manager.prompt_for(req.message, self.agent_type.value)
        if not skill_prompt:
            return self.system_prompt
        return f"{self.system_prompt}\n\n[动态 Skills]\n{skill_prompt}"

    def _needs_escalation(self, content: str) -> bool:
        """检测 Agent 是否建议升级（简单关键词检测）。"""
        keywords = ["需要招生办人工确认", "请联系招生办人工确认", "无法核实该个案"]
        return any(kw in content for kw in keywords)


_COMMON_RULES = (
    "你是河北工业大学普通本科报考咨询助手。只回答普通本科招生相关问题。"
    "招生政策、计划、收费、校区和录取数据等事实必须优先依据背景中的官方知识；"
    "资料不足、年份不明或来源冲突时要明确说明并追问，"
    "不得编造分数、位次、计划、专业课程或录取结果。历史数据仅供参考，不能承诺录取。"
    "涉及身份证号、准考证号和录取结果查询时，不收集敏感信息，直接引导到官方渠道。"
)


class GeneralAgent(BaseAgent):
    agent_type    = AgentType.GENERAL
    system_prompt = (
        _COMMON_RULES
        + "你负责学校概况、办学层次、校区位置与特色，以及宿舍、食堂、交通、社团和校园体验等通用咨询。"
        "也负责问候、信息不足时的澄清、无法归类问题的兜底，并在必要时给出本科招生网和招生办等官方渠道。"
    )


class PolicyAgent(BaseAgent):
    agent_type    = AgentType.POLICY
    system_prompt = (
        _COMMON_RULES
        + "你负责解释招生章程、投档录取、专业调剂、退档条件、转专业、体检与选科限制、"
        "招生批次、收费标准、奖助规则，以及招生计划的官方公布规则。"
        "区分学校规则与省级招生主管部门规则；具体计划人数或录取数据查询应交给风险分析能力处理。"
    )


class RiskAgent(BaseAgent):
    agent_type    = AgentType.RISK
    system_prompt = (
        _COMMON_RULES
        + "你负责分数、位次、历年最低分、招生计划数量、冲稳保和录取风险判断。"
        "省份、科类、目标专业及分数/位次足够时，优先使用背景中的[结构化录取数据查询结果]。"
        "查询成功时必须逐年列出最低分、最低位次和考生位次差，采用工具给出的风险等级并说明命中年份；"
        "位次优先于裸分，只有裸分时明确标为低置信参考并提醒补充位次。"
        "缺少必要信息、数据不覆盖或记录不足时按工具状态澄清，不得给出概率、虚构数字或稳录承诺。"
    )


class PlanningAgent(BaseAgent):
    agent_type    = AgentType.PLANNING
    system_prompt = (
        _COMMON_RULES
        + "你负责专业学习内容、适合人群、专业比较、就业行业、考研保研方向、志愿组合、"
        "中外合作项目选择和长期学业职业规划。先理解兴趣、能力、选科和发展目标，再给出条件化建议；"
        "通用职业建议与学校官方培养信息必须明确区分。"
    )


# ── 编排器 ────────────────────────────────────────────────────────────────────

class AgentOrchestrator:
    """
    多 Agent 编排器。

    路由逻辑（三层）：
      1. 意图 → Agent 类型映射
      2. 同类多实例时按 routing_score() 选最优
      3. 专属 Agent 失败时降级到 GeneralAgent
    """

    # 意图 → Agent 类型的静态映射（路由表）
    _INTENT_ROUTING: Dict[IntentCategory, AgentType] = {
        IntentCategory.ADMISSION_POLICY: AgentType.POLICY,
        IntentCategory.TUITION:          AgentType.POLICY,
        IntentCategory.SCORE_RISK:       AgentType.RISK,
        IntentCategory.MAJOR_INFO:       AgentType.PLANNING,
        IntentCategory.CAREER:           AgentType.PLANNING,
        IntentCategory.COMPARISON:       AgentType.PLANNING,
    }

    def __init__(
        self,
        api_key:  str,
        base_url: Optional[str] = None,
        model:    str = "claude-3-5-sonnet-20241022",
        skill_manager: Optional[Any] = None,
    ):
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        client = AsyncAnthropic(**kwargs)

        self._intent_recognizer = IntentRecognizer(api_key=api_key, base_url=base_url, model=model)
        self._skill_manager = skill_manager

        # Agent 池：每种类型可有多个实例（水平扩展）
        self._pool: Dict[AgentType, List[BaseAgent]] = {
            AgentType.GENERAL:  [GeneralAgent(client, model, skill_manager)],
            AgentType.POLICY:   [PolicyAgent(client, model, skill_manager)],
            AgentType.RISK:     [RiskAgent(client, model, skill_manager)],
            AgentType.PLANNING: [PlanningAgent(client, model, skill_manager)],
        }

    def set_skill_manager(self, skill_manager: Optional[Any]) -> None:
        """更新 SkillManager 引用，供运行时重载或测试替换使用。"""
        self._skill_manager = skill_manager
        for agents in self._pool.values():
            for agent in agents:
                agent._skill_manager = skill_manager

    # ── 主入口 ────────────────────────────────────────────────────────────────

    async def run(self, req: Request) -> OrchestratorResult:
        """
        处理一次请求的完整流程：
          意图识别 → 路由选 Agent → 执行 → 检查升级 → 返回结果
        """
        t0 = time.monotonic()

        # 1. 意图识别（如果调用方已识别则跳过）
        if req.intent is None:
            intent_result = await self._intent_recognizer.recognize(req.message, history=req.history)
            req.intent  = intent_result.intent
            req.urgency = intent_result.urgency
            req.entities = intent_result.entities

        # 复杂问题自动并行协作，例如同一句同时涉及录取风险和专业规划。
        collaboration = self._collaboration_targets(req)
        if len(collaboration) > 1:
            return await self.run_parallel(req, collaboration)

        # 2. 路由：选择 Agent 类型
        agent_type = self._route(req.intent, req.urgency)

        # 3. 执行（含降级）
        response = await self._execute(req, agent_type)

        # 4. 升级检查
        escalated = False
        if response.escalate or req.urgency == UrgencyLevel.CRITICAL or req.intent == IntentCategory.ESCALATION:
            escalated = True
            logger.warning(f"请求 {req.request_id} 触发升级: urgency={req.urgency}")
            # 生产环境可在此创建招生咨询工单或通知人工值守。

        return OrchestratorResult(
            request_id=req.request_id,
            response=response.content,
            agent_type=response.agent_type,
            intent=req.intent,
            escalated=escalated,
            latency_ms=(time.monotonic() - t0) * 1000,
            entities=req.entities,
        )

    async def run_parallel(self, req: Request, agent_types: List[AgentType]) -> OrchestratorResult:
        """
        并行派发给多个 Agent，合并结果。
        适用于复杂问题（如同时涉及录取风险和专业规划）。
        """
        t0 = time.monotonic()
        tasks = [self._execute(req, at) for at in agent_types]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        # Agent 类型属于内部路由元数据，不混入用户可见正文。
        parts = []
        completed_agents = []
        for r in responses:
            if isinstance(r, AgentResponse) and r.success:
                content = r.content.strip()
                if content:
                    parts.append(content)
                    completed_agents.append(r.agent_type.value)

        combined = "\n\n".join(parts) if parts else "抱歉，所有 Agent 均处理失败。"
        logger.info(
            "请求 %s 多 Agent 协作完成: %s",
            req.request_id,
            ", ".join(completed_agents) or "none",
        )
        escalated = (
            any(isinstance(r, AgentResponse) and r.escalate for r in responses)
            or req.urgency == UrgencyLevel.CRITICAL
            or req.intent == IntentCategory.ESCALATION
        )

        return OrchestratorResult(
            request_id=req.request_id,
            response=combined,
            agent_type=agent_types[0],
            intent=req.intent,
            escalated=escalated,
            latency_ms=(time.monotonic() - t0) * 1000,
            entities=req.entities,
        )

    # ── 路由逻辑 ──────────────────────────────────────────────────────────────

    def _route(self, intent: Optional[IntentCategory], urgency: Optional[UrgencyLevel]) -> AgentType:
        """
        三层路由决策：
          1. 意图映射
          2. 紧急度覆盖（CRITICAL 交给通用入口并标记升级）
          3. 默认 GENERAL
        """
        if urgency == UrgencyLevel.CRITICAL:
            return AgentType.GENERAL

        if intent and intent in self._INTENT_ROUTING:
            target = self._INTENT_ROUTING[intent]
            # 如果目标类型有可用实例则使用，否则降级
            if target in self._pool and self._pool[target]:
                return target

        return AgentType.GENERAL

    def _collaboration_targets(self, req: Request) -> List[AgentType]:
        """
        判断是否需要多个 Agent 并行协作。

        意图识别通常只返回一个主意图；这里用领域关键词补充检测复合问题，
        例如“我的位次适合电气还是自动化，哪个就业方向更匹配”需要风险与规划能力协作。
        """
        msg = req.message.lower()
        targets: List[AgentType] = []

        policy_kws = ["录取规则", "调剂", "退档", "体检", "色盲", "色弱", "选科", "学费", "住宿费"]
        risk_kws = ["分数", "位次", "最低分", "稳不稳", "冲稳保", "录取概率", "招生计划", "招多少"]
        planning_kws = ["专业对比", "专业比较", "适合", "就业", "考研", "保研", "志愿搭配", "中外合作"]

        if req.intent in (IntentCategory.ADMISSION_POLICY, IntentCategory.TUITION) or any(kw in msg for kw in policy_kws):
            targets.append(AgentType.POLICY)
        if req.intent == IntentCategory.SCORE_RISK or any(kw in msg for kw in risk_kws):
            targets.append(AgentType.RISK)
        if req.intent in (IntentCategory.MAJOR_INFO, IntentCategory.CAREER, IntentCategory.COMPARISON) or any(kw in msg for kw in planning_kws):
            targets.append(AgentType.PLANNING)

        # 保持顺序去重，并只返回当前有实例的 Agent 类型。
        deduped = list(dict.fromkeys(targets))
        return [agent_type for agent_type in deduped if self._pool.get(agent_type)]

    def _best_agent(self, agent_type: AgentType) -> Optional[BaseAgent]:
        """
        性能路由：从同类 Agent 中选 routing_score() 最高的。
        这是"基于在线表现动态调整路由"的核心。
        """
        agents = self._pool.get(agent_type, [])
        if not agents:
            return None
        return max(agents, key=lambda a: a.stats.routing_score())

    async def _execute(self, req: Request, agent_type: AgentType) -> AgentResponse:
        """执行 Agent，失败时降级到 GeneralAgent。"""
        agent = self._best_agent(agent_type)
        if agent is None:
            agent = self._best_agent(AgentType.GENERAL)
        if agent is None:
            return AgentResponse(
                agent_type=AgentType.GENERAL,
                content="服务暂时不可用，请稍后重试。",
                success=False,
            )

        response = await agent.handle(req)

        # 专属 Agent 失败时降级到 GeneralAgent
        if not response.success and agent_type != AgentType.GENERAL:
            logger.warning(f"{agent_type.value} 失败，降级到 GeneralAgent")
            fallback = self._best_agent(AgentType.GENERAL)
            if fallback:
                response = await fallback.handle(req)

        return response

    # ── 统计（供 Monitor 读取）────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        result = {}
        for agent_type, agents in self._pool.items():
            for i, agent in enumerate(agents):
                key = f"{agent_type.value}_{i}"
                result[key] = {
                    "total":        agent.stats.total,
                    "success_rate": round(agent.stats.success_rate, 3),
                    "avg_ms":       round(agent.stats.avg_ms, 1),
                    "monitor_penalty": round(agent.stats.monitor_penalty, 3),
                    "routing_score": round(agent.stats.routing_score(), 3),
                }
        return result

    def update_routing_penalties(self, penalties: Dict[str, float]) -> None:
        """
        接收 Monitor 的在线表现反馈，动态调整路由惩罚项。

        penalties 的 key 使用 get_stats() 中的 agent key，例如 risk_0。
        """
        for agent_type, agents in self._pool.items():
            for i, agent in enumerate(agents):
                key = f"{agent_type.value}_{i}"
                penalty = penalties.get(key, 0.0)
                agent.stats.monitor_penalty = min(max(penalty, 0.0), 0.9)
