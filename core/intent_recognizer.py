"""
亮点：端到端意图识别

三路融合策略：
  1. LLM 语义理解（权重 70%）—— 主力，理解复杂语义和上下文
  2. Embedding 向量相似度（权重 20%）—— 快速匹配常见表达
  3. 关键词模式匹配（权重 10%）—— 零延迟兜底

三路结果通过加权投票合并，置信度低于阈值时降级为 OTHER。
LLM 和 Embedding 并行调用，不串行等待。
"""
import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic

from core.llm_client import create_message, extract_text

logger = logging.getLogger(__name__)


class IntentCategory(Enum):
    SCHOOL_INFO      = "school_info"       # 学校概况、校区、办学特色
    MAJOR_INFO       = "major_info"        # 专业介绍、课程、适合人群
    ADMISSION_POLICY = "admission_policy"  # 招生章程、录取规则、调剂、转专业
    SCORE_RISK       = "score_risk"        # 分数、位次、冲稳保风险
    TUITION          = "tuition"           # 学费、住宿费、奖助学金
    CAMPUS_LIFE      = "campus_life"       # 宿舍、食堂、社团、校园生活
    CAREER           = "career"            # 就业、升学、行业前景
    COMPARISON       = "comparison"        # 专业、方向、校区对比
    GREETING         = "greeting"          # 问候
    ESCALATION       = "escalation"        # 招生办、人工确认、投诉
    OTHER            = "other"


class UrgencyLevel(Enum):
    LOW      = 1
    MEDIUM   = 2
    HIGH     = 3
    CRITICAL = 4


@dataclass
class IntentResult:
    intent:     IntentCategory
    confidence: float
    urgency:    UrgencyLevel
    entities:   Dict[str, List[str]]   # 从消息中提取的实体
    reasoning:  str
    latency_ms: float


# ── Few-shot 模板（同时用于 LLM 示例和 Embedding 匹配）────────────────────────
_TEMPLATES: Dict[IntentCategory, List[str]] = {
    IntentCategory.SCHOOL_INFO: ["河北工业大学是什么层次的学校？", "学校有几个校区？", "学校有什么办学特色？"],
    IntentCategory.MAJOR_INFO: ["电气工程专业主要学什么？", "计算机专业适合什么样的学生？", "请介绍一下机械类专业"],
    IntentCategory.ADMISSION_POLICY: ["学校的专业录取规则是什么？", "不服从调剂会退档吗？", "色弱可以报哪些专业？"],
    IntentCategory.SCORE_RISK: ["河北物理类620分报河工大稳吗？", "计算机专业去年最低位次是多少？", "今年在山东招多少人？"],
    IntentCategory.TUITION: ["普通本科一年学费多少？", "住宿费怎么收？", "学校有哪些奖助学金？"],
    IntentCategory.CAMPUS_LIFE: ["宿舍条件怎么样？", "学校食堂如何？", "有哪些学生社团？"],
    IntentCategory.CAREER: ["电气专业毕业后能做什么？", "这个专业保研和考研方向有哪些？", "毕业生主要去哪些行业？"],
    IntentCategory.COMPARISON: ["电气和自动化哪个更适合我？", "北辰校区和红桥校区有什么区别？", "几个中外合作项目怎么选？"],
    IntentCategory.GREETING: ["你好", "嗨，有人吗", "早上好"],
    IntentCategory.ESCALATION: ["请给我招生办联系方式", "这个问题需要人工确认", "我要投诉招生咨询服务"],
}

# 紧急关键词
_URGENCY_KEYWORDS = {
    UrgencyLevel.CRITICAL: ["截止今天", "最后一天", "已经退档", "录取异常"],
    UrgencyLevel.HIGH:     ["马上截止", "填报截止", "急需确认", "尽快确认"],
    UrgencyLevel.MEDIUM:   ["这周填报", "近期填报", "等待录取"],
}


def _cosine(a: List[float], b: List[float]) -> float:
    """纯 Python 余弦相似度，不依赖 numpy。"""
    dot = sum(x * y for x, y in zip(a, b))
    na  = sum(x * x for x in a) ** 0.5
    nb  = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


class IntentRecognizer:
    """
    端到端意图识别器。

    初始化时不加载任何本地模型，所有 AI 能力通过 Anthropic API 调用。
    模板 Embedding 在首次请求时懒加载并缓存，后续复用。
    """

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "claude-3-5-sonnet-20241022",
        confidence_threshold: float = 0.5,
    ):
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client    = AsyncAnthropic(**kwargs)
        self.model     = model
        self.threshold = confidence_threshold
        # 第三方兼容 API（如 DeepSeek）通常不支持 Embedding，禁用该策略。
        # 官方 Anthropic SDK 当前没有 embeddings 资源，因此下面会使用稳定的
        # 本地字符 n-gram 向量作为轻量兜底，保证三路融合链路真实可跑。
        self._embedding_enabled = not bool(base_url)

        self._tpl_embeddings: Dict[IntentCategory, List[List[float]]] = {}
        self._cache: Dict[str, IntentResult] = {}
        self.cache_hits   = 0
        self.cache_misses = 0

    # ── 公开接口 ──────────────────────────────────────────────────────────────

    async def recognize(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> IntentResult:
        """
        识别用户意图。

        history 格式：[{"role": "user"/"assistant", "content": "..."}]
        """
        key = self._cache_key(message, history)
        if key in self._cache:
            self.cache_hits += 1
            return self._cache[key]
        self.cache_misses += 1

        t0 = time.monotonic()

        # LLM 和 Embedding 并行（Embedding 不可用时跳过）
        llm_task = asyncio.create_task(self._llm_recognize(message, history))
        emb_task = asyncio.create_task(self._embedding_recognize(message)) if self._embedding_enabled else None
        pat      = self._pattern_recognize(message)

        if emb_task:
            llm, emb = await asyncio.gather(llm_task, emb_task)
        else:
            llm = await llm_task
            emb = {"intent": IntentCategory.OTHER, "confidence": 0.0}

        intent = self._vote(llm, emb, pat)
        entities = await self._extract_entities(message)
        urgency  = self._urgency(message, intent)

        result = IntentResult(
            intent=intent,
            confidence=float(llm.get("confidence", 0.0)),
            urgency=urgency,
            entities=entities,
            reasoning=llm.get("reasoning", ""),
            latency_ms=(time.monotonic() - t0) * 1000,
        )

        # LRU 缓存
        if len(self._cache) >= 1000:
            for k in list(self._cache)[:500]:
                del self._cache[k]
        self._cache[key] = result
        return result

    def learn(self, message: str, correct: IntentCategory) -> None:
        """在线学习：将纠正样本加入模板，清除对应 Embedding 缓存。"""
        tpls = _TEMPLATES.setdefault(correct, [])
        if message not in tpls:
            tpls.append(message)
            self._tpl_embeddings.pop(correct, None)  # 下次重新计算
            logger.info(f"学习新样本 → {correct.value}: {message[:40]}")

    # ── 三路识别策略 ──────────────────────────────────────────────────────────

    async def _llm_recognize(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]],
    ) -> Dict[str, Any]:
        """策略 1：LLM 语义理解（Few-shot + 上下文）。"""
        message = self._clean_text(message)
        # 构建 Few-shot 示例
        examples = "\n".join(
            f'  消息: "{t}" → 意图: {cat.value}'
            for cat, tpls in _TEMPLATES.items()
            for t in tpls[:1]  # 每类取 1 条，控制 prompt 长度
        )
        # 最近 3 轮对话上下文
        ctx = ""
        if history:
            ctx = "\n最近对话:\n" + "\n".join(
                f"  {self._clean_text(m.get('role', 'user'))}: {self._clean_text(m.get('content', ''))}"
                for m in history[-3:]
            )

        prompt = f"""你是河北工业大学普通本科报考咨询的意图分析专家。根据示例和上下文判断用户的主要意图，返回 JSON。

边界说明：招生计划的具体人数、历年分数、位次和录取风险归 score_risk；学费、住宿费和奖助规则归 tuition；专业内容与适合人群归 major_info；就业与升学归 career；需要比较多个选项时归 comparison。学校概况、校区和办学特色归 school_info；宿舍、食堂、社团和校园生活归 campus_life。

示例:
{examples}

{ctx}
用户消息: "{message}"

返回格式（仅 JSON，不要其他文字）:
{{"intent": "<意图值>", "confidence": <0-1>, "reasoning": "<一句话说明>"}}

可选意图: {", ".join(c.value for c in IntentCategory)}"""
        prompt = self._clean_text(prompt)

        try:
            resp = await create_message(
                self.client,
                model=self.model,
                max_tokens=256,
                temperature=0.1,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = extract_text(resp)
            s, e = raw.find("{"), raw.rfind("}") + 1
            data = json.loads(raw[s:e])
            try:
                data["intent"] = IntentCategory(data["intent"])
            except ValueError:
                data["intent"] = IntentCategory.OTHER
            return data
        except Exception as ex:
            logger.warning(f"LLM 识别失败: {ex}")
            return {"intent": IntentCategory.OTHER, "confidence": 0.0, "reasoning": "LLM 失败", "failed": True}

    async def _embedding_recognize(self, message: str) -> Dict[str, Any]:
        """策略 2：Embedding 向量相似度匹配。"""
        try:
            await self._load_template_embeddings()
            msg_vec = await self._embed_text(message)

            best_cat, best_score = IntentCategory.OTHER, 0.0
            for cat, vecs in self._tpl_embeddings.items():
                score = max(_cosine(msg_vec, v) for v in vecs)
                if score > best_score:
                    best_score, best_cat = score, cat

            return {"intent": best_cat, "confidence": best_score}
        except Exception as ex:
            logger.warning(f"Embedding 识别失败: {ex}")
            return {"intent": IntentCategory.OTHER, "confidence": 0.0}

    def _pattern_recognize(self, message: str) -> Dict[str, Any]:
        """策略 3：关键词模式匹配（同步，零延迟兜底）。"""
        msg = message.lower()
        patterns = {
            IntentCategory.ESCALATION: ["招生办", "人工", "联系电话", "联系方式", "投诉", "官方确认"],
            IntentCategory.SCORE_RISK: ["分数", "位次", "最低分", "最低位次", "录取概率", "能录取", "能上", "稳不稳", "冲稳保", "招生计划", "招多少"],
            IntentCategory.ADMISSION_POLICY: ["招生章程", "录取规则", "投档", "提档", "退档", "调剂", "专业级差", "级差", "平行志愿", "体检", "色盲", "色弱", "转专业", "选科"],
            IntentCategory.TUITION: ["学费", "住宿费", "奖学金", "助学金", "助学贷款", "收费"],
            IntentCategory.COMPARISON: ["对比", "比较", "区别", "哪个更", "怎么选"],
            IntentCategory.CAREER: ["就业", "岗位", "行业", "薪资", "考研", "保研", "升学", "前景"],
            IntentCategory.MAJOR_INFO: ["专业", "课程", "培养方向", "学什么", "适合"],
            IntentCategory.CAMPUS_LIFE: ["宿舍", "食堂", "社团", "交通", "校园生活", "校园环境"],
            IntentCategory.SCHOOL_INFO: ["学校概况", "学校介绍", "校区", "双一流", "优势学科", "办学层次"],
            IntentCategory.GREETING: ["你好", "您好", "嗨", "hello", "hi"],
        }
        best_cat, best_score = IntentCategory.OTHER, 0.0
        for cat, kws in patterns.items():
            hits = sum(1 for kw in kws if kw in msg)
            if hits:
                # 关键词表长度不应稀释命中分；多命中用于打破交叉场景的平分。
                score = min(1.0, 0.6 + 0.15 * (hits - 1))
                if score > best_score:
                    best_score, best_cat = score, cat
        return {"intent": best_cat, "confidence": best_score}

    # ── 投票合并 ──────────────────────────────────────────────────────────────

    def _vote(self, llm: Dict, emb: Dict, pat: Dict) -> IntentCategory:
        """加权投票。embedding 不可用时权重自动转移到 LLM 和 Pattern。"""
        if llm.get("failed"):
            if emb.get("intent") != IntentCategory.OTHER and emb.get("confidence", 0.0) > 0:
                return emb["intent"]
            if pat.get("intent") != IntentCategory.OTHER and pat.get("confidence", 0.0) > 0:
                return pat["intent"]
            return IntentCategory.OTHER

        if self._embedding_enabled:
            weights = [(llm, 0.7), (emb, 0.2), (pat, 0.1)]
        else:
            weights = [(llm, 0.85), (pat, 0.15)]
        scores: Dict[IntentCategory, float] = {}
        for result, w in weights:
            cat  = result.get("intent", IntentCategory.OTHER)
            conf = result.get("confidence", 0.0)
            scores[cat] = scores.get(cat, 0.0) + w * conf

        best = max(scores, key=scores.get)  # type: ignore
        return best if scores[best] >= self.threshold else IntentCategory.OTHER

    # ── 实体提取 ──────────────────────────────────────────────────────────────

    async def _extract_entities(self, message: str) -> Dict[str, List[str]]:
        """用 LLM 从消息中提取结构化实体。"""
        message = self._clean_text(message)
        prompt = f"""从河北工业大学普通本科报考咨询中提取实体，返回 JSON（字段值为列表，没有则为空列表，不推测用户没有提供的信息）:
消息: "{message}"
格式: {{"admission_year":[],"province":[],"exam_mode":[],"subject_combination":[],"batch":[],"score":[],"rank":[],"major":[],"college":[],"campus":[],"candidate_type":[]}}"""
        prompt = self._clean_text(prompt)
        try:
            resp = await create_message(
                self.client,
                model=self.model, max_tokens=256, temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = extract_text(resp)
            s, e = raw.find("{"), raw.rfind("}") + 1
            return json.loads(raw[s:e])
        except Exception:
            return {
                "admission_year": [], "province": [], "exam_mode": [],
                "subject_combination": [], "batch": [], "score": [], "rank": [],
                "major": [], "college": [], "campus": [], "candidate_type": [],
            }

    # ── 辅助 ──────────────────────────────────────────────────────────────────

    async def _load_template_embeddings(self) -> None:
        """懒加载所有模板的 Embedding（只在首次调用时执行）。"""
        missing = [cat for cat in _TEMPLATES if cat not in self._tpl_embeddings]
        if not missing:
            return

        all_texts = [t for cat in missing for t in _TEMPLATES[cat]]
        vecs = [await self._embed_text(text) for text in all_texts]
        idx = 0
        for cat in missing:
            n = len(_TEMPLATES[cat])
            self._tpl_embeddings[cat] = vecs[idx: idx + n]
            idx += n

    async def _embed_text(self, text: str) -> List[float]:
        """
        生成文本向量。

        如果未来接入的官方/兼容客户端提供 embeddings.create，会优先使用远端向量；
        当前 Anthropic SDK 没有该资源时，退化为字符 n-gram 哈希向量。这样不会因为
        Embedding 服务缺失导致三路融合中断。
        """
        embeddings = getattr(self.client, "embeddings", None)
        if embeddings is not None:
            try:
                resp = await embeddings.create(model="voyage-3-lite", input=[text])
                return list(resp.data[0].embedding)
            except Exception as ex:
                logger.warning(f"远端 Embedding 失败，使用本地向量兜底: {ex}")

        return self._local_embedding(text)

    @staticmethod
    def _local_embedding(text: str, dims: int = 256) -> List[float]:
        """稳定的字符 n-gram 哈希向量，用于无远端 Embedding 时的语义近似匹配。"""
        normalized = text.lower().strip()
        vec = [0.0] * dims
        tokens = set()
        for n in (1, 2, 3):
            if len(normalized) >= n:
                tokens.update(normalized[i:i + n] for i in range(len(normalized) - n + 1))
        if not tokens:
            tokens.add(normalized)

        for token in tokens:
            digest = hashlib.md5(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % dims
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        return vec

    def _urgency(self, message: str, intent: IntentCategory) -> UrgencyLevel:
        msg = message.lower()
        for level, kws in _URGENCY_KEYWORDS.items():
            if any(kw in msg for kw in kws):
                return level
        if intent == IntentCategory.ESCALATION:
            return UrgencyLevel.HIGH
        return UrgencyLevel.LOW

    def _cache_key(self, message: str, history: Optional[List[Dict[str, str]]] = None) -> str:
        """缓存键包含近期上下文，避免“这个专业呢”等追问跨会话误命中。"""
        recent = "|".join(
            f"{m.get('role', '')}:{m.get('content', '')}" for m in (history or [])[-3:]
        )
        raw = self._clean_text(f"hebut-v1|{recent}|{message}")
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _clean_text(value: Any) -> str:
        """移除 Unicode 代理字符，避免 HTTP 客户端编码 prompt 时崩溃。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    @property
    def cache_stats(self) -> Dict[str, Any]:
        total = self.cache_hits + self.cache_misses
        return {
            "size": len(self._cache),
            "hits": self.cache_hits,
            "misses": self.cache_misses,
            "hit_rate": self.cache_hits / total if total else 0.0,
        }
