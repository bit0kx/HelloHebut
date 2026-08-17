import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


# 这些测试只覆盖画像聚合逻辑，不初始化真实 Redis、ChromaDB 或 Anthropic 客户端。
original_modules = {name: sys.modules.get(name) for name in ("chromadb", "redis", "anthropic")}
chromadb_stub = types.ModuleType("chromadb")
chromadb_stub.Settings = lambda **kwargs: kwargs
chromadb_stub.HttpClient = object
chromadb_stub.PersistentClient = object
redis_stub = types.ModuleType("redis")
redis_stub.from_url = lambda *args, **kwargs: None
anthropic_stub = types.ModuleType("anthropic")
anthropic_stub.AsyncAnthropic = object
sys.modules["chromadb"] = chromadb_stub
sys.modules["redis"] = redis_stub
sys.modules["anthropic"] = anthropic_stub

from memory import conversation_memory
from memory.conversation_memory import MemoryManager, Message, MsgRole

for module_name, original in original_modules.items():
    if original is None:
        sys.modules.pop(module_name, None)
    else:
        sys.modules[module_name] = original


class FakeProfileCollection:
    def __init__(self, records=None):
        self.records = dict(records or {})

    def get(self, ids=None, where=None, limit=None, **kwargs):
        records = list(self.records.items())
        if ids is not None:
            ids = set(ids)
            records = [(doc_id, value) for doc_id, value in records if doc_id in ids]
        if where is not None:
            records = [
                (doc_id, value)
                for doc_id, value in records
                if all(value["metadata"].get(key) == expected for key, expected in where.items())
            ]
        if limit is not None:
            records = records[:limit]
        return {
            "ids": [doc_id for doc_id, _ in records],
            "documents": [value["document"] for _, value in records],
            "metadatas": [value["metadata"] for _, value in records],
        }

    def upsert(self, ids, documents, metadatas):
        for doc_id, document, metadata in zip(ids, documents, metadatas):
            self.records[doc_id] = {"document": document, "metadata": metadata}

    def delete(self, ids):
        for doc_id in ids:
            self.records.pop(doc_id, None)


class ProfileMergeTests(unittest.TestCase):
    def test_merge_profiles_preserves_order_and_deduplicates(self):
        merged = MemoryManager._merge_profiles(
            {
                "preferences": ["关注就业", " 计算机 专业 "],
                "entities": {"省份": ["河北"], "目标专业": "计算机科学与技术"},
            },
            {
                "preferences": ["关注就业", "关注保研"],
                "entities": {"省份": ["河北", "天津"], "选科": ["物理", "化学"]},
            },
        )

        self.assertEqual(merged["preferences"], ["关注就业", "计算机 专业", "关注保研"])
        self.assertEqual(merged["entities"]["省份"], ["河北", "天津"])
        self.assertEqual(merged["entities"]["目标专业"], ["计算机科学与技术"])
        self.assertEqual(merged["entities"]["选科"], ["物理", "化学"])


class ProfilePersistenceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def manager_with(records):
        manager = object.__new__(MemoryManager)
        manager._profile = FakeProfileCollection(records)
        manager._profile_locks = {}
        manager._client = object()
        manager._model = "test-model"
        return manager

    async def test_get_profile_sorts_and_merges_legacy_conversations(self):
        manager = self.manager_with({
            "newer": {
                "document": json.dumps({
                    "preferences": ["关注保研"],
                    "entities": {"目标专业": ["人工智能"]},
                }),
                "metadata": {"user_id": "u1", "ts": "2026-08-18T10:00:00"},
            },
            "older": {
                "document": json.dumps({
                    "preferences": ["关注就业"],
                    "entities": {"省份": ["河北"]},
                }),
                "metadata": {"user_id": "u1", "ts": "2026-08-17T10:00:00"},
            },
        })

        profile = await manager._get_profile("u1")

        self.assertEqual(profile["preferences"], ["关注就业", "关注保研"])
        self.assertEqual(profile["entities"]["省份"], ["河北"])
        self.assertEqual(profile["entities"]["目标专业"], ["人工智能"])

    async def test_update_profile_upserts_canonical_document_and_removes_legacy_records(self):
        manager = self.manager_with({
            "u1_profile_old-conversation": {
                "document": json.dumps({
                    "preferences": ["关注就业"],
                    "entities": {"省份": ["河北"]},
                }),
                "metadata": {"user_id": "u1", "ts": "2026-08-17T10:00:00"},
            },
        })
        manager._get_working_memory = AsyncMock(return_value=[
            Message(role=MsgRole.USER, content="我还关注人工智能专业和保研"),
        ])

        extracted = json.dumps({
            "preferences": ["关注保研"],
            "entities": {"目标专业": ["人工智能"]},
        })
        with (
            patch.object(conversation_memory, "create_message", new=AsyncMock(return_value=object())),
            patch.object(conversation_memory, "extract_text", return_value=extracted),
        ):
            await manager.update_profile("u1", "new-conversation")

        canonical_id = MemoryManager._profile_doc_id("u1")
        self.assertEqual(list(manager._profile.records), [canonical_id])
        stored = manager._profile.records[canonical_id]
        profile = json.loads(stored["document"])
        self.assertEqual(profile["preferences"], ["关注就业", "关注保研"])
        self.assertEqual(profile["entities"]["省份"], ["河北"])
        self.assertEqual(profile["entities"]["目标专业"], ["人工智能"])
        self.assertEqual(stored["metadata"]["conv_id"], "new-conversation")
        self.assertEqual(stored["metadata"]["schema_version"], 2)


if __name__ == "__main__":
    unittest.main()
