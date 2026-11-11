"""Live deployed owner-bound tasks/SSE, a deterministic CSV turn and state correction."""
import hashlib
import json
from pathlib import Path
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[3]

def main():
    rows = []
    with httpx.Client(base_url='http://localhost:8088/api/python', timeout=100) as client:
        client.get('/ready').raise_for_status()
        session = client.post('/session').json()
        headers = {'Authorization': 'Bearer ' + session['session_token']}
        conv = 'smoke-' + uuid.uuid4().hex
        try:
            body = {'message':'河北物理类位次12000，计算机科学与技术录取风险如何？',
                    'conv_id':conv, 'client_request_id':uuid.uuid4().hex}
            task = client.post('/chat/tasks', json=body, headers=headers)
            task.raise_for_status(); run_id=task.json()['run_id']
            repeated = client.post('/chat/tasks', json=body, headers=headers)
            assert repeated.json()['run_id'] == run_id
            assert client.get('/chat/tasks/'+run_id).status_code==401
            other=client.post('/session').json()
            assert client.get('/chat/tasks/'+run_id,headers={'Authorization':'Bearer '+other['session_token']}).status_code==404
            rows.append({'check':'durable_admission_idempotency_and_owner_isolation','passed':True})
            events = []
            with client.stream('GET', '/chat/tasks/'+run_id+'/events', headers=headers) as stream:
                stream.raise_for_status()
                for line in stream.iter_lines():
                    if line.startswith('data: '):
                        events.append(json.loads(line[6:]))
            final = [event for event in events if event.get('type')=='final']
            assert len(final)==1 and final[0]['status']=='completed', final
            assert all('result' not in event for event in events if event.get('type')!='final')
            result=final[0]['result']
            assert result['verified'] and result['admission_data_used']
            state=client.get('/conversations/'+conv+'/state',headers=headers).json()
            assert state['entities']['province'] == ['河北']
            assert client.get('/chat/tasks/'+run_id,headers=headers).json()['result']['response']==result['response']
            cursor=final[0]['sequence']
            assert 'data:' not in client.get('/chat/tasks/'+run_id+'/events',headers={**headers,'Last-Event-ID':str(cursor)}).text
            rows.append({'check':'nginx_sse_progress_final_and_reconnect','passed':True,'stages':[e.get('stage') for e in events if e.get('stage')],
                         'verified':result['verified'],'state_available':bool(state)})
        finally:
            response=client.delete('/conversations/'+conv,headers=headers)
            response.raise_for_status()
    report={'mode':'live_http_through_nginx','checks':rows,'passed':len(rows),'total':2,
            'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ('api/task_routes.py','memory/job_store.py')},
            'limitations':'Two functional checks, not a statistical latency or answer-quality benchmark.'}
    (ROOT/'experiments/v2/reports/stage6_task_http.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':len(rows),'total':2}))

if __name__=='__main__':
    main()
