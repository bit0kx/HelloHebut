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
import json, os, importlib.metadata as m
import chromadb
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
client = chromadb.HttpClient(host='chromadb', port=8000,
                            settings=chromadb.Settings(anonymized_telemetry=False))
collection = client.get_collection(os.getenv('V2_KNOWLEDGE_COLLECTION', 'hello_hebut_v2'))
vectors = DefaultEmbeddingFunction()(['admission tuition'])
result = collection.query(query_texts=['admission tuition'], n_results=3)
print(json.dumps({'onnxruntime': m.version('onnxruntime'), 'chroma': m.version('chromadb'),
                  'dimensions': len(vectors[0]), 'documents': collection.count(),
                  'vector_results': len(result['ids'][0])}))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--api', default='http://localhost:18000')
    parser.add_argument('--web', default='http://localhost:8088')
    parser.add_argument('--build-mode', choices=['standard', 'reused_local_base'], required=True)
    args = parser.parse_args()
    rows = []

    def check(name, operation):
        started = time.monotonic()
        try:
            details = operation() or {}
            rows.append({'check': name, 'passed': True, **details})
        except Exception as exc:
            rows.append({'check': name, 'passed': False, 'error_type': type(exc).__name__})
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
            assert data['response'] and data['trace_id']
            return payload, data

        def admission_and_replay():
            payload, data = chat('河北物理类位次12000，计算机科学与技术录取风险如何？')
            assert data['admission_data_used'] and data['citations']
            assert data['status'] in ('completed', 'insufficient_evidence')
            if data['status'] == 'completed':
                assert data['verified'] is True
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
            _, data = chat('河北工业大学普通本科专业学费标准是多少？')
            assert data['knowledge_used'] and data['citations']
            assert data['status'] == 'completed' and data['verified'] is True
            return {'status': data['status'], 'verified': data['verified'],
                    'citation_count': len(data['citations'])}
        check('live_rag_verified_tuition_answer', policy_answer)

    report = {'mode': 'live_container_http_smoke', 'build_mode': args.build_mode,
              'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'source_hashes': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                for folder in ('core', 'mcp', 'memory', 'rag', 'api')
                                for p in sorted((ROOT / folder).glob('*.py'))},
              'checks': rows, 'passed': sum(row['passed'] for row in rows), 'total': len(rows),
              'limitations': 'Small functional smoke; no statistical answer-quality claim. '
                             'Local-base reuse does not validate a clean standard Dockerfile build.'}
    path = ROOT / 'experiments/v2/reports/deployment_smoke.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if report['passed'] == report['total'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
