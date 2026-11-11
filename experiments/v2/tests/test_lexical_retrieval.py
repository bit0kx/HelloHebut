import math
import unittest

from experiments.v2.scripts.retrieval_ablation import matches, score
from tooling.knowledge_base import KnowledgeBase
from tooling.lexical_retrieval import BM25Cache, BM25Index, tokenize, without_filter_year


def corpus(*documents):
    return {'ids': [str(i) for i in range(len(documents))], 'documents': list(documents),
            'metadatas': [{} for _ in documents]}


def hit(key, **extra):
    return {'id': key, 'title': key, 'content': key, **extra}


class LexicalTests(unittest.TestCase):
    def test_bm25_matches_hand_calculation(self):
        # N=2, df(rare)=1, tf=2, lengths 3 and 1, average length=2.
        index = BM25Index(corpus('rare rare common', 'common'))
        expected = math.log(2) * 2 * 2.2 / (2 + 1.2 * (.25 + .75 * 3/2))
        ranked = index.rank('rare', 8)
        self.assertEqual(len(ranked), 1)
        self.assertAlmostEqual(ranked[0][0], expected)
        self.assertEqual(ranked[0][1][0], '0')

    def test_length_normalization_and_term_saturation(self):
        index = BM25Index(corpus('campus', 'campus ' + 'other ' * 30))
        self.assertEqual(index.rank('campus', 2)[0][1][0], '0')
        saturated = BM25Index(corpus('campus', 'campus ' * 100), b=0)
        scores = {row[0]: value for value, row in saturated.rank('campus', 2)}
        self.assertGreater(scores['1'], scores['0'])
        self.assertLess(scores['1'] / scores['0'], 2.2)

    def test_chinese_boundaries_and_unicode_normalization(self):
        values = tokenize('北辰校区，红桥校区 ＡＩ ２０２６')
        self.assertTrue({'北辰', '红桥', '校区', 'ai', '2026'} <= set(values))
        self.assertNotIn('区红', values)
        self.assertNotIn('哪些', tokenize('有哪些校区'))

    def test_filter_year_preserves_non_year_numbers(self):
        self.assertNotIn('2026', without_filter_year('2026年学校有哪些校区', 2026))
        self.assertNotIn('2026', without_filter_year('2026年人数与分数线', 2026))
        self.assertEqual(without_filter_year('2026年学费', None), '2026年学费')
        self.assertIn('2025', without_filter_year('2025年与2026年度', 2026))
        for query in ['位次2026', '排名为2026', '学费2026元', '分数是2026', '人数2026人', '位次120260']:
            with self.subTest(query=query):
                self.assertEqual(without_filter_year(query, 2026), query)

    def test_empty_or_unmatched_index_has_no_positive_hits(self):
        self.assertEqual(BM25Index(corpus()).rank('学校', 8), [])
        index = BM25Index(corpus('北辰校区'))
        self.assertEqual(index.rank('学费', 8), [])
        self.assertEqual(index.rank('', 8), [])

    def test_raw_body_is_not_double_counted(self):
        data = corpus('title: irrelevant raw_content: campus')
        data['metadatas'][0] = {'raw_content': 'campus'}
        self.assertEqual(BM25Index(data).lengths, [1])

    def test_cache_reuse_and_bounded_eviction(self):
        cache, calls = BM25Cache(capacity=2), []
        def loader():
            calls.append(1)
            return corpus('校区')
        first = cache.get_or_build('a', loader)
        cache.get_or_build('b', loader)
        self.assertIs(first, cache.get_or_build('a', loader))
        cache.get_or_build('c', loader)  # b is now least recently used.
        cache.get_or_build('b', loader)
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(cache._items), 2)

    def test_balanced_fusion_preserves_disjoint_channel_recall(self):
        vector = [hit(f'v{i}') for i in range(8)]
        lexical = [hit(f'l{i}') for i in range(8)]
        result = KnowledgeBase._balanced_fusion(vector, lexical, 8)
        self.assertEqual(len(result), 8)
        self.assertEqual({r['id'] for r in result}, {f'{c}{i}' for c in 'vl' for i in range(4)})
        self.assertEqual(result[0]['id'], 'l0')

    def test_shared_evidence_is_boosted_without_duplicate_inflation(self):
        vector = [hit('vector'), hit('shared', score=.7)]
        lexical = [hit('lexical'), hit('shared', keyword_score=3)]
        result = KnowledgeBase._balanced_fusion(vector, lexical, 8)
        duplicated = KnowledgeBase._balanced_fusion(vector + [vector[1]], lexical, 8)
        self.assertEqual(result, duplicated)
        self.assertEqual(result[0]['id'], 'shared')
        self.assertEqual(result[0]['score'], .7)
        self.assertEqual(result[0]['keyword_score'], 3)

    def test_benchmark_requires_matching_source_and_evidence(self):
        target = {'source_file': 'source.txt', 'contains_all': ['五年', '学士']}
        wrong_chunk = hit('a', source_file='source.txt', content='仅学士')
        correct = hit('b', source_file='source.txt', content='五年学制授予学士')
        self.assertFalse(matches(wrong_chunk, target))
        self.assertFalse(matches({**correct, 'source_file': 'other.txt'}, target))
        row = score({'id': 'case', 'targets': [target]}, [wrong_chunk, correct, correct])
        self.assertEqual(row['recall_at_8'], 1)
        self.assertAlmostEqual(row['ndcg_at_8'], 1 / math.log2(3))
        self.assertEqual(row['mrr_at_8'], .5)


if __name__ == '__main__':
    unittest.main()
