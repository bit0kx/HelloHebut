import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tooling.knowledge_indexing import contextual_chunks, lexical_score
from tooling.knowledge_base import KnowledgeBase
from tooling.tool_manager import MCPToolManager, ToolResult


class KnowledgeIndexingTests(unittest.TestCase):
    def test_contextual_chunks_include_college_major_section(self):
        chunks = contextual_chunks({
            "title": "河北工业大学2026年本科招生简章之人工智能与数据科学学院",
            "content": """专业简介

1

计算机科学与技术

计算机科学与技术专业成立于1985年。

专业核心课程：

数据结构、操作系统、计算机网络。""",
        })

        course = next(item for item in chunks if "数据结构" in item["content"])
        self.assertEqual(course["college"], "人工智能与数据科学学院")
        self.assertEqual(course["major"], "计算机科学与技术")
        self.assertEqual(course["section"], "核心课程")
        self.assertIn("学院：人工智能与数据科学学院", course["index_text"])
        self.assertIn("专业：计算机科学与技术", course["index_text"])
        self.assertNotIn("学院：", course["content"])

    def test_explicit_context_is_used_for_structured_document(self):
        chunk = contextual_chunks({
            "title": "专业说明",
            "college": "人工智能与数据科学学院",
            "major": "人工智能",
            "section": "培养目标",
            "category": "major_info",
            "keywords": "人工智能、培养目标",
            "content": "培养人工智能领域工程技术人才。",
        })[0]

        self.assertIn("专业：人工智能", chunk["index_text"])
        self.assertIn("类别：major_info", chunk["index_text"])
        self.assertIn("关键词：人工智能、培养目标", chunk["index_text"])
        self.assertEqual(chunk["content"], "培养人工智能领域工程技术人才。")

    def test_chinese_ordinal_section_does_not_replace_major(self):
        chunks = contextual_chunks({
            "title": "河北工业大学2026年本科招生简章之建筑与艺术设计学院",
            "content": "专业介绍五\n\n艺术与科技\n\n学生可从事数字交互与用户体验设计。",
        })

        self.assertEqual(chunks[0]["major"], "艺术与科技")

    def test_numbered_body_and_decimal_heading_are_not_majors(self):
        chunks = contextual_chunks({
            "title": "河北工业大学2026年本科招生简章之能源与环境工程学院",
            "content": "5.就职国内外知名企业、政府和事业单位。\n\n1.1能源动力方向\n\n方向课程与实践介绍。",
        })

        self.assertTrue(all(not item["major"] for item in chunks))


    def test_keyword_score_rescues_fee_document(self):
        query = "2026年住宿费和奖助政策是什么"
        fee_score = lexical_score(
            query,
            "住宿费为每生每学年700元、970元和1400元。",
            {"section": "住宿费与学生资助", "keywords": "住宿费、奖学金、助学金"},
        )
        unrelated_score = lexical_score(query, "数字交互与用户体验设计。", {"section": "就业方向"})

        self.assertGreater(fee_score, 0)
        self.assertEqual(unrelated_score, 0)


class KnowledgeBaseScopeTests(unittest.TestCase):
    def setUp(self):
        self.knowledge_base = KnowledgeBase.__new__(KnowledgeBase)

    def test_category_search_reserves_twenty_percent_global_candidates(self):
        def search(_query, top_k, categories):
            if categories:
                return [{"id": f"c{index}"} for index in range(top_k)]
            return [
                *({"id": f"c{index}"} for index in range(8)),
                *({"id": f"g{index}"} for index in range(top_k - 8)),
            ]

        self.knowledge_base._hybrid_search = Mock(side_effect=search)
        results = self.knowledge_base.search("住宿费", top_k=10, categories=["tuition"])

        self.assertEqual([item["id"] for item in results], [
            "c0", "c1", "c2", "c3", "g0", "c4", "c5", "c6", "c7", "g1",
        ])
        self.assertEqual(self.knowledge_base._hybrid_search.call_args_list[1].args, ("住宿费", 20, None))

    def test_missing_category_candidates_are_filled_from_global_pool(self):
        self.knowledge_base._hybrid_search = Mock(side_effect=lambda _q, top_k, categories: (
            [{"id": "c0"}] if categories else [{"id": f"g{index}"} for index in range(top_k)]
        ))

        results = self.knowledge_base.search("校园生活", top_k=10, categories=["campus_life"])

        self.assertEqual(len(results), 10)
        self.assertEqual(sum(item["id"].startswith("g") for item in results), 9)

    def test_search_without_category_uses_only_global_pool(self):
        self.knowledge_base._hybrid_search = Mock(
            return_value=[{"id": f"g{index}"} for index in range(10)]
        )

        results = self.knowledge_base.search("其他问题", top_k=10)

        self.assertEqual(len(results), 10)
        self.knowledge_base._hybrid_search.assert_called_once_with("其他问题", 10, None)

    def test_keyword_floor_candidates_are_promoted_into_early_results(self):
        self.knowledge_base._vector_search = Mock(return_value=[
            {"id": f"v{index}", "content": "向量候选"} for index in range(10)
        ])
        self.knowledge_base._keyword_search = Mock(return_value=[
            {"id": f"k{index}", "content": "关键词候选", "keyword_score": 10 - index}
            for index in range(10)
        ])

        results = self.knowledge_base._hybrid_search("住宿费", 10, None)

        self.assertIn("k0", [item["id"] for item in results[:5]])
        self.assertEqual(sum(item["id"].startswith("k") for item in results), 2)


class RetrievalPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = MCPToolManager(
            api_key="test-key",
            base_url="https://api.deepseek.com/anthropic",
            rerank_min_score=5.0,
        )

    def test_stable_key_ignores_query_dependent_score(self):
        base = {
            "title": "人工智能与数据科学学院",
            "content": "计算机科学与技术专业介绍",
            "source_file": "college.txt",
            "document_version": "v1",
            "chunk": 3,
        }
        self.assertEqual(
            self.manager._retrieval_key({**base, "score": -0.3}),
            self.manager._retrieval_key({**base, "score": 0.8}),
        )

    def test_stable_key_prefers_document_id(self):
        content = {"content": "相同正文", "document_version": ""}
        self.assertNotEqual(
            self.manager._retrieval_key({**content, "id": "source-a", "source_url": "https://a.example"}),
            self.manager._retrieval_key({**content, "id": "source-b", "source_url": "https://b.example"}),
        )

    async def test_rewrite_query_stays_inside_tool_scope(self):
        response = SimpleNamespace(content=[])
        create = AsyncMock(return_value=response)
        with (
            patch("tooling.tool_manager.create_message", create),
            patch("tooling.tool_manager.extract_text", return_value='["河北工业大学住宿费标准"]'),
        ):
            queries = await self.manager.rewrite_query(
                "住宿费", scope="搜索河北工业大学官方本科招生知识库",
            )

        prompt = create.await_args.kwargs["messages"][0]["content"]
        self.assertIn("搜索河北工业大学官方本科招生知识库", prompt)
        self.assertIn("不得扩展到无关业务", prompt)
        self.assertEqual(queries[0], "住宿费")

    async def test_search_deduplicates_before_rerank(self):
        duplicate = {
            "title": "人工智能与数据科学学院",
            "content": "计算机科学与技术专业介绍",
            "source_file": "college.txt",
            "document_version": "v1",
            "chunk": 3,
        }
        responses = [
            ToolResult(True, [{**duplicate, "score": -0.3}], "knowledge_search"),
            ToolResult(True, [{**duplicate, "score": 0.8}], "knowledge_search"),
        ]
        self.manager.rewrite_query = AsyncMock(return_value=["计算机", "计算机专业"])
        self.manager.call = AsyncMock(side_effect=responses)
        self.manager._rerank = AsyncMock(side_effect=lambda _q, items, _k: items)

        result = await self.manager.search_with_rewrite("knowledge_search", "计算机", top_k=5)

        self.assertEqual(len(result.data), 1)
        self.assertEqual(len(self.manager._rerank.await_args.args[1]), 1)

    async def test_search_passes_category_and_expands_recall(self):
        self.manager.rewrite_query = AsyncMock(return_value=["住宿费"])
        self.manager.call = AsyncMock(return_value=ToolResult(
            True,
            [{"id": "fee", "content": "住宿费分档"}],
            "knowledge_search",
        ))
        self.manager._rerank = AsyncMock(side_effect=lambda _q, items, _k: items)

        await self.manager.search_with_rewrite(
            "knowledge_search", "住宿费", top_k=3, categories=["tuition"],
        )

        params = self.manager.call.await_args.args[1]
        self.assertEqual(params["top_k"], 30)
        self.assertEqual(params["categories"], ["tuition"])

    async def test_rewrite_results_are_merged_round_robin(self):
        self.manager.rewrite_query = AsyncMock(return_value=["q0", "q1", "q2", "q3"])
        self.manager.call = AsyncMock(side_effect=[
            ToolResult(
                True,
                [{"id": f"q{query_no}-{rank}", "content": "候选"} for rank in range(30)],
                "knowledge_search",
            )
            for query_no in range(4)
        ])
        self.manager._rerank = AsyncMock(side_effect=lambda _q, items, _k: items[:3])

        await self.manager.search_with_rewrite("knowledge_search", "问题", top_k=3)

        candidates = self.manager._rerank.await_args.args[1]
        self.assertEqual([item["id"] for item in candidates[:4]], ["q0-0", "q1-0", "q2-0", "q3-0"])
        self.assertEqual(len(candidates), 20)

    async def test_rerank_filters_low_scores_and_can_return_empty(self):
        response = SimpleNamespace(content=[])
        with (
            patch("tooling.tool_manager.create_message", AsyncMock(return_value=response)),
            patch("tooling.tool_manager.extract_text", return_value='[{"index":0,"score":4.9}]'),
        ):
            result = await self.manager._rerank("计算机", [{"content": "数字交互设计"}], 5)

        self.assertEqual(result, [])

    async def test_rerank_keeps_relevant_results_with_score(self):
        response = SimpleNamespace(content=[])
        with (
            patch("tooling.tool_manager.create_message", AsyncMock(return_value=response)),
            patch(
                "tooling.tool_manager.extract_text",
                return_value='[{"index":0,"score":3.0},{"index":1,"score":9.2}]',
            ),
        ):
            result = await self.manager._rerank(
                "计算机",
                [{"content": "艺术设计"}, {"content": "计算机科学与技术"}],
                5,
            )

        self.assertEqual([item["content"] for item in result], ["计算机科学与技术"])
        self.assertEqual(result[0]["rerank_score"], 9.2)

    async def test_rerank_prompt_uses_selected_fields_not_raw_json(self):
        response = SimpleNamespace(content=[])
        create = AsyncMock(return_value=response)
        with (
            patch("tooling.tool_manager.create_message", create),
            patch("tooling.tool_manager.extract_text", return_value='[{"index":0,"score":9.0}]'),
        ):
            await self.manager._rerank(
                "住宿费",
                [{
                    "id": "hidden-id",
                    "title": "收费与资助",
                    "category": "tuition",
                    "section": "住宿费",
                    "effective_year": 2026,
                    "content": "住宿费为700元、970元和1400元。",
                    "retrieval_score": 0.01,
                }],
                3,
            )

        prompt = create.await_args.kwargs["messages"][0]["content"]
        self.assertIn("标题：收费与资助", prompt)
        self.assertIn("正文：住宿费为700元、970元和1400元。", prompt)
        self.assertNotIn("hidden-id", prompt)
        self.assertNotIn("retrieval_score", prompt)


if __name__ == "__main__":
    unittest.main()
