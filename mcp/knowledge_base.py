"""
RAG 知识库 —— 基于 ChromaDB 的真实检索实现。

功能：
  1. 文档导入：将文本切片后存入 ChromaDB（自动生成 Embedding）
  2. 语义检索：根据 query 从知识库中检索最相关的文档片段
  3. 与 MCP 工具框架集成：作为 knowledge_search 工具的真实 handler

ChromaDB 在这里的角色：
  - memory/ 中用于存储对话记忆（情景记忆 + 用户画像）
  - 这里用于存储知识库文档（RAG 检索）
  两者是不同的 collection，互不干扰。
"""
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.knowledge_indexing import INDEX_VERSION, contextual_chunks, lexical_score, split_text

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """
    基于 ChromaDB 的 RAG 知识库。

    ChromaDB 内置了 Embedding 模型（all-MiniLM-L6-v2），
    调用 add() 时自动生成向量，query() 时自动做语义匹配。
    不需要额外调用 Anthropic Embeddings API。
    """

    COLLECTION_NAME = "hello_hebut"
    SEED_EXTENSIONS = {".txt", ".md", ".json"}

    def __init__(
        self,
        chroma_host: str = "localhost",
        chroma_port: int = 8000,
        chroma_path: str = "./data/chroma",
        collection_name: Optional[str] = None,
        seed_dir: Optional[str] = None,
    ):
        import chromadb

        # 优先连接独立 ChromaDB 服务（服务端内置 embedding 模型，客户端无需下载）
        self._use_server = False
        try:
            # HttpClient 默认也会初始化 ChromaDB telemetry；显式关闭避免 posthog 兼容性错误日志。
            self._client = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._client.heartbeat()
            self._use_server = True
            logger.info(f"知识库 ChromaDB 已连接: {chroma_host}:{chroma_port}")
        except Exception:
            logger.info(f"知识库 ChromaDB 服务不可用，使用本地模式: {chroma_path}")
            self._client = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # 使用服务端时不传 embedding_function，让服务端处理
        # 本地模式时也不传，使用 ChromaDB 默认的（会触发模型下载）
        self._collection = self._client.get_or_create_collection(
            name=collection_name or self.COLLECTION_NAME,
            metadata={"description": "河北工业大学普通本科报考咨询官方知识库"},
        )

        # 首次启动或索引规则升级时重建内置知识；不重复向量化已是最新版的内容。
        builtin = self._collection.get(
            where={"source_type": "builtin"},
            include=["metadatas"],
        )
        builtin_metas = builtin.get("metadatas") or []
        if not builtin.get("ids") or any(
            meta.get("index_version") != INDEX_VERSION for meta in builtin_metas
        ):
            self._load_default_docs()

        self.last_seed_report: Dict[str, Any] = {
            "directory": seed_dir or "",
            "discovered_files": 0,
            "imported_files": 0,
            "skipped_files": 0,
            "failed_files": 0,
            "added_chunks": 0,
            "errors": [],
        }
        if seed_dir:
            self.last_seed_report = self.import_seed_directory(seed_dir)

    # ── 文档管理 ──────────────────────────────────────────────────────────────

    def add_documents(self, documents: List[Dict[str, Any]]) -> int:
        """
        批量导入文档到知识库。

        documents 格式: [{"title": "...", "content": "..."}, ...]
        长文档会自动切片（每片 500 字）。
        """
        ids, docs, metas = [], [], []

        for doc in documents:
            title   = str(doc.get("title", "")).strip()
            raw_meta = doc.get("metadata")
            base_meta = {
                str(key): value
                for key, value in (raw_meta.items() if isinstance(raw_meta, dict) else [])
                if isinstance(value, (str, int, float, bool))
            }
            for key in (
                "source_url",
                "source_file",
                "source_type",
                "category",
                "keywords",
                "published_at",
                "fetched_at",
                "effective_year",
                "document_version",
                "college",
                "major",
                "section",
            ):
                value = doc.get(key)
                if isinstance(value, (str, int, float, bool)):
                    base_meta[key] = value
            if isinstance(doc.get("keywords"), (list, tuple, set)):
                base_meta["keywords"] = "、".join(map(str, doc["keywords"]))
            chunks = contextual_chunks({**doc, "metadata": base_meta}, chunk_size=500)

            for i, chunk in enumerate(chunks):
                raw_content = chunk["content"]
                identity = (
                    f"{base_meta.get('source_url', '')}|{base_meta.get('source_file', '')}|"
                    f"{base_meta.get('document_version', '')}|{title}|{i}|{raw_content[:80]}"
                )
                doc_id = hashlib.md5(identity.encode()).hexdigest()
                ids.append(doc_id)
                docs.append(chunk["index_text"])
                metas.append({
                    **base_meta,
                    "title": title,
                    "raw_content": raw_content,
                    "college": chunk["college"],
                    "major": chunk["major"],
                    "section": chunk["section"],
                    "index_version": INDEX_VERSION,
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                })

        if ids:
            # ChromaDB 会自动生成 Embedding
            self._collection.upsert(ids=ids, documents=docs, metadatas=metas)
            logger.info(f"知识库导入 {len(ids)} 个文档片段")

        return len(ids)

    def import_seed_directory(self, directory: str) -> Dict[str, Any]:
        """扫描部署资料目录并增量导入；内容未变化的文件不会重复向量化。"""
        root = Path(directory).expanduser().resolve()
        report: Dict[str, Any] = {
            "directory": str(root),
            "discovered_files": 0,
            "imported_files": 0,
            "skipped_files": 0,
            "failed_files": 0,
            "added_chunks": 0,
            "errors": [],
        }
        if not root.is_dir():
            logger.warning("知识库自动导入目录不存在，已跳过: %s", root)
            return report

        files = sorted(
            path for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in self.SEED_EXTENSIONS
        )
        report["discovered_files"] = len(files)

        for path in files:
            source_file = path.relative_to(root).as_posix()
            try:
                raw = path.read_bytes()
                version = hashlib.sha256(raw).hexdigest()
                documents = self._parse_seed_file(path, raw, source_file, version)
                expected_chunks = sum(
                    len(contextual_chunks(doc, chunk_size=500))
                    for doc in documents
                )
                if expected_chunks == 0:
                    raise ValueError("文件没有可导入的文本内容")

                existing = self._collection.get(
                    where={"source_file": source_file},
                    include=["metadatas"],
                )
                existing_ids = existing.get("ids") or []
                existing_metas = existing.get("metadatas") or []
                unchanged = (
                    len(existing_ids) == expected_chunks
                    and all(
                        meta.get("document_version") == version
                        and meta.get("index_version") == INDEX_VERSION
                        for meta in existing_metas
                    )
                )
                if unchanged:
                    report["skipped_files"] += 1
                    continue

                added_chunks = self.add_documents(documents)
                if added_chunks != expected_chunks:
                    raise RuntimeError(
                        f"预期导入 {expected_chunks} 个片段，实际导入 {added_chunks} 个"
                    )

                # 新版本先 upsert 成功，再清理同一文件的旧片段，避免导入失败时丢失旧知识。
                refreshed = self._collection.get(
                    where={"source_file": source_file},
                    include=["metadatas"],
                )
                stale_ids = [
                    doc_id
                    for doc_id, meta in zip(
                        refreshed.get("ids") or [],
                        refreshed.get("metadatas") or [],
                    )
                    if meta.get("document_version") != version
                    or meta.get("index_version") != INDEX_VERSION
                ]
                if stale_ids:
                    self._collection.delete(ids=stale_ids)

                report["imported_files"] += 1
                report["added_chunks"] += added_chunks
            except Exception as exc:
                report["failed_files"] += 1
                report["errors"].append({"file": source_file, "error": str(exc)})
                logger.exception("自动导入知识文件失败: %s", path)

        logger.info(
            "部署知识自动导入完成: 发现 %s，导入 %s，未变化 %s，失败 %s，新增/更新片段 %s",
            report["discovered_files"],
            report["imported_files"],
            report["skipped_files"],
            report["failed_files"],
            report["added_chunks"],
        )
        return report

    def search(
        self,
        query: str,
        top_k: int = 5,
        categories: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """类别优先混合召回，并为每条查询保留至少 20% 的全库候选。"""
        top_k = max(1, int(top_k))
        categories = [value for value in (categories or []) if value]
        if not categories:
            return self._hybrid_search(query, top_k, None)

        global_quota = max(1, (top_k + 4) // 5)
        category_pool = self._hybrid_search(query, top_k, categories)
        category_items = category_pool[:top_k - global_quota]
        global_pool = self._hybrid_search(query, top_k * 2, None)
        seen = {item["id"] for item in category_items}
        global_items = []
        for item in global_pool:
            if item["id"] not in seen:
                seen.add(item["id"])
                global_items.append(item)
                if len(global_items) >= global_quota:
                    break

        for pool, target in ((category_pool, category_items), (global_pool, global_items)):
            for item in pool:
                if len(category_items) + len(global_items) >= top_k:
                    break
                if item["id"] not in seen:
                    seen.add(item["id"])
                    target.append(item)
        return self._interleave_scopes(category_items, global_items, top_k)

    @staticmethod
    def _interleave_scopes(
        category_items: List[Dict[str, Any]],
        global_items: List[Dict[str, Any]],
        limit: int,
    ) -> List[Dict[str, Any]]:
        """按 4:1 交错两个候选池，任一侧不足时自动由另一侧补齐。"""
        merged, category_index, global_index = [], 0, 0
        while len(merged) < limit:
            size = len(merged)
            for _ in range(4):
                if category_index < len(category_items) and len(merged) < limit:
                    merged.append(category_items[category_index])
                    category_index += 1
            if global_index < len(global_items) and len(merged) < limit:
                merged.append(global_items[global_index])
                global_index += 1
            if len(merged) == size:
                break
        return merged

    def _hybrid_search(
        self,
        query: str,
        top_k: int,
        categories: Optional[List[str]],
    ) -> List[Dict[str, Any]]:
        vector = self._vector_search(query, top_k, categories)
        keyword = self._keyword_search(query, top_k, categories)
        items: Dict[str, Dict[str, Any]] = {}
        scores: Dict[str, float] = {}
        for weight, ranked in ((0.6, vector), (0.4, keyword)):
            for rank, item in enumerate(ranked, start=1):
                doc_id = item["id"]
                stored = items.setdefault(doc_id, dict(item))
                for field in ("score", "keyword_score"):
                    if field in item:
                        stored[field] = item[field]
                scores[doc_id] = scores.get(doc_id, 0.0) + weight / (60 + rank)
        for doc_id, score in scores.items():
            items[doc_id]["retrieval_score"] = round(score, 6)
        ranked = sorted(items.values(), key=lambda item: -item["retrieval_score"])

        # 两个通道各保底约 20% 的独有候选，其余位置按 0.6/0.4 融合分数竞争。
        floor = max(1, top_k // 5)
        vector_ids = {item["id"] for item in vector}
        keyword_ids = {item["id"] for item in keyword}
        channels = (
            [item for item in vector if item["id"] not in keyword_ids] or vector,
            [item for item in keyword if item["id"] not in vector_ids] or keyword,
        )
        selected_ids = set()
        protected_channels = []
        for channel in channels:
            protected = []
            added = 0
            for item in channel:
                if item["id"] in selected_ids:
                    continue
                selected_ids.add(item["id"])
                protected.append(items[item["id"]])
                added += 1
                if added >= floor or len(selected_ids) >= top_k:
                    break
            protected_channels.append(protected)
            if len(selected_ids) >= top_k:
                break
        for item in ranked:
            if len(selected_ids) >= top_k:
                break
            selected_ids.add(item["id"])
        while len(protected_channels) < 2:
            protected_channels.append([])
        protected_ids = {
            item["id"] for channel in protected_channels for item in channel
        }
        remaining = [
            item for item in ranked
            if item["id"] in selected_ids and item["id"] not in protected_ids
        ]
        return self._interleave_channels(remaining, *protected_channels, top_k)

    @staticmethod
    def _interleave_channels(
        ranked: List[Dict[str, Any]],
        vector: List[Dict[str, Any]],
        keyword: List[Dict[str, Any]],
        limit: int,
    ) -> List[Dict[str, Any]]:
        """每轮交错融合候选与两路保底候选，避免保底结果只停留在列表末尾。"""
        merged, ranked_index, vector_index, keyword_index = [], 0, 0, 0
        while len(merged) < limit:
            size = len(merged)
            if ranked_index < len(ranked):
                merged.append(ranked[ranked_index])
                ranked_index += 1
            if vector_index < len(vector) and len(merged) < limit:
                merged.append(vector[vector_index])
                vector_index += 1
            if keyword_index < len(keyword) and len(merged) < limit:
                merged.append(keyword[keyword_index])
                keyword_index += 1
            for _ in range(2):
                if ranked_index < len(ranked) and len(merged) < limit:
                    merged.append(ranked[ranked_index])
                    ranked_index += 1
            if len(merged) == size:
                break
        return merged

    def _vector_search(
        self,
        query: str,
        top_k: int,
        categories: Optional[List[str]],
    ) -> List[Dict[str, Any]]:
        kwargs: Dict[str, Any] = {"query_texts": [query], "n_results": top_k}
        where = self._category_where(categories)
        if where:
            kwargs["where"] = where
        try:
            available = (
                len((self._collection.get(where=where, include=["metadatas"]).get("ids") or []))
                if where else self._collection.count()
            )
            if not available:
                return []
            kwargs["n_results"] = min(top_k, available)
            results = self._collection.query(**kwargs)
        except Exception as exc:
            logger.warning("知识库向量召回失败: %s", exc)
            return []
        if not results.get("documents") or not results["documents"][0]:
            return []
        return [
            self._result_item(doc_id, doc, meta, dist)
            for doc_id, doc, meta, dist in zip(
                results["ids"][0], results["documents"][0],
                results["metadatas"][0], results["distances"][0],
            )
        ]

    def _keyword_search(
        self,
        query: str,
        top_k: int,
        categories: Optional[List[str]],
    ) -> List[Dict[str, Any]]:
        kwargs: Dict[str, Any] = {"include": ["documents", "metadatas"]}
        where = self._category_where(categories)
        if where:
            kwargs["where"] = where
        try:
            stored = self._collection.get(**kwargs)
        except Exception as exc:
            logger.warning("知识库关键词召回失败: %s", exc)
            return []
        ranked = []
        for doc_id, doc, meta in zip(
            stored.get("ids") or [],
            stored.get("documents") or [],
            stored.get("metadatas") or [],
        ):
            score = lexical_score(query, doc, meta)
            if score:
                item = self._result_item(doc_id, doc, meta)
                item["keyword_score"] = score
                ranked.append(item)
        return sorted(ranked, key=lambda item: (-item["keyword_score"], item["id"]))[:top_k]

    @staticmethod
    def _category_where(categories: Optional[List[str]]) -> Optional[Dict[str, Any]]:
        if not categories:
            return None
        return {"category": categories[0]} if len(categories) == 1 else {"category": {"$in": categories}}

    @staticmethod
    def _result_item(
        doc_id: str,
        document: str,
        meta: Dict[str, Any],
        distance: Optional[float] = None,
    ) -> Dict[str, Any]:
        item = {
            "id": doc_id,
            "title": meta.get("title", ""),
            "content": meta.get("raw_content", document),
            "chunk": meta.get("chunk_index", 0),
            **{
                key: meta.get(key, "")
                for key in (
                    "source_url", "category", "published_at", "effective_year",
                    "document_version", "source_file", "source_type", "college",
                    "major", "section", "keywords", "index_version",
                )
            },
        }
        if distance is not None:
            item["score"] = round(1.0 - distance, 4)
        return item

    @property
    def doc_count(self) -> int:
        return self._collection.count()

    # ── MCP 工具 handler ─────────────────────────────────────────────────────

    async def search_handler(self, params: Dict[str, Any], context: Any) -> List[Dict]:
        """
        作为 MCP 工具的 handler 注册。

        MCPToolManager.register(Tool(
            name="knowledge_search",
            handler=kb.search_handler,
            ...
        ))
        """
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        categories = params.get("categories")
        return self.search(query, top_k=top_k, categories=categories if isinstance(categories, list) else None)

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    def _chunk_text(self, text: str, chunk_size: int = 500) -> List[str]:
        return split_text(text, chunk_size)

    def _parse_seed_file(
        self,
        path: Path,
        raw: bytes,
        source_file: str,
        version: str,
    ) -> List[Dict[str, Any]]:
        """将 txt/md/json 统一转换为 add_documents 所需格式并补齐溯源元数据。"""
        text = raw.decode("utf-8-sig", errors="replace")
        if path.suffix.lower() == ".json":
            payload = json.loads(text)
            if isinstance(payload, dict) and isinstance(payload.get("documents"), list):
                payload = payload["documents"]
            elif isinstance(payload, dict):
                payload = [payload]
            if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
                raise ValueError("JSON 应为文档对象、文档数组或包含 documents 数组的对象")
            documents = [dict(item) for item in payload]
        else:
            documents = [{"title": path.stem, "content": text}]

        year_match = re.search(r"20\d{2}", path.name)
        for index, document in enumerate(documents, start=1):
            document.setdefault("title", path.stem if len(documents) == 1 else f"{path.stem}-{index}")
            document.setdefault("category", "major_info")
            document["document_version"] = version
            if year_match:
                document.setdefault("effective_year", int(year_match.group(0)))
            metadata = dict(document.get("metadata") or {})
            metadata.update({
                "source_file": source_file,
                "source_type": "deployment_seed",
            })
            document["metadata"] = metadata
        return documents

    def _load_default_docs(self) -> None:
        """导入可追溯的河北工业大学官方本科招生摘要。"""
        charter_url = "https://zs.hebut.edu.cn/2026-05-29/225.html"
        default_docs = [
            {
                "title": "河北工业大学基本情况与校区",
                "content": (
                    "河北工业大学学校代码为4113010080，是公办全日制普通高校，办学层次包括博士研究生、硕士研究生和本科生。"
                    "学校始建于1903年，是河北省人民政府、天津市人民政府和教育部共建高校、国家“双一流”建设高校。"
                    "学校设天津市北辰校区、红桥校区和河北省廊坊市廊坊校区，学校住所地为天津市北辰区西平道5340号。"
                ),
                "source_url": charter_url,
                "category": "school_info",
                "section": "学校概况与校区",
                "keywords": "学校性质、办学层次、历史、校区数量、校区地址、北辰校区、红桥校区、廊坊校区",
                "published_at": "2026-05-29",
                "effective_year": 2026,
                "document_version": "2026-charter",
            },
            {
                "title": "2026年普通本科招生计划与录取总则",
                "content": (
                    "2026年招生计划按照河北省教育厅核准的年度计划和有关规定编制，招生计划及专业报考要求以各省级招生机构公布为准，学校无预留计划。"
                    "录取遵循公平竞争、公正选拔、公开程序等原则，以全国普通高校招生考试成绩为主要依据，全面衡量、择优录取。"
                    "实行平行志愿的省份或批次按平行志愿政策录取；非平行志愿按学校志愿先后录取。"
                ),
                "source_url": charter_url,
                "category": "admission_policy",
                "section": "招生计划与录取原则",
                "keywords": "招生计划、投档、录取规则、平行志愿",
                "published_at": "2026-05-29",
                "effective_year": 2026,
                "document_version": "2026-charter",
            },
            {
                "title": "2026年专业安排、调剂与高考改革规则",
                "content": (
                    "非高考改革省份对进档考生按分数优先、专业之间不设级差安排专业。所有专业志愿不能满足时，服从专业调剂者调剂到计划未满专业，不服从者予以退档。"
                    "高考综合改革省份按各省公布的改革方案和办法执行，考生须满足所报专业选考科目要求；投档成绩相同时按各省同分排序规则录取。"
                    "政策加分按教育部及考生所在省份招生主管部门的政策执行，安排专业时同样适用。"
                ),
                "source_url": charter_url,
                "category": "admission_policy",
                "section": "专业安排与调剂",
                "keywords": "分数优先、专业级差、专业调剂、服从调剂、退档",
                "published_at": "2026-05-29",
                "effective_year": 2026,
                "document_version": "2026-charter",
            },
            {
                "title": "2026年体检和专业限制",
                "content": (
                    "录取体检执行教育部《普通高等学校招生体检工作指导意见》及补充规定。"
                    "2026年章程列明色弱、色盲以及不能准确识别颜色时受限的专业，具体名单应逐条查阅当年章程。"
                    "英语专业只招英语语种考生；报考使用英语教材教学的相关专业时，非英语语种考生应慎重考虑。"
                ),
                "source_url": charter_url,
                "category": "admission_policy",
                "section": "体检与专业限制",
                "keywords": "体检、色弱、色盲、限报专业、选考科目、英语语种",
                "published_at": "2026-05-29",
                "effective_year": 2026,
                "document_version": "2026-charter",
            },
            {
                "title": "2026年收费、合作办学与学生资助",
                "content": (
                    "普通专业学费以各省公布的招生计划为准，待定标准以河北省物价主管部门批准为准；大类分流后按分流专业标准收费。"
                    "2026年章程载明住宿费按条件分为每生每学年700元、970元和1400元，最终以学校新生报到须知为准。"
                    "学校建立奖、贷、助、补、减资助体系，包括奖学金、助学贷款、绿色通道、勤工助学和困难补助等。"
                    "中外合作项目的培养地点和收费差异较大，必须结合当年章程和招生简章逐项核对。"
                ),
                "source_url": charter_url,
                "category": "tuition",
                "section": "住宿费与学生资助",
                "keywords": "住宿费、宿舍费、收费标准、奖学金、助学金、助学贷款、绿色通道",
                "published_at": "2026-05-29",
                "effective_year": 2026,
                "document_version": "2026-charter",
            },
            {
                "title": "河北工业大学历年招生计划官方查询入口",
                "content": (
                    "本科招生网提供按年份、省份和专业查询历年招生计划的官方入口。"
                    "计划数字属于结构化动态数据，回答具体人数时必须明确年份、省份、科类或选科和专业，并以省级招生机构最终公布为准。"
                ),
                "source_url": "https://zs.hebut.edu.cn/baokao/jihua.html",
                "category": "score_risk",
                "section": "招生计划查询",
                "keywords": "招生计划、招生人数、年份、省份、专业",
                "fetched_at": "2026-07-14",
                "effective_year": 2026,
                "document_version": "official-query-entry",
            },
            {
                "title": "河北工业大学历年录取成绩官方查询入口",
                "content": (
                    "本科招生网提供按年份、省份和专业查询历年录取成绩的官方入口。"
                    "历史分数和位次只能作为参考，不能直接等同于下一年度录取线；进行冲稳保分析前应补齐省份、年份、科类或选科、位次和目标专业。"
                ),
                "source_url": "https://zs.hebut.edu.cn/baokao/fenshu.html",
                "category": "score_risk",
                "section": "录取成绩查询",
                "keywords": "最低分、最低位次、历年录取、冲稳保",
                "fetched_at": "2026-07-14",
                "effective_year": 2026,
                "document_version": "official-query-entry",
            },
            {
                "title": "河北工业大学本科招生官方联系渠道",
                "content": (
                    "河北工业大学本科招生网址为https://zs.hebut.edu.cn/。"
                    "2026年本科招生章程公布的联系电话为022-60438029、022-60438259；招生网公布的电子邮箱为zsb@hebut.edu.cn。"
                    "涉及个案资格、录取结果、材料异常或政策冲突时，应由招生办公室人工确认。"
                ),
                "source_url": "https://zs.hebut.edu.cn/contact/index.html",
                "category": "escalation",
                "section": "本科招生联系渠道",
                "keywords": "招生办、联系电话、电子邮箱、本科招生网、人工确认",
                "fetched_at": "2026-07-14",
                "effective_year": 2026,
                "document_version": "official-contact",
            },
        ]
        for document in default_docs:
            document["source_type"] = "builtin"
        self.add_documents(default_docs)
        titles = {document["title"] for document in default_docs}
        stored = self._collection.get(include=["metadatas"])
        stale_ids = [
            doc_id
            for doc_id, meta in zip(stored.get("ids") or [], stored.get("metadatas") or [])
            if meta.get("title") in titles and meta.get("index_version") != INDEX_VERSION
        ]
        if stale_ids:
            self._collection.delete(ids=stale_ids)
        logger.info(f"已导入河北工业大学官方默认知识: {len(default_docs)} 篇文档")
