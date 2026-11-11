"""Actual Chroma/Redis publication fault checks and a small labeled retrieval development set."""
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import uuid


def inside(reference):
    import chromadb
    from core.runtime import CURRENT_RUN, RunContext
    from mcp.knowledge_base import KnowledgeBase
    from mcp.knowledge_catalog import KnowledgeCatalog
    client = chromadb.HttpClient(host='chromadb', port=8000,
                                settings=chromadb.Settings(anonymized_telemetry=False))
    name = 'v2_acceptance_' + uuid.uuid4().hex
    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb._collection = client.create_collection(name)
    kb._catalog = KnowledgeCatalog(os.environ['REDIS_URL'], name)
    checks = []

    def check(label, passed):
        checks.append({'check': label, 'passed': bool(passed)})

    def doc(text):
        return {'title': '合成测试资料', 'content': text, 'source_file': 'synthetic_fixture.txt', 'effective_year': 2026}

    try:
        kb.add_documents([doc('旧版本：测试住宿费700元。' * 50)])
        first = kb._catalog.read()
        token = CURRENT_RUN.set(RunContext())
        try:
            kb.cache_epoch()
            kb.add_documents([doc('新版本：测试住宿费970元。')])
            pinned = kb.search('测试住宿费', year=2026)
            check('inflight_read_keeps_old_snapshot', pinned and all('970元' not in r['content'] for r in pinned))
        finally:
            CURRENT_RUN.reset(token)
        fresh = kb.search('测试住宿费', year=2026)
        check('new_request_sees_only_shorter_new_revision', len(fresh) == 1 and '970元' in fresh[0]['content'])
        real = kb._collection

        class InterruptedWrite:
            def __getattr__(self, field):
                return getattr(real, field)

            def upsert(self, ids, documents, metadatas):
                real.upsert(ids=ids[:1], documents=documents[:1], metadatas=metadatas[:1])
                raise RuntimeError('injected_mid_stage_failure')

        kb._collection = InterruptedWrite()
        try:
            kb.add_documents([doc('尚未发布的新版本1400元。' * 50)])
        except RuntimeError:
            pass
        finally:
            kb._collection = real
        check('interrupted_real_embedding_write_stays_invisible', kb.search('住宿费')[0]['content'] == fresh[0]['content'])
        kb.rollback(first['generation'])
        check('rollback_restores_old_revision', all('970元' not in r['content'] for r in kb.search('住宿费')))
    finally:
        assert name.startswith('v2_acceptance_')
        client.delete_collection(name)
        kb._catalog.client.delete(kb._catalog.key, kb._catalog.history_key)
        kb._catalog.close()

    # Read-only against actual project documents; never publish fixture data into the application collection.
    kb = KnowledgeBase.__new__(KnowledgeBase)
    collection = os.getenv('V2_KNOWLEDGE_COLLECTION', 'hello_hebut_v2')
    kb._collection = client.get_collection(collection)
    kb._catalog = KnowledgeCatalog(os.environ['REDIS_URL'], collection)
    rows = []
    token = CURRENT_RUN.set(RunContext())
    try:
        for case in reference['cases']:
            hits = kb.search(case['query'], top_k=8, year=case['year'])
            expected, seen, dcg = set(case['expected_titles']), set(), 0.0
            for rank, hit in enumerate(hits, 1):
                title = hit['title']
                if title in expected and title not in seen:
                    dcg += 1 / math.log2(rank + 1)
                seen.add(title)
            row = {'id': case['id'], 'returned': len(hits), 'titles': list(dict.fromkeys(h['title'] for h in hits))}
            if expected:
                row.update(recall_at_8=len(expected & seen)/len(expected),
                           ndcg_at_8=dcg/sum(1/math.log2(i+2) for i in range(min(8,len(expected)))))
            else:
                row['empty_as_expected'] = not hits
            rows.append(row)
        publication = kb.publication_status()
    finally:
        CURRENT_RUN.reset(token)
        kb._catalog.close()
    positive = [r for r in rows if 'recall_at_8' in r]
    return {'storage_checks': checks, 'storage_passed': sum(r['passed'] for r in checks),
            'storage_total': len(checks), 'benchmark_id': reference['benchmark_id'], 'annotation': reference['annotation'],
            'retrieval': rows, 'recall_at_8': sum(r['recall_at_8'] for r in positive)/len(positive),
            'ndcg_at_8': sum(r['ndcg_at_8'] for r in positive)/len(positive), 'publication': publication,
            'limitations': 'Small source-derived development set, without reranking; not an independent quality benchmark.'}


if __name__ == '__main__':
    if '--inside' in sys.argv:
        print(json.dumps(inside(json.load(sys.stdin)), ensure_ascii=True))
    else:
        root = Path(__file__).resolve().parents[3]
        dataset = (root / 'experiments/v2/datasets/retrieval_reference.json').read_text(encoding='utf-8')
        output = subprocess.check_output(['docker', 'compose', 'exec', '-T', 'hellohebut', 'python', '-c',
                                          Path(__file__).read_text(encoding='utf-8'), '--inside'],
                                         cwd=root, input=dataset, text=True, encoding='utf-8', timeout=100)
        report = json.loads(output.strip().splitlines()[-1])
        report['dataset_sha256'] = hashlib.sha256(dataset.encode()).hexdigest()
        (root / 'experiments/v2/reports/stage2_storage_retrieval.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(json.dumps({k: report[k] for k in ('storage_passed','storage_total','recall_at_8','ndcg_at_8')}))
        raise SystemExit(0 if report['storage_passed']==report['storage_total'] else 1)
