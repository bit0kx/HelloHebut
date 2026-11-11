"""Restart MySQL/application, recover a computed answer and retain committed facts."""
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = '''
import asyncio, json, os, sys
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock
from core.chat_service import ChatResult, ChatService
from core.contracts import TaskState
from memory.fact_store import FactStore
from memory.mysql_turn_store import MySQLTurnStore
async def main():
    schema, action = sys.argv[1:]
    assert schema.startswith('test_restart_') and len(schema) == 45
    store = FactStore(os.getenv('V2_FACT_DATABASE_URL', ''), schema)
    turns = MySQLTurnStore(os.environ['REDIS_URL'], database=store)
    owner, conv, message = schema, 'restart-conversation', 'restart fixture'
    if action == 'write':
        await store.initialize()
        await turns.initialize()
        await store.mutate('restart-fixture', 'save', 'save', 0, key='interests', value='robotics')
        async with turns.guard(owner, conv) as token:
            await turns.begin_request(owner, conv, 'ready', message, token)
            result = asdict(ChatResult(conv_id=conv,response='persisted answer',intent='general',agent_type='general',
                                      escalated=False,latency_ms=1,request_id='ready',trace_id='original-run'))
            await turns.save_checkpoint(owner, conv, 'ready', message, result, TaskState(revision=1), token)
        assert await turns.receipt(owner, conv, 'ready', message) is None
    elif action == 'clear':
        await turns.drop_test_tables()
        await store.drop_test_tables()
        await turns.close()
        return
    else:
        service = ChatService(None, SimpleNamespace(turns=turns,maintenance=None), None)
        service._handle_turn = AsyncMock(side_effect=AssertionError('must resume without generation'))
        result = await service.handle(message,owner,conv,client_request_id='ready',update_profile=False)
        again = await service.handle(message,owner,conv,client_request_id='ready',update_profile=False)
        assert asdict(result) == asdict(again) and result.trace_id == 'original-run'
        assert len((await turns.working_snapshot(owner,conv))['messages']) == 2
        assert service._handle_turn.await_count == 0
    snapshot = await store.snapshot('restart-fixture')
    assert len(snapshot['items']) == 1 and snapshot['items'][0]['value'] == 'robotics'
    print(json.dumps({'revision': snapshot['revision'], 'memory_id': snapshot['items'][0]['memory_id']}))
    await turns.close()
asyncio.run(main())
'''


def step(schema, action):
    output = subprocess.check_output(['docker', 'compose', 'exec', '-T', 'hellohebut', 'python', '-c',
                                     SCRIPT, schema, action], cwd=ROOT, text=True, encoding='utf-8', timeout=30)
    return json.loads(output.strip().splitlines()[-1]) if output.strip() else None


if __name__ == '__main__':
    schema = 'test_restart_' + uuid.uuid4().hex
    passed, error, cleaned = False, '', False
    started = time.monotonic()
    try:
        before = step(schema, 'write')
        subprocess.run(['docker', 'compose', 'restart', 'mysql', 'hellohebut'], cwd=ROOT, check=True, timeout=75)
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            try:
                if httpx.get('http://localhost:18000/health', timeout=3).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        after = step(schema, 'read')
        passed = before == after
    except Exception as exc:
        error = type(exc).__name__
    finally:
        try:
            step(schema, 'clear')
            cleaned = True
        except Exception as exc:
            error = type(exc).__name__
        report = {'mode': 'mysql_and_app_restart', 'passed': passed and cleaned, 'cleaned': cleaned,
                  'error_type': error, 'duration_s': round(time.monotonic()-started, 2),
                  'source_sha256': {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest()
                                    for p in ('memory/fact_store.py', 'memory/mysql_database.py', 'memory/mysql_turn_store.py',
                                              'memory/migrations/001_facts.sql','memory/migrations/002_turns.sql','core/chat_service.py')},
                  'checks': ['preference_survives_restart','computed_answer_resumes_without_generation',
                             'replayed_receipt_has_original_trace','one_committed_turn'],
                  'limitations': 'Isolated fixtures survive a real MySQL/application restart. Recovery is limited to the '
                                 'post-generation boundary; not LangGraph or internal retrieval-node recovery.'}
        (ROOT/'experiments/v2/reports/stage6_restart.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
        print(json.dumps(report))
    raise SystemExit(0 if report['passed'] else 1)
