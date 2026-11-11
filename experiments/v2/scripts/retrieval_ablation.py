"""Compare frozen retrieval variants on one live, read-only Chroma publication snapshot."""
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import types

VARIANTS = ('vector_only', 'legacy_lexical_only', 'legacy_hybrid', 'filtered_legacy_hybrid', 'filtered_legacy_balanced_hybrid',
            'bm25_only', 'bm25_hybrid', 'bm25_balanced_hybrid')


def targets(case):
    return case.get('targets', [{'title': title} for title in case.get('expected_titles', [])])


def matches(hit, target):
    return (all(hit.get(key) == target[key] for key in ('title', 'source_file') if key in target)
            and all(term in hit.get('content', '') for term in target.get('contains_all', [])))


def score(case, hits):
    expected = targets(case)
    row = {'case_id': case['id'], 'returned': len(hits),
           'hits': [{'chunk_id': h['id'], 'title': h['title'], 'source_file': h.get('source_file', ''),
                     'chunk': h.get('chunk'), 'keyword_score': h.get('keyword_score'),
                     'content_hash': hashlib.sha256(h['content'].encode()).hexdigest()} for h in hits]}
    if not expected:
        return {**row, 'empty_as_expected': not hits}
    seen, dcg, first = set(), 0.0, None
    for rank, hit in enumerate(hits, 1):
        found = {i for i, target in enumerate(expected) if matches(hit, target)}
        if found - seen:
            dcg += 1/math.log2(rank+1)
            first = first or rank
        seen.update(found)
    return {**row, 'recall_at_8': len(seen)/len(expected),
            'ndcg_at_8': dcg/sum(1/math.log2(i+2) for i in range(min(8, len(expected)))),
            'mrr_at_8': 1/first if first else 0.0, 'first_relevant_rank': first}


def inside(payload):
    # Load the exact candidate source supplied by the host; no image rebuild needed for an ablation.
    for name, source in payload['sources'].items():
        module = types.ModuleType(name)
        module.__file__ = 'candidate:' + name
        sys.modules[name] = module
        exec(compile(source, module.__file__, 'exec'), module.__dict__)
    import chromadb
    from core.runtime import CURRENT_RUN, RunContext
    from mcp.knowledge_base import KnowledgeBase
    from mcp.knowledge_catalog import KnowledgeCatalog
    from mcp.lexical_retrieval import ANALYZER_VERSION, BM25Cache
    kb = KnowledgeBase.__new__(KnowledgeBase)
    collection = os.getenv('V2_KNOWLEDGE_COLLECTION', 'hello_hebut_v2')
    kb._collection = chromadb.HttpClient(host='chromadb', port=8000,
        settings=chromadb.Settings(anonymized_telemetry=False)).get_collection(collection)
    kb._catalog = KnowledgeCatalog(os.environ['REDIS_URL'], collection)
    kb._bm25_cache = BM25Cache()
    token = CURRENT_RUN.set(RunContext(timeout_s=240))
    try:
        corpus = kb._active_get(include=['documents', 'metadatas'])
        manifest = kb._snapshot()
        benchmarks = []
        original_vector = kb._vector_search
        for dataset in payload['datasets']:
            # Fail invalid labels rather than reporting fabricated zero/positive quality scores.
            source_hits = [kb._result_item(i, d, m) for i, d, m in zip(corpus['ids'], corpus['documents'], corpus['metadatas'])]
            for case in dataset['cases']:
                for target in targets(case):
                    assert any(matches(hit, target) for hit in source_hits), f"unmatched_reference:{case['id']}"
            results = {name: [] for name in VARIANTS}
            for case in dataset['cases']:
                started = time.monotonic()
                vector = original_vector(case['query'], 8, None, year=case.get('year'))
                vector_ms = (time.monotonic()-started)*1000
                # Every hybrid consumes exactly the same vector candidates on this query/snapshot.
                kb._vector_search = lambda *a, **kw: vector
                for name in VARIANTS:
                    kb._lexical_mode = 'bm25' if name.startswith('bm25') else 'filtered_legacy' if name.startswith('filtered') else 'legacy'
                    kb._fusion_mode = 'balanced' if name.endswith('_balanced_hybrid') else 'legacy'
                    started = time.monotonic()
                    if name == 'vector_only':
                        hits = vector
                    elif name.endswith('_only'):
                        hits = kb._keyword_search(case['query'], 8, None, year=case.get('year'))
                    else:
                        hits = kb._hybrid_search(case['query'], 8, None, year=case.get('year'))
                    row = score(case, hits)
                    row.update(vector_ms_shared=round(vector_ms, 2),
                               component_ms=round((time.monotonic()-started)*1000, 2))
                    results[name].append(row)
            metrics = {}
            for name, rows in results.items():
                positive = [r for r in rows if 'recall_at_8' in r]
                negative = [r for r in rows if 'empty_as_expected' in r]
                metrics[name] = {metric: round(sum(r[metric] for r in positive)/len(positive), 6)
                                 for metric in ('recall_at_8', 'ndcg_at_8', 'mrr_at_8')}
                metrics[name].update(answerable=len(positive), missing_year=len(negative),
                    missing_year_empty=sum(r['empty_as_expected'] for r in negative))
            benchmarks.append({'benchmark_id': dataset['benchmark_id'], 'annotation': dataset['annotation'],
                               'metrics': metrics, 'cases': results})
        return {'benchmarks': benchmarks, 'corpus': corpus, 'generation': manifest['generation'],
                'python': platform.python_version(), 'analyzer': ANALYZER_VERSION, 'bm25_k1': 1.2, 'bm25_b': .75,
                'rrf_weights': {'legacy': {'vector': .6, 'lexical': .4}, 'balanced': {'vector': .5, 'lexical': .5}},
                'vector_model': 'all-MiniLM-L6-v2',
                'embedding_comparison': {'completed': False, 'note': 'BGE-M3 not run; the same existing vectors are used in all hybrids.'}}
    finally:
        CURRENT_RUN.reset(token)
        kb._catalog.close()


if __name__ == '__main__':
    if '--inside' in sys.argv:
        print(json.dumps(inside(json.load(sys.stdin)), ensure_ascii=True))
    else:
        root = Path(__file__).resolve().parents[3]
        files = ['retrieval_reference.json', 'retrieval_holdout.json']
        payload = {'datasets': [json.loads((root/'experiments/v2/datasets'/f).read_text(encoding='utf-8')) for f in files],
                   'sources': {name: (root/relative).read_text(encoding='utf-8') for name, relative in
                               [('mcp.lexical_retrieval', 'mcp/lexical_retrieval.py'), ('mcp.knowledge_base', 'mcp/knowledge_base.py')]}}
        output = subprocess.check_output(['docker', 'compose', 'exec', '-T', 'hellohebut', 'python', '-c',
            Path(__file__).read_text(encoding='utf-8'), '--inside'], cwd=root, input=json.dumps(payload),
            text=True, encoding='utf-8', timeout=240)
        report = json.loads(output.strip().splitlines()[-1])
        corpus = json.dumps(report.pop('corpus'), sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(corpus.encode()).hexdigest()
        folder = root/'experiments/v2/tmp'
        folder.mkdir(parents=True, exist_ok=True)
        (folder/f'retrieval_corpus_{digest[:12]}.json').write_text(corpus, encoding='utf-8')
        report.update(corpus_sha256=digest,
            baseline_commit='0a1b991', dataset_sha256={f: hashlib.sha256((root/'experiments/v2/datasets'/f).read_bytes()).hexdigest() for f in files},
            candidate_source_sha256={k: hashlib.sha256(v.encode()).hexdigest() for k,v in payload['sources'].items()},
            limitations='Small source-derived development/validation sets, not human-reviewed. Validation failures informed the fusion fix; not an untouched holdout. Fixed BM25 defaults; no reranker. '
                        'Component timings exclude shared vector work and mix cold/warm indexes, not end-to-end latency estimates.')
        path=root/'experiments/v2/reports/stage3_retrieval_ablation.json'
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n',encoding='utf-8')
        print(json.dumps([{'benchmark': b['benchmark_id'], 'metrics': b['metrics']} for b in report['benchmarks']]))
