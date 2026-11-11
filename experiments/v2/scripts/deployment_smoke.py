"""Opt-in HTTP/container acceptance; uses real Chroma, Redis and configured model."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid
import httpx

ROOT = Path(__file__).resolve().parents[3]

VECTOR_CHECK = '''
import json, os, hashlib, importlib.metadata as m
from pathlib import Path
import chromadb
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
client = chromadb.HttpClient(host='chromadb', port=8000,
                            settings=chromadb.Settings(anonymized_telemetry=False))
collection = client.get_collection(os.getenv('V2_KNOWLEDGE_COLLECTION', 'hello_hebut_v2'))
vectors = DefaultEmbeddingFunction()(['admission tuition'])
result = collection.query(query_texts=['admission tuition'], n_results=3)
print(json.dumps({'onnxruntime': m.version('onnxruntime'), 'chroma': m.version('chromadb'),
                  'dimensions': len(vectors[0]), 'documents': collection.count(),
                  'vector_results': len(result['ids'][0]),
                  'retrieval_config': {'lexical': os.getenv('RAG_LEXICAL_MODE', 'filtered_legacy'),
                                       'fusion': os.getenv('RAG_FUSION_MODE', 'balanced')},
                  'runtime_source_sha256': {name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
                      for name in ('api/main.py', 'tooling/knowledge_base.py', 'tooling/lexical_retrieval.py')}}))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--api', default='http://localhost:18000')
    parser.add_argument('--web', default='http://localhost:8088')
    parser.add_argument('--build-mode', choices=['standard', 'reused_local_base'], required=True)
    parser.add_argument('--report', default='experiments/v2/reports/stage3_deployment.json')
    args = parser.parse_args()
    rows = []
    last_chat = {}

    def check(name, operation):
        started = time.monotonic()
        last_chat.clear()
        try:
            details = operation() or {}
            rows.append({'check': name, 'passed': True, **details})
        except Exception as exc:
            rows.append({'check': name, 'passed': False, 'error_type': type(exc).__name__})
            if last_chat:
                rows[-1]['chat_diagnostic'] = {key: last_chat.get(key) for key in
                    ('trace_id', 'status', 'verified', 'stop_reason', 'verification_method')}
        rows[-1]['duration_s'] = round(time.monotonic() - started, 3)
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

    with httpx.Client(timeout=65) as client:
        def public_http():
            for url in [args.api + '/health', args.api + '/docs', args.web,
                        args.web + '/api/python/health']:
                assert client.get(url).status_code == 200
            assert client.post(args.api + '/chat', json={'message': '你好'}).status_code == 401
        check('health_frontend_proxy_and_auth', public_http)

        def vector_search():
            output = subprocess.check_output(['docker', 'compose', 'exec', '-T', 'hellohebut',
                                               'python', '-c', VECTOR_CHECK], cwd=ROOT, text=True, timeout=45)
            details = json.loads(output.strip().splitlines()[-1])
            assert details['dimensions'] == 384 and details['documents'] > 0 and details['vector_results'] == 3
            assert all(hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest
                       for name, digest in details['runtime_source_sha256'].items()), 'runtime_source_mismatch'
            return details
        check('actual_onnx_embedding_and_chroma_query', vector_search)

        session = client.post(args.api + '/session')
        session.raise_for_status()
        headers = {'Authorization': 'Bearer ' + session.json()['session_token']}
        conversation = {}

        def chat(question, **extra):
            payload = {'message': question, 'client_request_id': uuid.uuid4().hex, **extra}
            result = client.post(args.api + '/chat', headers=headers, json=payload)
            result.raise_for_status()
            data = result.json()
            last_chat.update(data)
            assert data['response'] and data['trace_id']
            return payload, data

        def admission_and_replay():
            payload, data = chat('河北物理类位次12000，计算机科学与技术录取风险如何？')
            assert data['admission_data_used'] and data['citations']
            assert data['status'] == 'completed' and data['verified'] is True
            assert data['verification_method'] == 'deterministic_csv'
            assert all(str(value) in data['response'] for value in [608, 607, 621, 12287, 12449, 13587])
            replay = client.post(args.api + '/chat', headers=headers, json=payload)
            assert replay.status_code == 200 and replay.json() == data
            conflict = client.post(args.api + '/chat', headers=headers, json={**payload, 'message': '另一条消息'})
            assert conflict.status_code == 409
            conversation['id'] = data['conv_id']
            return {'status': data['status'], 'verified': data['verified'],
                    'citation_count': len(data['citations']), 'idempotent_replay': True}
        check('live_csv_chat_replay_and_conflict', admission_and_replay)

        def scope_change():
            assert conversation.get('id'), 'initial conversation unavailable'
            _, data = chat('换天津考生，计算机呢？', conv_id=conversation['id'])
            assert data['status'] == 'clarification' and not data['entities'].get('rank')
            return {'status': data['status'], 'old_rank_invalidated': True}
        check('live_candidate_scope_change', scope_change)

        def policy_answer():
            _, data = chat('2026年河北工业大学住宿费有哪几个收费标准？')
            assert data['knowledge_used'] and data['citations']
            assert data['status'] == 'completed' and data['verified'] is True
            assert all(str(value) in data['response'] for value in [700, 970, 1400])
            return {'status': data['status'], 'verified': data['verified'],
                    'citation_count': len(data['citations'])}
        check('live_rag_verified_accommodation_answer', policy_answer)

        def campus_answer():
            _, data = chat('2026年河北工业大学有哪些校区？')
            assert data['knowledge_used'] and data['citations']
            assert data['status'] == 'completed' and data['verified'] is True
            assert all(name in data['response'] for name in ['北辰', '红桥', '廊坊'])
            return {'status': data['status'], 'verified': data['verified'],
                    'citation_count': len(data['citations']), 'all_three_campuses': True}
        check('live_rag_verified_campus_answer', campus_answer)

        def missing_tuition_rates():
            _, data = chat('河北工业大学普通本科专业学费标准是多少？')
            assert data['status'] == 'insufficient_evidence' and data['verified'] is False
            return {'status': data['status'], 'verified': data['verified'], 'stop_reason': data['stop_reason']}
        check('live_rag_missing_tuition_rates_abstain', missing_tuition_rates)

    report = {'mode': 'live_container_http_smoke', 'build_mode': args.build_mode,
              'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'source_hashes': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                for folder in ('core', 'tooling', 'memory', 'rag', 'api')
                                for p in sorted((ROOT / folder).glob('*.py'))},
              'checks': rows, 'passed': sum(row['passed'] for row in rows), 'total': len(rows),
              'limitations': 'Small functional smoke; no statistical answer-quality claim. '
                             'Local-base reuse does not validate a clean standard Dockerfile build.'}
    path = ROOT / args.report
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if report['passed'] == report['total'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
