"""Real MySQL/Redis ledger checks in disposable tables and private Redis identities.

Run after building the application. No production conversation is read or modified.
The recovery fixture resumes an already computed answer, not an internal RAG node.
"""
import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid


async def inside():
    from core.chat_service import ChatResult, ChatService
    from core.contracts import TaskState
    from core.runtime import LeaseLost, RequestConflict
    from memory.conversation_memory import MemoryManager
    from memory.mysql_database import MySQLDatabase
    from memory.mysql_turn_store import MySQLTurnStore

    namespace = 'test_turns_' + uuid.uuid4().hex
    database = MySQLDatabase(os.getenv('V2_FACT_DATABASE_URL', ''), namespace)
    store = MySQLTurnStore(os.environ['REDIS_URL'], database=database)
    await store.initialize()
    owner = 'fixture_' + uuid.uuid4().hex
    checks, redis_keys = [], set()

    async def check(name, operation):
        try:
            await operation()
            checks.append({'check': name, 'passed': True})
        except Exception as exc:
            checks.append({'check': name, 'passed': False, 'error_type': type(exc).__name__})
            import traceback
            traceback.print_exc(file=sys.stderr)

    async def commit(conv, request, message=None, response=None):
        message, response = message or request, response or 'answer-' + request
        async with store.guard(owner, conv) as token:
            receipt = await store.receipt(owner, conv, request, message)
            if receipt:
                return receipt
            checkpoint = await store.begin_request(owner, conv, request, message, token)
            if checkpoint:
                result = checkpoint['result']
                state = TaskState.model_validate(checkpoint['state'])
                memory_revision = checkpoint['memory_revision']
            else:
                state = await store.state(owner, conv)
                state.revision += 1
                result = {'response': response, 'trace_id': request}
                memory_revision = 3
                await store.save_checkpoint(owner, conv, request, message, result, state, token,
                                            memory_revision=memory_revision)
            await store.commit(owner, conv, request, message, result, state, token,
                               memory_revision=memory_revision)
            return result

    async def atomic_turn():
        result = await commit('atomic', 'atomic-1', 'question-1', 'answer-1')
        snapshot = await store.working_snapshot(owner, 'atomic')
        assert [(m['role'], m['content']) for m in snapshot['messages']] == [
            ('user', 'question-1'), ('assistant', 'answer-1')]
        assert (await store.state(owner, 'atomic')).revision == 1
        assert await store.receipt(owner, 'atomic', 'atomic-1', 'question-1') == result
        source = await store.receipt_record(owner, 'atomic', 'atomic-1')
        assert source['message_hash'] == store.message_hash('question-1')
        assert source['memory_revision'] == 3 and source['result']['trace_id'] == 'atomic-1'

    async def rollback():
        async with store.guard(owner, 'rollback') as token:
            await store.begin_request(owner, 'rollback', 'rollback-1', 'q', token)
            state = TaskState(revision=1)
            result = {'response': 'a'}
            await store.save_checkpoint(owner, 'rollback', 'rollback-1', 'q', result, state, token)
            with patch.object(store, '_mark_committed', AsyncMock(side_effect=RuntimeError('injected_commit_failure'))):
                try:
                    await store.commit(owner, 'rollback', 'rollback-1', 'q', result, state, token)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError('fault did not interrupt commit')
        assert not (await store.working_snapshot(owner, 'rollback'))['messages']
        assert (await store.state(owner, 'rollback')).revision == 0
        assert await store.receipt(owner, 'rollback', 'rollback-1', 'q') is None
        await commit('rollback', 'rollback-1', 'q', 'a')
        assert len((await store.working_snapshot(owner, 'rollback'))['messages']) == 2

    async def duplicate_and_conflict():
        results = await asyncio.gather(*[commit('race', 'race-1', 'same') for _ in range(3)])
        assert results[0] == results[1] == results[2]
        assert len((await store.working_snapshot(owner, 'race'))['messages']) == 2
        try:
            await store.receipt(owner, 'race', 'race-1', 'changed')
        except RequestConflict:
            pass
        else:
            raise AssertionError('committed ID accepted different content')
        async with store.guard(owner, 'bound') as token:
            await store.begin_request(owner, 'bound', 'bound-1', 'original', token)
        async with store.guard(owner, 'bound') as token:
            try:
                await store.begin_request(owner, 'bound', 'bound-1', 'changed', token)
            except RequestConflict:
                pass
            else:
                raise AssertionError('unfinished ID accepted different content')

    async def lease_fence():
        async with store.guard(owner, 'lease', lease_s=.3) as stale:
            await store.begin_request(owner, 'lease', 'lease-1', 'q', stale)
            state, result = TaskState(revision=1), {'response': 'a'}
            await store.save_checkpoint(owner, 'lease', 'lease-1', 'q', result, state, stale)
            await asyncio.sleep(.35)
            try:
                await store.commit(owner, 'lease', 'lease-1', 'q', result, state, stale)
            except LeaseLost:
                pass
            else:
                raise AssertionError('expired lease committed')
            async with store.guard(owner, 'lease') as current:
                assert current != stale
                try:
                    await store.commit(owner, 'lease', 'lease-1', 'q', result, state, stale)
                except LeaseLost:
                    pass
                else:
                    raise AssertionError('old token committed under replacement lease')
                checkpoint = await store.begin_request(owner, 'lease', 'lease-1', 'q', current)
                assert checkpoint is not None
                await store.commit(owner, 'lease', 'lease-1', 'q', result, state, current)
        assert len((await store.working_snapshot(owner, 'lease'))['messages']) == 2

    def service(counter):
        memory = SimpleNamespace(turns=store, maintenance=None)
        instance = ChatService(SimpleNamespace(), memory, None)
        async def compute(message, user, conv, previous, run, request):
            counter.append(request)
            state = previous.model_copy(deep=True)
            state.revision += 1
            run.memory_revision = 7
            return ChatResult(conv_id=conv, response='computed-once', intent='fixture', agent_type='general',
                              escalated=False, latency_ms=1, request_id=request, trace_id=run.run_id), state
        instance._handle_turn = compute
        return instance

    async def checkpoint_recovery():
        calls = []
        first = service(calls)
        with patch.object(store, '_mark_committed', AsyncMock(side_effect=RuntimeError('restart_boundary'))):
            try:
                await first.handle('recover-question', owner, 'recovery', client_request_id='recover-1')
            except RuntimeError:
                pass
            else:
                raise AssertionError('checkpoint boundary was not reached')
        assert calls == ['recover-1']
        assert not (await store.working_snapshot(owner, 'recovery'))['messages']
        # A fresh service instance must reuse the persisted completed generation.
        second = service(calls)
        result = await second.handle('recover-question', owner, 'recovery', client_request_id='recover-1')
        assert calls == ['recover-1'] and result.response == 'computed-once'
        record = await store.receipt_record(owner, 'recovery', 'recover-1')
        assert record['memory_revision'] == 7 and record['result']['trace_id'] == result.trace_id
        assert len((await store.working_snapshot(owner, 'recovery'))['messages']) == 2

    async def reply_lost_after_commit():
        calls, original = [], store.commit
        async def commit_then_fail(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError('response_connection_lost')
        with patch.object(store, 'commit', commit_then_fail):
            try:
                await service(calls).handle('lost-response', owner, 'lost', client_request_id='lost-1')
            except RuntimeError:
                pass
        result = await service(calls).handle('lost-response', owner, 'lost', client_request_id='lost-1')
        assert calls == ['lost-1'] and result.response == 'computed-once'
        assert len((await store.working_snapshot(owner, 'lost'))['messages']) == 2

    async def stale_checkpoints():
        for conv, request in [('expired', 'expired-1'), ('advanced', 'advanced-1')]:
            async with store.guard(owner, conv) as token:
                await store.begin_request(owner, conv, request, 'stale-question', token)
                await store.save_checkpoint(owner, conv, request, 'stale-question',
                                            {'response': 'stale'}, TaskState(revision=1), token)
        async with database.connection() as conn:
            await conn.execute(f'UPDATE {database.table("chat_requests")} '
                               'SET updated_at=UTC_TIMESTAMP(6)-INTERVAL 2 HOUR WHERE request_hash=%s',
                               (hashlib.sha256(b'expired-1').hexdigest(),))
        async with store.guard(owner, 'expired') as token:
            assert await store.begin_request(owner, 'expired', 'expired-1', 'stale-question', token) is None
        await commit('advanced', 'newer-turn', 'new question')
        async with store.guard(owner, 'advanced') as token:
            assert await store.begin_request(owner, 'advanced', 'advanced-1', 'stale-question', token) is None

    async def seed_redis(conv):
        keys = {kind: store.key(kind, owner, conv) for kind in ('wm', 'summary', 'state', 'receipts')}
        receipt_key = store.receipt_key(owner, conv, 'legacy-request')
        redis_keys.update([*keys.values(), receipt_key])
        raw = lambda role, content: json.dumps({'role': role, 'content': content,
            'ts': datetime.now().isoformat(), 'metadata': {'request_id': 'legacy-request'}})
        await store.client.delete(keys['wm'])
        await store.client.rpush(keys['wm'], raw('assistant', 'legacy answer'), raw('user', 'legacy question'))
        await store.client.set(keys['summary'], 'legacy summary')
        await store.client.set(keys['state'], json.dumps({'schema_version': 1, 'revision': 3,
                                                        'entities': {'rank': ['12000']}}))
        await store.client.set(receipt_key, json.dumps({'message_hash': store.message_hash('legacy question'),
            'result': {'response': 'legacy answer', 'trace_id': 'legacy-trace'}, 'memory_revision': 2}))
        await store.client.sadd(keys['receipts'], receipt_key)

    async def migration_and_clear():
        await seed_redis('legacy')
        migrated = await store.working_snapshot(owner, 'legacy')
        assert [m['content'] for m in migrated['messages']] == ['legacy question', 'legacy answer']
        assert migrated['summary'] == 'legacy summary'
        state = await store.state(owner, 'legacy')
        assert state.schema_version == 2 and not state.subject_is_self
        assert state.entities['rank'] == ['12000']
        assert (await store.receipt(owner, 'legacy', 'legacy-request', 'legacy question'))['response'] == 'legacy answer'
        assert (await store.receipt_record(owner, 'legacy', 'legacy-request'))['memory_revision'] == 2
        await store.clear(owner, 'legacy')
        # Reintroduce the old fixture in Redis: the SQL import tombstone must still win.
        await seed_redis('legacy')
        empty = await store.working_snapshot(owner, 'legacy')
        assert not empty['messages'] and empty['summary'] == ''
        assert not (await store.state(owner, 'legacy')).entities
        assert await store.receipt(owner, 'legacy', 'legacy-request', 'legacy question') is None

    async def compression_cas():
        for i in range(8):
            await commit('compression', f'compression-{i}', f'q{i}', f'a{i}')
        before = await store.working_snapshot(owner, 'compression')
        await commit('compression', 'compression-new', 'q-new', 'a-new')
        assert await store.compress(owner, 'compression', before, 10, 'summary of old turns')
        after = await store.working_snapshot(owner, 'compression')
        assert [m['content'] for m in after['messages']] == ['q5', 'a5', 'q6', 'a6', 'q7', 'a7', 'q-new', 'a-new']
        assert after['summary'] == 'summary of old turns'
        # Compress only the working context; the durable committed turn retains its original text.
        async with database.connection() as conn:
            archived = await (await conn.execute(f'SELECT message_text,result_json,status FROM {database.table("chat_requests")} '
                'WHERE conversation_key=%s AND request_hash=%s',
                (store.identity(owner, 'compression'), store.request_hash('compression-0')))).fetchone()
        assert archived['message_text'] == 'q0' and json.loads(archived['result_json'])['response'] == 'a0'
        assert archived['status'] == 'committed'
        assert not await store.compress(owner, 'compression', before, 10, 'obsolete competing summary')
        await store.clear(owner, 'compression')
        assert not await store.compress(owner, 'compression', after, 2, 'resurrected summary')
        assert (await store.working_snapshot(owner, 'compression'))['summary'] == ''

    async def summary_failure_preserves_messages():
        for i in range(8):
            await commit('summary-failure', f'summary-{i}')
        before = await store.working_snapshot(owner, 'summary-failure')
        memory = MemoryManager.__new__(MemoryManager)
        memory.turns, memory._client, memory._model = store, object(), 'fixture'
        memory.verified_memory_mode = True
        with patch('memory.conversation_memory.create_message', AsyncMock(side_effect=RuntimeError('model_offline'))):
            await memory._compress(owner, 'summary-failure')
        assert await store.working_snapshot(owner, 'summary-failure') == before

    async def isolation_and_redis_loss():
        await commit('isolation', 'isolation-1', 'isolated question')
        assert await store.receipt(owner + '-other', 'isolation', 'isolation-1', 'isolated question') is None
        assert not (await store.working_snapshot(owner + '-other', 'isolation'))['messages']
        keys = [store.key(kind, owner, 'isolation') for kind in ('wm', 'state', 'summary', 'receipts', 'lease')]
        keys.append(store.receipt_key(owner, 'isolation', 'isolation-1'))
        await store.client.delete(*keys)
        other = MySQLTurnStore(os.environ['REDIS_URL'], database=database)
        try:
            assert (await other.receipt(owner, 'isolation', 'isolation-1', 'isolated question'))['response']
            assert len((await other.working_snapshot(owner, 'isolation'))['messages']) == 2
            assert (await other.state(owner, 'isolation')).revision == 1
        finally:
            await other.close()

    async def clear_checkpoint():
        async with store.guard(owner, 'clear-checkpoint') as token:
            await store.begin_request(owner, 'clear-checkpoint', 'clear-1', 'q', token)
            await store.save_checkpoint(owner, 'clear-checkpoint', 'clear-1', 'q',
                                        {'response': 'obsolete'}, TaskState(revision=1), token)
        await store.clear(owner, 'clear-checkpoint')
        async with store.guard(owner, 'clear-checkpoint') as token:
            assert await store.begin_request(owner, 'clear-checkpoint', 'clear-1', 'q', token) is None
        assert not (await store.working_snapshot(owner, 'clear-checkpoint'))['messages']

    try:
        for name, operation in [
            ('atomic_turn_state_and_provenance', atomic_turn),
            ('commit_failure_rolls_back_entire_turn', rollback),
            ('concurrent_duplicate_and_request_binding', duplicate_and_conflict),
            ('expired_and_replaced_lease_fenced', lease_fence),
            ('computed_checkpoint_reused_by_fresh_service', checkpoint_recovery),
            ('lost_response_after_commit_replays_once', reply_lost_after_commit),
            ('expired_or_stale_checkpoint_recomputes', stale_checkpoints),
            ('redis_migration_and_clear_tombstone', migration_and_clear),
            ('summary_cas_preserves_new_turn_and_rejects_stale', compression_cas),
            ('summary_failure_keeps_original_messages', summary_failure_preserves_messages),
            ('owner_isolation_and_redis_working_data_loss', isolation_and_redis_loss),
            ('clear_removes_uncommitted_checkpoint', clear_checkpoint),
        ]:
            await check(name, operation)
        async with database.connection() as conn:
            version = (await (await conn.execute('SELECT VERSION() AS version')).fetchone())['version']
        return {'mode': 'real_mysql_redis_isolated_ledger', 'mysql': version,
                'checks': checks, 'passed': sum(c['passed'] for c in checks), 'total': len(checks),
                'limitations': 'Synthetic functional checks with real MySQL transactions. Fresh service recovery starts '
                               'after answer computation; no internal retrieval-node or LangGraph recovery claim. '
                               'Full database/application restart is validated separately.'}
    finally:
        if redis_keys:
            await store.client.delete(*redis_keys)
        await store.drop_test_tables()
        await store.close()


if __name__ == '__main__':
    if '--inside' in sys.argv:
        print(json.dumps(asyncio.run(inside()), ensure_ascii=True))
    else:
        root = Path(__file__).resolve().parents[3]
        output = subprocess.check_output(['docker', 'compose', 'exec', '-T', 'hellohebut', 'python', '-c',
                                         Path(__file__).read_text(encoding='utf-8'), '--inside'], cwd=root,
                                        text=True, encoding='utf-8', timeout=240)
        report = json.loads(output.strip().splitlines()[-1])
        report['source_sha256'] = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for folder in ('memory', 'core', 'api')
                                  for p in sorted((root / folder).rglob('*')) if p.suffix in {'.py', '.sql'}}
        report['script_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        target = root / 'experiments/v2/reports/stage6_turns.json'
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({k: report[k] for k in ('passed', 'total', 'mysql')}))
        raise SystemExit(0 if report['passed'] == report['total'] else 1)
