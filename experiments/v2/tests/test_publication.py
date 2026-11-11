import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest

from core.runtime import CURRENT_RUN, RunContext
from tooling.knowledge_base import KnowledgeBase
from tooling.knowledge_catalog import KnowledgeCatalog, PublicationConflict
from tooling.lexical_retrieval import BM25Cache
from tooling.tool_manager import MCPToolManager, Tool
from redis_support import RedisProcess


class Collection:
    """Storage fake only; publication CAS runs against a real isolated Redis."""
    def __init__(self):
        self.rows = {}
        self.fail = False
        self.after_stage = None

    @staticmethod
    def matches(meta, where):
        if not where:
            return True
        if '$and' in where:
            return all(Collection.matches(meta, value) for value in where['$and'])
        return all(meta.get(key) in value['$in'] if isinstance(value, dict) else meta.get(key) == value
                   for key, value in where.items())

    def upsert(self, ids, documents, metadatas):
        for key, document, meta in zip(ids, documents, metadatas):
            self.rows[key] = (document, deepcopy(meta))
            if self.fail:
                raise RuntimeError('partial_embedding_write')
        if self.after_stage:
            self.after_stage()

    def get(self, ids=None, where=None, include=None):
        rows = [(key, value) for key, value in list(self.rows.items())
                if (ids is None or key in ids) and self.matches(value[1], where)]
        return {'ids': [key for key, _ in rows], 'documents': [value[0] for _, value in rows],
                'metadatas': [deepcopy(value[1]) for _, value in rows]}

    def count(self):
        return len(self.rows)

    def query(self, query_texts, n_results, where=None):
        values = self.get(where=where)
        return {**{key: [value[:n_results]] for key, value in values.items()},
                'distances': [[0.1] * min(n_results, len(values['ids']))]}


def document(content, **kwargs):
    return {'title': '测试政策', 'content': content, 'source_file': 'fixture.txt',
            'effective_year': 2026, **kwargs}


class PublicationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = RedisProcess()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def setUp(self):
        self.kb = KnowledgeBase.__new__(KnowledgeBase)
        self.kb._collection = Collection()
        self.kb._catalog = KnowledgeCatalog(self.server.url, 'publication-test')
        self.kb._catalog.client.flushdb()
        self.kb._bm25_cache = BM25Cache()
        self.kb._lexical_mode, self.kb._fusion_mode = 'bm25', 'balanced'

    def tearDown(self):
        self.kb._catalog.close()

    def test_shorter_revision_hides_old_chunks_and_can_rollback(self):
        self.kb.add_documents([document('旧内容。' * 500)])
        old = self.kb.cache_epoch()
        old_count = self.kb.doc_count
        self.kb.add_documents([document('新内容。')])
        self.assertEqual(self.kb.doc_count, 1)
        self.assertGreater(self.kb._collection.count(), 1)
        self.assertEqual({m['raw_content'] for m in self.kb._active_get()['metadatas']}, {'新内容。'})
        self.assertEqual(self.kb.rollback(int(old)), 3)
        self.assertEqual(self.kb.doc_count, old_count)

    def test_partial_stage_is_never_visible(self):
        self.kb.add_documents([document('旧政策')])
        before = self.kb._catalog.read()
        self.kb._collection.fail = True
        with self.assertRaises(RuntimeError):
            self.kb.add_documents([document('新政策。' * 500)])
        self.assertEqual(self.kb._catalog.read(), before)
        self.assertEqual(self.kb.search('政策')[0]['content'], '旧政策')

    def test_staged_revision_is_hidden_until_cas(self):
        self.kb.add_documents([document('旧政策')])
        observed = []
        self.kb._collection.after_stage = lambda: observed.extend(self.kb.search('政策'))
        self.kb.add_documents([document('新政策')])
        self.assertEqual({r['content'] for r in observed}, {'旧政策'})
        self.assertEqual(self.kb.search('政策')[0]['content'], '新政策')

    def test_concurrent_publish_has_one_winner(self):
        self.kb.add_documents([document('旧政策')])
        barrier = threading.Barrier(2)
        self.kb._collection.after_stage = lambda: barrier.wait(timeout=3)
        with ThreadPoolExecutor(2) as pool:
            jobs = [pool.submit(self.kb.add_documents, [document(value)]) for value in ['版本甲', '版本乙']]
            results = []
            for job in jobs:
                try:
                    results.append(job.result())
                except PublicationConflict:
                    results.append('conflict')
        self.assertEqual(results.count('conflict'), 1)
        self.assertEqual(self.kb.cache_epoch(), '2')
        self.assertEqual(self.kb.doc_count, 1)

    def test_same_url_different_years_remain_distinct(self):
        self.kb.add_documents([document('旧年', effective_year=2025), document('新年')])
        self.assertEqual(len(self.kb._catalog.read()['documents']), 2)
        self.assertEqual([row['content'] for row in self.kb.search('年', year=2025)], ['旧年'])

    def test_duplicate_import_and_catalog_restart(self):
        self.kb.add_documents([document('政策')])
        self.kb.add_documents([document('政策')])
        self.assertEqual(self.kb.cache_epoch(), '1')
        other = KnowledgeCatalog(self.server.url, 'publication-test')
        try:
            self.assertEqual(other.read(), self.kb._catalog.read())
        finally:
            other.close()

    def test_request_pins_snapshot_until_next_run(self):
        self.kb.add_documents([document('旧政策')])
        token = CURRENT_RUN.set(RunContext())
        try:
            self.assertEqual(self.kb.cache_epoch(), '1')
            self.kb.add_documents([document('新政策')])
            self.assertEqual(self.kb.search('政策')[0]['content'], '旧政策')
            self.assertEqual(self.kb.cache_epoch(), '1')
        finally:
            CURRENT_RUN.reset(token)
        self.assertEqual(self.kb.search('政策')[0]['content'], '新政策')

    def test_publication_during_search_cannot_poison_new_generation_index(self):
        self.kb.add_documents([document('旧校区资料')])
        original = self.kb._vector_search
        def publish_during_vector(*args, **kwargs):
            self.kb.add_documents([document('新校区资料')])
            return []
        self.kb._vector_search = publish_during_vector
        self.assertEqual(self.kb.search('校区', year=2026)[0]['content'], '旧校区资料')
        self.kb._vector_search = original
        self.assertEqual(self.kb._keyword_search('校区', 8, None, year=2026)[0]['content'], '新校区资料')

    def test_bm25_cache_isolates_year_category_and_rollback(self):
        self.kb.add_documents([document('旧年校区', effective_year=2025, category='policy'),
                               document('本年校区', category='policy'),
                               document('专业校区', title='专业', category='major')])
        def lookup(year, category):
            return {row['content'] for row in self.kb._keyword_search('校区', 8, [category], year=year)}
        self.assertEqual(lookup(2025, 'policy'), {'旧年校区'})
        self.assertEqual(lookup(2026, 'policy'), {'本年校区'})
        self.assertEqual(lookup(2026, 'major'), {'专业校区'})
        self.assertEqual(lookup(2024, 'major'), set())
        old = int(self.kb.cache_epoch())
        self.kb.add_documents([document('更新校区', category='policy')])
        self.assertEqual(lookup(2026, 'policy'), {'更新校区'})
        self.kb.rollback(old)
        self.assertEqual(lookup(2026, 'policy'), {'本年校区'})

    def test_tool_cache_scope_changes_with_retrieval_strategy(self):
        self.kb.add_documents([document('校区')])
        before = self.kb.retrieval_cache_scope()
        self.kb._fusion_mode = 'legacy'
        self.assertNotEqual(before, self.kb.retrieval_cache_scope())

    def test_seed_file_replacement_removes_deleted_documents(self):
        import json
        root = Path(__file__).resolve().parents[1] / 'tmp'
        with tempfile.TemporaryDirectory(dir=root) as folder:
            source = Path(folder) / 'policy.json'
            source.write_text(json.dumps([document('甲', title='甲'), document('乙', title='乙')]), encoding='utf-8')
            self.assertEqual(self.kb.import_seed_directory(folder)['failed_files'], 0)
            source.write_text(json.dumps([document('甲', title='甲')]), encoding='utf-8')
            self.assertEqual(self.kb.import_seed_directory(folder)['failed_files'], 0)
            self.assertEqual(self.kb.doc_count, 1)

    async def test_another_worker_cannot_reuse_old_cache(self):
        self.kb.add_documents([document('旧政策')])
        worker = MCPToolManager('test-key')
        worker.register(Tool('lookup', 'test', self.kb.search_handler, {'type': 'object'},
                             cache_ttl=300, cache_scope=self.kb.retrieval_cache_scope))
        try:
            first = await worker.call('lookup', {'query': '政策'})
            self.kb.add_documents([document('新政策')])  # No worker.invalidate_cache call.
            second = await worker.call('lookup', {'query': '政策'})
            self.assertFalse(second.cached)
            self.assertEqual(first.data[0]['content'], '旧政策')
            self.assertEqual(second.data[0]['content'], '新政策')
        finally:
            await worker._client.close()
