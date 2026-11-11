"""Lazy, read-only adapters. Constructing this backend never seeds or creates collections."""
import asyncio
import os
from pathlib import Path
import threading


class PublicDataBackend:
    def __init__(self):
        self._knowledge = None
        self._admission = None
        self._lock = threading.Lock()

    def _kb(self):
        with self._lock:
            if self._knowledge is None:
                import chromadb
                from tooling.knowledge_base import KnowledgeBase
                from tooling.knowledge_catalog import KnowledgeCatalog
                from tooling.lexical_retrieval import BM25Cache
                collection = os.getenv("V2_KNOWLEDGE_COLLECTION", "hello_hebut_v2")
                kb = KnowledgeBase.__new__(KnowledgeBase)
                kb._client = chromadb.HttpClient(
                    host=os.getenv("CHROMA_HOST", "localhost"),
                    port=int(os.getenv("CHROMA_PORT", "8001")),
                    settings=chromadb.Settings(anonymized_telemetry=False))
                # No get_or_create, seed import, publication write or memory access.
                kb._collection = kb._client.get_collection(collection)
                kb._catalog = KnowledgeCatalog(os.getenv("REDIS_URL", "redis://localhost:6379/0"), collection)
                kb._bm25_cache = BM25Cache()
                kb._lexical_mode = os.getenv("RAG_LEXICAL_MODE", "filtered_legacy")
                kb._fusion_mode = os.getenv("RAG_FUSION_MODE", "balanced")
                kb._use_server = True
                self._knowledge = kb
            return self._knowledge

    def _data(self):
        with self._lock:
            if self._admission is None:
                from tooling.admission_data import AdmissionDataStore
                default = Path(__file__).resolve().parents[1] / "data" / "hebut_admission.csv"
                self._admission = AdmissionDataStore(os.getenv("ADMISSION_DATA_PATH", str(default)))
            return self._admission

    def _dispatch(self, name, params):
        if name == "knowledge_search":
            return self._kb().search(params["query"], top_k=params.get("top_k", 5),
                                     categories=params.get("categories"), year=params.get("year"))
        if name == "admission_data_query":
            return self._data().query(params)
        if name == "data_version_stats":
            return {"knowledge": self._kb().publication_status(), "admission": self._data().stats}
        raise ValueError("tool_not_allowed")

    async def call(self, name, params):
        return await asyncio.to_thread(self._dispatch, name, params)

    def close(self):
        if self._knowledge is not None:
            self._knowledge._catalog.close()
