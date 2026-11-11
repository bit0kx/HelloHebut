"""Dependency-free BM25 with Chinese character n-grams and immutable snapshot indexes.

Formula/defaults: https://lucene.apache.org/core/9_12_3/core/org/apache/lucene/search/similarities/BM25Similarity.html
This is a small-corpus implementation, not a Lucene/BM25F index.
"""
from collections import Counter, OrderedDict
import math
import re
from threading import RLock
import unicodedata

ANALYZER_VERSION = 'cjk-ngram-v1'
STOP_TERMS = frozenset({'什么', '怎么', '哪些', '哪几', '如何', '是否', '请问', '一下', '几个', '多少', '具体'})


def without_filter_year(query: str, year: int | None) -> str:
    """Remove only the year already enforced by metadata, never an arbitrary number."""
    if year is None:
        return query
    def replace(match):
        if match.group().endswith(('年', '年度')):
            return ' '
        prefix, suffix = query[:match.start()], query[match.end():]
        if (re.search(r'(?:位次|排名|排位|分数)\s*(?:为|是|约)?\s*$', prefix)
                or re.match(r'\s*(?:元|分|名|人)', suffix)):
            return match.group()
        return ' '
    return re.sub(rf'(?<!\d){int(year)}(?:年度|年)?(?!\d)', replace, query)


def tokenize(text: str) -> list[str]:
    text = unicodedata.normalize('NFKC', str(text)).casefold()
    tokens = []
    for block in re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]+', text):
        if re.fullmatch(r'[a-z0-9]+', block):
            tokens.append(block)
        elif len(block) == 1:
            tokens.append(block)
        else:
            # Keep punctuation/Latin boundaries; do not concatenate unrelated words.
            for size in (2, 3):
                tokens.extend(block[i:i+size] for i in range(len(block)-size+1)
                              if block[i:i+size] not in STOP_TERMS)
    return tokens


class BM25Index:
    def __init__(self, stored: dict, k1: float = 1.2, b: float = .75):
        if k1 < 0 or not 0 <= b <= 1:
            raise ValueError('invalid_bm25_parameters')
        self.k1, self.b = k1, b
        self.rows, self.frequencies, self.lengths = [], [], []
        df = Counter()
        for key, document, meta in zip(stored['ids'], stored['documents'], stored['metadatas']):
            meta = dict(meta or {})
            # Raw body appears once. Business fields add context, without copying index labels.
            fields = [meta.get(field, '') for field in ('title', 'section', 'keywords', 'college', 'major')]
            text = '\n'.join(str(value) for value in fields + [meta.get('raw_content', document)])
            frequencies = Counter(tokenize(text))
            self.rows.append((key, document, meta))
            self.frequencies.append(frequencies)
            self.lengths.append(sum(frequencies.values()))
            df.update(frequencies.keys())
        n = len(self.rows)
        self.average_length = sum(self.lengths) / n if n else 0
        self.idf = {term: math.log(1 + (n - count + .5)/(count + .5)) for term, count in df.items()}

    def rank(self, query: str, top_k: int):
        terms = set(tokenize(query))
        if not terms or not self.average_length or top_k < 1:
            return []
        ranked = []
        for row, frequencies, length in zip(self.rows, self.frequencies, self.lengths):
            norm = self.k1 * (1 - self.b + self.b * length / self.average_length)
            score = 0.0
            for term in terms:
                count = frequencies.get(term, 0)
                if count:
                    score += self.idf[term] * count * (self.k1+1) / (count+norm)
            if score > 0:
                ranked.append((score, row))
        # Text/metadata-derived ordering avoids UUID-dependent tie order after re-publication.
        ranked.sort(key=lambda value: (-value[0], str(value[1][2].get('source_file') or value[1][2].get('source_url', '')),
                                       str(value[1][2].get('title', '')), int(value[1][2].get('chunk_index', 0)), value[1][0]))
        return ranked[:top_k]


class BM25Cache:
    def __init__(self, capacity=8):
        self.capacity = capacity
        self._items = OrderedDict()
        self._lock = RLock()

    def get_or_build(self, key, loader):
        with self._lock:
            if key not in self._items:
                self._items[key] = BM25Index(loader())
            self._items.move_to_end(key)
            value = self._items[key]
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)
            return value
