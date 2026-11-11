"""Opt-in: restart v2 PostgreSQL/application, verify one disposable schema, then remove it."""
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
from psycopg import sql
from memory.fact_store import FactStore
async def main():
    schema, action = sys.argv[1:]
    assert schema.startswith('test_restart_') and len(schema) == 45
    store = FactStore(os.getenv('V2_FACT_DATABASE_URL', ''), schema)
    if action == 'write':
        await store.initialize()
        await store.mutate('restart-fixture', 'save', 'save', 0, key='interests', value='robotics')
    elif action == 'clear':
        async with store.connection() as conn:
            await conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
        return
    snapshot = await store.snapshot('restart-fixture')
    assert len(snapshot['items']) == 1 and snapshot['items'][0]['value'] == 'robotics'
    print(json.dumps({'revision': snapshot['revision'], 'memory_id': snapshot['items'][0]['memory_id']}))
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
        subprocess.run(['docker', 'compose', 'restart', 'postgres', 'hellohebut'], cwd=ROOT, check=True, timeout=75)
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
        report = {'mode': 'postgres_and_app_restart', 'passed': passed and cleaned, 'cleaned': cleaned,
                  'error_type': error, 'duration_s': round(time.monotonic()-started, 2),
                  'source_sha256': {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest()
                                    for p in ('memory/fact_store.py', 'memory/migrations/001_facts.sql')},
                  'limitations': 'One confirmed preference in an isolated schema survives a real database/application restart. '
                                 'Does not claim workflow checkpoint recovery or atomic PostgreSQL chat commits.'}
        (ROOT/'experiments/v2/reports/stage4_restart.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
        print(json.dumps(report))
    raise SystemExit(0 if report['passed'] else 1)
