"""Real MySQL/Redis checks in disposable tables; no live preference rows touched."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


async def inside():
    from memory.fact_store import FactStore, MemoryConflict, MemoryNotFound
    schema = 'test_memory_' + uuid.uuid4().hex
    store = FactStore(os.getenv('V2_FACT_DATABASE_URL', ''), schema)
    await store.initialize()
    checks = []

    async def check(name, operation):
        try:
            await operation()
            checks.append({'check': name, 'passed': True})
        except Exception as exc:
            checks.append({'check': name, 'passed': False, 'error_type': type(exc).__name__})
            import traceback
            traceback.print_exc(file=sys.stderr)

    async def save(user, value, operation='save', key='interests'):
        revision = (await store.snapshot(user))['revision']
        await store.mutate(user, 'save', operation, revision, key=key, value=value)

    async def candidate():
        await store.capture('candidate', 'c', 'r', 'run', '我喜欢计算机', 0)
        snapshot = await store.snapshot('candidate')
        assert len(snapshot['items']) == 1
        row = snapshot['items'][0]
        assert row['status'] == 'pending' and row['verification_status'] == 'source_checked'
        assert row['source_message_id'] == 'r' and row['source_run_id'] == 'run'
        await store.mutate('candidate', 'confirm', 'confirm', snapshot['revision'], memory_id=row['memory_id'])
        confirmed = (await store.snapshot('candidate'))['items'][0]
        assert confirmed['status'] == 'active' and confirmed['verification_status'] == 'user_confirmed'

    async def correction():
        await save('correction', '计算机')
        await save('correction', '电气', 'correct')
        rows = (await store.snapshot('correction', history=True))['items']
        assert len(rows) == 2 and len({r['version'] for r in rows}) == 2
        assert {r['value'] for r in rows if r['status'] == 'active'} == {'电气'}
        assert {r['value'] for r in rows if r['status'] == 'superseded'} == {'计算机'}

    async def concurrent():
        jobs = [store.mutate('race', 'save', value, 0, key='interests', value=value) for value in ['机器人', '绘画']]
        results = await asyncio.gather(*jobs, return_exceptions=True)
        assert sum(isinstance(r, MemoryConflict) for r in results) == 1
        assert sum(r is None for r in results) == 1
        assert len((await store.snapshot('race'))['items']) == 1

    async def idempotent():
        await store.mutate('retry', 'save', 'same', 0, key='interests', value='机器人')
        await store.mutate('retry', 'save', 'same', 0, key='interests', value='机器人')
        assert len((await store.snapshot('retry', history=True))['items']) == 1
        try:
            await store.mutate('retry', 'save', 'same', 0, key='interests', value='电气')
        except MemoryConflict:
            pass
        else:
            raise AssertionError('changed payload reused operation id')

    async def deletion():
        await store.capture('delete', 'old', 'old', 'run', '我喜欢计算机', 0)
        await store.mutate('delete', 'save', 'manual', 1, key='major_preference', value='电气')
        revision = (await store.snapshot('delete'))['revision']
        await store.mutate('delete', 'clear', 'clear', revision)
        await store.capture('delete', 'old', 'old', 'run', '我喜欢计算机', 0)
        await store.capture('delete', 'inflight', 'inflight', 'run', '我喜欢机器人', revision)
        await store.mutate('delete', 'save', 'manual', 1, key='major_preference', value='电气')
        assert not (await store.snapshot('delete', history=True))['items']

    async def expiry():
        await save('expiry', '机器人')
        async with store.connection() as conn:
            await conn.execute(f"UPDATE {store.table('memory_records')} SET valid_until=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE owner_id=%s", (store.owner('expiry'),))
        assert not (await store.snapshot('expiry'))['items']
        assert (await store.snapshot('expiry', history=True))['items'][0]['status'] == 'expired'
        await save('expiry', '绘画', 'new')
        assert [r['value'] for r in (await store.snapshot('expiry'))['items']] == ['绘画']

    async def rollback():
        await save('rollback', '原偏好')
        snapshot = await store.snapshot('rollback')
        original = store._insert
        async def fail(*args):
            raise RuntimeError('injected_between_supersede_and_insert')
        store._insert = fail
        try:
            try:
                await store.mutate('rollback', 'save', 'fault', snapshot['revision'], key='interests', value='错误更新')
            except RuntimeError:
                pass
        finally:
            store._insert = original
        assert await store.snapshot('rollback') == snapshot

    async def reconnect():
        await save('reconnect', '机器人')
        other = FactStore(store.dsn, schema)
        await other.initialize()
        assert (await other.snapshot('reconnect'))['items'][0]['value'] == '机器人'

    async def scope():
        await store.capture('alice', 'c', 'r', 'run', '我喜欢计算机', 0)
        row = (await store.snapshot('alice'))['items'][0]
        assert not (await store.snapshot('bob'))['items']
        try:
            await store.mutate('bob', 'confirm', 'forged', 0, memory_id=row['memory_id'])
        except MemoryNotFound:
            pass
        else:
            raise AssertionError('cross-owner confirmation accepted')

    async def sequence():
        # 30 turns, two conversations, correction/delete, score-like statements never persist.
        for i in range(30):
            revision = (await store.snapshot('sequence'))['revision']
            await store.capture('sequence', 'first' if i < 15 else 'second', str(i), 'run', '我喜欢位次12000', revision)
            if i == 4:
                await save('sequence', '机器人', 'first')
            elif i == 14:
                await save('sequence', '绘画', 'correct')
            elif i == 24:
                await store.mutate('sequence', 'clear', 'clear', (await store.snapshot('sequence'))['revision'])
            expected = [] if i < 4 or i >= 24 else ['机器人'] if i < 14 else ['绘画']
            assert [r['value'] for r in (await store.snapshot('sequence'))['items']] == expected

    async def http_flow():
        import httpx
        import redis
        from types import SimpleNamespace
        from unittest.mock import patch
        import api.main as api
        from agents.agent_orchestrator import AgentType
        from core.chat_service import ChatService
        from memory.conversation_memory import MemoryManager
        from memory.mysql_turn_store import MySQLTurnStore
        memory = MemoryManager.__new__(MemoryManager)
        memory.facts, memory.verified_memory_mode = store, True
        memory.maintenance = None
        memory.turns = MySQLTurnStore(os.environ['REDIS_URL'], database=store)
        await memory.turns.initialize()
        memory._redis = redis.from_url(os.environ['REDIS_URL'], decode_responses=True)
        requests, sessions, conversations = [], [], []
        async def answer(request):
            requests.append(request)
            return SimpleNamespace(response='fixture response', intent=None, agent_type=AgentType.GENERAL, escalated=False)
        service = ChatService(SimpleNamespace(run=answer), memory, None)
        try:
            with patch.object(api, '_memory', memory), patch.object(api, '_chat_service', service):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test') as client:
                    assert (await client.get('/memories')).status_code == 401
                    for _ in range(2):
                        sessions.append((await client.post('/session')).json())
                    session, outsider = sessions
                    headers = {'Authorization': 'Bearer ' + session['session_token']}
                    other_headers = {'Authorization': 'Bearer ' + outsider['session_token']}
                    async def chat(text, request_id, conv=None):
                        response = await client.post('/chat', headers=headers, json={'message': text, 'client_request_id': request_id, 'conv_id': conv})
                        assert response.status_code == 200
                        result = response.json()
                        conversations.append((session['user_id'], result['conv_id']))
                        return result
                    source = await chat('我偏好的专业是计算机科学与技术', 'declaration')
                    listing = (await client.get('/memories', headers=headers)).json()
                    row = listing['items'][0]
                    assert row['status'] == 'pending' and row['source_message_id'] == 'declaration'
                    before = await chat('我的专业偏好是什么？', 'before')
                    assert '没有已确认' in before['response']
                    assert (await client.post('/memories', headers=headers, json={'key': 'interests', 'value': '机器人',
                        'operation_id': 'fake', 'expected_revision': listing['revision'], 'user_id': outsider['user_id']})).status_code == 422
                    assert (await client.post('/memories/'+row['memory_id']+'/confirm', headers=other_headers,
                        json={'operation_id': 'forged', 'expected_revision': 0})).status_code == 404
                    response = await client.post('/memories/'+row['memory_id']+'/confirm', headers=headers,
                        json={'operation_id': 'confirm', 'expected_revision': listing['revision']})
                    assert response.status_code == 200
                    confirmed = await chat('我的专业偏好是什么？', 'new-conversation')
                    assert confirmed['verified'] and '计算机科学与技术' in confirmed['response']
                    other = await chat('帮朋友问专业选择', 'other-person')
                    await chat('再推荐一下', 'followup', other['conv_id'])
                    assert all('计算机科学与技术' not in r.context for r in requests)
                    current = response.json()
                    response = await client.post('/memories', headers=headers, json={'key': 'major_preference', 'value': '电气工程',
                        'operation_id': 'edit', 'expected_revision': current['revision'], 'valid_days': 30})
                    assert response.status_code == 200
                    corrected = await chat('我的专业偏好是什么？', 'corrected')
                    assert '电气工程' in corrected['response'] and '计算机科学与技术' not in corrected['response']
                    response = await client.request('DELETE', '/memories/major_preference', headers=headers,
                        json={'operation_id': 'delete', 'expected_revision': response.json()['revision']})
                    assert response.status_code == 200 and not response.json()['items']
                    replay = await chat('我偏好的专业是计算机科学与技术', 'declaration')
                    assert replay == source
                    assert not (await client.get('/memories?history=true', headers=headers)).json()['items']
                    after = await chat('我的专业偏好是什么？', 'after-delete')
                    assert '没有已确认' in after['response']
        finally:
            for owner, conv in set(conversations):
                await memory.turns.clear(owner, conv)
            for session in sessions:
                await memory.turns.client.delete('v2:session:' + hashlib.sha256(session['session_token'].encode()).hexdigest())
            await memory.turns.drop_test_tables()
            await memory.turns.close()
            memory._redis.close()

    try:
        for name, operation in [('source_and_confirmation', candidate), ('versioned_correction', correction),
            ('concurrent_cas_one_winner', concurrent), ('idempotency_conflict', idempotent),
            ('delete_and_old_replay_no_resurrection', deletion), ('expiry_excluded', expiry),
            ('transaction_failure_rolls_back', rollback), ('reconnect_preserves_facts', reconnect),
            ('owner_isolation', scope), ('thirty_turn_sequence', sequence), ('http_chat_confirmation_and_delete', http_flow)]:
            await check(name, operation)
        async with store.connection() as conn:
            version = (await (await conn.execute('SELECT VERSION() AS version')).fetchone())['version']
        return {'mode': 'real_mysql_redis_isolated_tables', 'mysql': version,
                'checks': checks, 'passed': sum(r['passed'] for r in checks), 'total': len(checks),
                'limitations': 'Functional assertions and synthetic 30-turn sequence, not human-labelled memory precision. '
                               'Reconnect tested; full process restart is checked separately.'}
    finally:
        assert schema.startswith('test_memory_') and len(schema) == 44
        await store.drop_test_tables()


if __name__ == '__main__':
    if '--inside' in sys.argv:
        print(json.dumps(asyncio.run(inside()), ensure_ascii=True))
    else:
        root = Path(__file__).resolve().parents[3]
        output = subprocess.check_output(['docker', 'compose', 'exec', '-T', 'hellohebut', 'python', '-c',
                                         Path(__file__).read_text(encoding='utf-8'), '--inside'], cwd=root,
                                         text=True, encoding='utf-8', timeout=180)
        report = json.loads(output.strip().splitlines()[-1])
        report['source_sha256'] = {str(p.relative_to(root).as_posix()): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for folder in ('memory', 'core', 'api') for p in sorted((root/folder).rglob('*.py'))}
        report['migration_sha256'] = hashlib.sha256((root/'memory/migrations/001_facts.sql').read_bytes()).hexdigest()
        (root/'experiments/v2/reports/stage5_memory.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(json.dumps({k: report[k] for k in ('passed', 'total', 'mysql')}))
        raise SystemExit(0 if report['passed'] == report['total'] else 1)
