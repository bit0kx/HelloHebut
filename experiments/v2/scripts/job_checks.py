"""Isolated real-MySQL job/outbox checks and in-process authenticated SSE fixtures."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch
import uuid


async def inside():
    from fastapi import FastAPI, Header, HTTPException
    import httpx
    from pydantic import BaseModel, ConfigDict
    from api.task_routes import build_task_router
    from core.contracts import TaskState
    from core.runtime import LeaseLost, PROGRESS_SINK, RequestConflict
    from memory.job_store import DurableWorkers, JobStore, QueueFull
    from memory.mysql_database import MySQLDatabase
    from memory.mysql_turn_store import MySQLTurnStore

    namespace = 'test_jobs_' + uuid.uuid4().hex
    db = MySQLDatabase(os.getenv('V2_FACT_DATABASE_URL', ''), namespace)
    store = JobStore(db)
    turns = MySQLTurnStore(os.environ['REDIS_URL'], database=db)
    await store.initialize()
    await turns.initialize()
    checks = []

    async def check(name, fn):
        # All rows are this script's own isolated namespace. No production jobs are claimed.
        async with db.connection() as conn:
            await conn.execute(f'DELETE FROM {store.table}')
        try:
            details = await fn() or {}
            checks.append({'check': name, 'passed': True, **details})
        except Exception as exc:
            import traceback
            traceback.print_exc(file=sys.stderr)
            checks.append({'check': name, 'passed': False, 'error_type': type(exc).__name__})

    async def enqueue_contract():
        first = await store.enqueue('alice', 'chat', 'r', {'message': 'fixture'})
        again = await store.enqueue('alice', 'chat', 'r', {'message': 'fixture'})
        assert first == again and first['status'] == 'queued'
        assert await store.get('bob', first['run_id']) is None
        try:
            await store.enqueue('alice', 'chat', 'r', {'message': 'changed'})
        except RequestConflict:
            pass
        else:
            raise AssertionError('changed idempotency payload accepted')
        other = await store.enqueue('bob', 'chat', 'r', {'message': 'fixture'})
        assert other['run_id'] != first['run_id']
        small = JobStore(db, capacity=2)
        assert await small.enqueue('alice', 'chat', 'r', {'message': 'fixture'}) == first
        try:
            await small.enqueue('alice', 'chat', 'third', {})
        except QueueFull:
            pass
        else:
            raise AssertionError('capacity exceeded')

    async def expire(job):
        async with db.connection() as conn:
            await conn.execute(f'UPDATE {store.table} SET lease_until=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND '
                               'WHERE job_id=%s', (job['id'],))

    async def make_available(job_id):
        async with db.connection() as conn:
            await conn.execute(f'UPDATE {store.table} SET available_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND '
                               'WHERE job_id=%s', (job_id,))

    async def lease_fencing():
        submitted = await store.enqueue('alice', 'chat', 'lease', {})
        first = await store.claim()
        await expire(first)
        second = await JobStore(db).claim()
        assert second['id'] == first['id'] and second['token'] != first['token'] and second['attempts'] == 2
        for action in (lambda: store.finish(first, {'leaked': 'old worker'}),
                       lambda: store.event(first, 'answer'), lambda: store.renew(first)):
            try:
                await action()
            except LeaseLost:
                pass
            else:
                raise AssertionError('stale job token accepted')
        await store.finish(second, {'response': 'current worker'})
        assert (await store.get('alice', submitted['run_id']))['result'] == {'response': 'current worker'}

    async def worker_restart():
        started = asyncio.Event()
        async def interrupted(job):
            started.set()
            await asyncio.Event().wait()
        item = await store.enqueue('alice', 'chat', 'restart', {})
        old = DurableWorkers(store, {'chat': interrupted}, workers=1)
        old.start()
        try:
            await asyncio.wait_for(started.wait(), 5)
        finally:
            await old.close()
        current = await store.get('alice', item['run_id'])
        assert current['status'] == 'running' and current['attempts'] == 1
        await expire({'id': item['run_id']})
        async def resumed(job):
            return {'response': 'resumed fixture'}
        fresh = DurableWorkers(JobStore(db), {'chat': resumed}, workers=1)
        fresh.start()
        try:
            for _ in range(100):
                current = await store.get('alice', item['run_id'])
                if current['status'] == 'completed':
                    break
                await asyncio.sleep(.05)
            assert current['status'] == 'completed' and current['attempts'] == 2
            assert current['result']['response'] == 'resumed fixture'
        finally:
            await fresh.close()

    async def bounded_retries():
        attempts = []
        async def broken(job):
            attempts.append(job['attempts'])
            raise RuntimeError('fixture failure must not be exported')
        item = await store.enqueue('alice', 'chat', 'retries', {})
        worker = DurableWorkers(store, {'chat': broken}, workers=1)
        for attempt in range(1, 4):
            await make_available(item['run_id'])
            job = await store.claim()
            assert job['attempts'] == attempt
            await worker._execute(job)
            state = await store.get('alice', item['run_id'])
            assert state['status'] == ('failed' if attempt == 3 else 'queued')
        assert attempts == [1, 2, 3] and await store.claim() is None
        assert state['error_type'] == 'RuntimeError' and state['result'] is None
        assert 'fixture failure' not in json.dumps(state)

    async def transactional_outbox():
        owner, conv, request, message = 'outbox-owner', 'conv', 'r', 'fixture message'
        result, state = {'response': 'fixture committed answer'}, TaskState(revision=1)
        turns.maintenance_outbox = store
        async with turns.guard(owner, conv) as token:
            await turns.begin_request(owner, conv, request, message, token)
            await turns.save_checkpoint(owner, conv, request, message, result, state, token)
            async def fail_after_outbox(*args):
                raise RuntimeError('fault_between_outbox_and_receipt')
            with patch.object(turns, '_mark_committed', fail_after_outbox):
                try:
                    await turns.commit(owner, conv, request, message, result, state, token)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError('fault did not interrupt commit')
            assert await turns.receipt(owner, conv, request, message) is None
            assert (await turns.working_snapshot(owner, conv))['messages'] == []
            async with db.connection() as conn:
                row = await (await conn.execute(f'SELECT COUNT(*) AS n FROM {store.table}')).fetchone()
                assert row['n'] == 0
            assert await turns.commit(owner, conv, request, message, result, state, token)
            assert not await turns.commit(owner, conv, request, message, result, state, token)
        assert await turns.receipt(owner, conv, request, message) == result
        assert len((await turns.working_snapshot(owner, conv))['messages']) == 2
        job = await store.claim()
        assert job['kind'] == 'maintenance' and job['payload'] == {
            'user_id': owner, 'conv_id': conv, 'request_id': request, 'message': message}
        await store.finish(job, {})
        assert await store.claim() is None
        turns.maintenance_outbox = None

    async def exhausted_attempt_recovers_only_own_committed_receipt():
        owner, conv, request, message = 'receipt-owner', 'exhausted-receipt', 'r', 'fixture committed question'
        result = {'response': 'CANONICAL_COMMITTED_RESULT'}
        state = TaskState(revision=1)
        async with turns.guard(owner, conv) as token:
            await turns.begin_request(owner, conv, request, message, token)
            await turns.save_checkpoint(owner, conv, request, message, result, state, token)
            await turns.commit(owner, conv, request, message, result, state, token)
        payload = {'user_id': owner, 'conv_id': conv, 'client_request_id': request, 'message': message}
        # Submit while the optional turns adapter is absent to additionally model
        # malformed historical payloads. Recovery must still enforce provenance.
        cases = [('own', owner, payload, True),
                 ('foreign-owner', 'attacker', payload, False),
                 ('changed-message', owner, {**payload, 'message': 'different'}, False),
                 ('uncommitted', owner, {**payload, 'client_request_id': 'missing'}, False)]
        submitted = []
        for key, identity, body, expected in cases:
            item = await store.enqueue(identity, 'chat', key, body)
            submitted.append((identity, item['run_id'], expected))
        async with db.connection() as conn:
            await conn.execute(f"UPDATE {store.table} SET status='running',attempts=3,lease_token='dead-worker',"
                               'lease_until=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND')
        fresh = JobStore(db)
        fresh.turns = turns
        assert await fresh.claim() is None
        for identity, job_id, expected in submitted:
            job = await fresh.get(identity, job_id)
            assert job['attempts'] == 3
            assert job['status'] == ('completed' if expected else 'failed')
            assert job['result'] == (result if expected else None)
            assert job['error_type'] == ('' if expected else 'WorkerAttemptsExhausted')
        assert len((await turns.working_snapshot(owner, conv))['messages']) == 2
        window_conv = 'last-attempt-commit-window'
        waiting = await store.enqueue(owner, 'chat', 'commit-window', {**payload, 'conv_id': window_conv})
        async with turns.guard(owner, window_conv) as token:
            await turns.begin_request(owner, window_conv, request, message, token)
            async with db.connection() as conn:
                await conn.execute(f"UPDATE {store.table} SET status='running',attempts=3,lease_token='paused-worker',"
                    'lease_until=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE job_id=%s', (waiting['run_id'],))
            assert await fresh.claim() is None
            pending = await fresh.get(owner, waiting['run_id'])
            assert pending['status'] == 'running' and pending['attempts'] == 3 and pending['result'] is None
            # Simulate the original worker resuming inside its still-valid chat
            # lease after its job lease expired. No fourth handler is invoked.
            await turns.save_checkpoint(owner, window_conv, request, message, result, TaskState(revision=1), token)
            await turns.commit(owner, window_conv, request, message, result, TaskState(revision=1), token)
            assert await fresh.claim() is None
            recovered = await fresh.get(owner, waiting['run_id'])
            assert recovered['status'] == 'completed' and recovered['attempts'] == 3
            assert recovered['result'] == result
        assert len((await turns.working_snapshot(owner, window_conv))['messages']) == 2

    async def sse_and_auth():
        sessions = {'alice-token': 'alice', 'bob-token': 'bob'}
        async def session_user(token):
            return sessions.get(token)
        async def require_admin(x_api_key: str = Header(default='')):
            if x_api_key != 'fixture-admin':
                raise HTTPException(401, 'fixture admin required')
        class EvalInput(BaseModel):
            model_config = ConfigDict(extra='forbid')
            intent_cases: list = []
            dialog_cases: list = []
        memory = SimpleNamespace(turns=SimpleNamespace(session_user=session_user))
        app = FastAPI()
        app.include_router(build_task_router(lambda: (memory, store), require_admin, EvalInput))
        alice = {'Authorization': 'Bearer alice-token'}
        bob = {'Authorization': 'Bearer bob-token'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
            payload = {'message': 'question fixture', 'client_request_id': 'r'}
            assert (await client.post('/chat/tasks', json=payload)).status_code == 401
            submitted = await client.post('/chat/tasks', json=payload, headers=alice)
            assert submitted.status_code == 202
            run_id = submitted.json()['run_id']
            assert (await client.post('/chat/tasks', json=payload, headers=alice)).json()['run_id'] == run_id
            changed = await client.post('/chat/tasks', json={**payload, 'message': 'changed'}, headers=alice)
            assert changed.status_code == 409
            for suffix in ('', '/events'):
                assert (await client.get('/chat/tasks/'+run_id+suffix, headers=bob)).status_code == 404
            job = await store.claim()
            await store.event(job, 'route')
            await store.event(job, 'this arbitrary model text must not be persisted')
            await store.event(job, 'verify')
            before = (await client.get('/chat/tasks/'+run_id, headers=alice)).json()
            assert before['result'] is None and len(before['events']) == 2
            assert all(set(event) == {'run_id', 'sequence', 'type', 'stage'} for event in before['events'])
            await store.finish(job, {'response': 'VERIFIED_FINAL_FIXTURE'})
            response = await client.get('/chat/tasks/'+run_id+'/events', headers=alice)
            assert response.status_code == 200 and response.headers['content-type'].startswith('text/event-stream')
            def events(text):
                return [json.loads(line[6:]) for line in text.splitlines() if line.startswith('data: ')]
            stream = events(response.text)
            assert [e['sequence'] for e in stream] == [1, 2, 3]
            assert [e['type'] for e in stream] == ['progress', 'progress', 'final']
            assert 'VERIFIED_FINAL_FIXTURE' not in json.dumps(stream[:-1])
            assert stream[-1]['result']['response'] == 'VERIFIED_FINAL_FIXTURE'
            resumed = await client.get('/chat/tasks/'+run_id+'/events?after_sequence=0',
                                       headers={**alice, 'Last-Event-ID': '1'})
            assert [e['sequence'] for e in events(resumed.text)] == [2, 3]
            ended = await client.get('/chat/tasks/'+run_id+'/events', headers={**alice, 'Last-Event-ID': '3'})
            assert events(ended.text) == []
            invalid = await client.get('/chat/tasks/'+run_id+'/events', headers={**alice, 'Last-Event-ID': 'invalid'})
            assert invalid.status_code == 400
            del sessions['alice-token']
            assert (await client.get('/chat/tasks/'+run_id+'/events', headers=alice)).status_code == 401
            assert (await client.post('/eval/jobs', json={})).status_code == 401
            evaluation = await client.post('/eval/jobs', json={}, headers={'X-API-Key': 'fixture-admin'})
            assert evaluation.status_code == 202
            evaluation_id = evaluation.json()['run_id']
            assert (await client.get('/eval/jobs/'+evaluation_id)).status_code == 401
            assert (await client.get('/chat/tasks/'+evaluation_id, headers=bob)).status_code == 404

    async def clear_cancels_claimed_and_queued_work():
        from core.chat_service import ChatService
        from unittest.mock import AsyncMock
        turns.maintenance_outbox, store.turns = store, turns
        owner, conv = 'clear-owner', 'clear-conv'
        payload = {'user_id': owner, 'conv_id': conv, 'message': 'must not revive', 'client_request_id': 'r'}
        running = await store.enqueue(owner, 'chat', 'r', payload)
        claimed = await store.claim()
        queued = await store.enqueue(owner, 'chat', 'r2', {**payload, 'client_request_id': 'r2'})
        foreign = await store.enqueue(owner, 'chat', 'other', {**payload, 'conv_id': 'other'})
        async with db.connection() as conn:
            await store.enqueue_turn(conn, owner, conv, 'source', 'maintenance fixture')
        original = store.clear_conversation
        async def fail_after_cancel(conn, user, conversation):
            await original(conn, user, conversation)
            raise RuntimeError('fault_after_cancel_before_clear')
        with patch.object(store, 'clear_conversation', fail_after_cancel):
            try:
                await turns.clear(owner, conv)
            except RuntimeError:
                pass
            else:
                raise AssertionError('clear transaction fault did not propagate')
        assert (await store.get(owner, running['run_id']))['status'] == 'running'
        assert (await store.get(owner, queued['run_id']))['status'] == 'queued'
        await turns.clear(owner, conv)
        for item in (running, queued):
            state = await store.get(owner, item['run_id'])
            assert state['status'] == 'failed' and state['error_type'] == 'ConversationCleared'
            assert state['result'] is None and state['events'] == []
        assert (await store.get(owner, foreign['run_id']))['status'] == 'queued'
        replay = await store.enqueue(owner, 'chat', 'r', payload)
        assert replay['status'] == 'failed'
        async with db.connection() as conn:
            rows = await (await conn.execute(f'SELECT payload_json FROM {store.table} WHERE error_type=%s',
                                            ('ConversationCleared',))).fetchall()
            assert len(rows) == 3 and all(json.loads(row['payload_json']) == {} for row in rows)
        # The worker already copied its payload before clear. Even the legacy path
        # must revalidate its job after taking the conversation lease, before compute.
        orchestrator = SimpleNamespace(run=AsyncMock(side_effect=AssertionError('cancelled worker generated answer')))
        memory = SimpleNamespace(turns=turns, get_context=AsyncMock(return_value=None))
        service = ChatService(orchestrator, memory, None, graph_enabled=False)
        token = PROGRESS_SINK.set(lambda stage: store.event(claimed, stage))
        try:
            try:
                await service.handle(payload['message'], owner, conv, client_request_id='r')
            except LeaseLost:
                pass
            else:
                raise AssertionError('cancelled worker entered the chat pipeline')
        finally:
            PROGRESS_SINK.reset(token)
        orchestrator.run.assert_not_awaited()
        assert (await turns.working_snapshot(owner, conv))['messages'] == []
        assert await turns.receipt(owner, conv, 'r', payload['message']) is None
        turns.maintenance_outbox, store.turns = None, None

    try:
        for name, fn in [('enqueue_idempotency_owner_conflict_and_capacity', enqueue_contract),
                         ('lease_reclaim_rejects_old_worker_writes', lease_fencing),
                         ('worker_recreation_resumes_durable_task', worker_restart),
                         ('automatic_retry_stops_after_three_attempts', bounded_retries),
                         ('last_attempt_recovers_only_owner_and_message_bound_chat_receipt', exhausted_attempt_recovers_only_own_committed_receipt),
                         ('outbox_chat_receipt_and_messages_commit_atomically', transactional_outbox),
                         ('clear_cancels_and_redacts_jobs_atomically_without_revival', clear_cancels_claimed_and_queued_work),
                         ('authenticated_sse_final_only_answer_and_sequence_resume', sse_and_auth)]:
            await check(name, fn)
        return {'mode': 'real_mysql_isolated_jobs_and_asgi_fixtures', 'checks': checks,
                'passed': sum(c['passed'] for c in checks), 'total': len(checks),
                'limitations': 'Deterministic handlers; real MySQL, leases, transaction rollback and SSE routes. '
                               'Worker objects are recreated; this suite does not restart the API container.'}
    finally:
        await store.drop_test_tables()
        await turns.drop_test_tables()
        await turns.close()


if __name__ == '__main__':
    if '--inside' in sys.argv:
        print(json.dumps(asyncio.run(inside()), ensure_ascii=True))
    else:
        root = Path(__file__).resolve().parents[3]
        output = subprocess.check_output(['docker', 'compose', 'exec', '-T', 'hellohebut', 'python', '-c',
            Path(__file__).read_text(encoding='utf-8'), '--inside'], cwd=root, text=True, encoding='utf-8', timeout=180)
        report = json.loads(output.strip().splitlines()[-1])
        report['source_sha256'] = {str(p.relative_to(root).as_posix()): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ('api', 'memory') for p in sorted((root/folder).rglob('*')) if p.suffix in {'.py', '.sql'}}
        report['script_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        path = root/'experiments/v2/reports/stage6_jobs.json'
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(json.dumps({k: report[k] for k in ('passed', 'total')}))
        raise SystemExit(0 if report['passed'] == report['total'] else 1)
