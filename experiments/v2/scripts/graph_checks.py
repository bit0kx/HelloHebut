"""Real MySQL checkpoints with deterministic model/tool fixtures, isolated tables."""
import asyncio
from collections import Counter
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch
import uuid


async def inside(namespace=None, process_phase=None):
    from agents.agent_orchestrator import AgentType
    from core.chat_service import ChatService
    from core.contracts import AnswerVerdict, Evidence, EvidenceVerdict, SupportedClaim, TaskState
    from core.intent_recognizer import IntentCategory, IntentResult, UrgencyLevel
    from core.runtime import BudgetExceeded, CURRENT_RUN, LeaseLost, RunContext
    from memory.mysql_database import MySQLDatabase
    from memory.mysql_turn_store import MySQLTurnStore
    from rag.agentic import AgenticRAG, RetrievalOutcome
    from workflows.chat_graph import ChatWorkflow, WORKFLOW_VERSION
    from workflows.checkpoints import MySQLCheckpointSaver

    namespace = namespace or 'test_graph_' + uuid.uuid4().hex
    if not __import__('re').fullmatch(r'test_graph_[0-9a-f]{32}', namespace):
        raise ValueError('isolated_graph_namespace_required')
    db = MySQLDatabase(os.getenv('V2_FACT_DATABASE_URL', ''), namespace)
    turns = MySQLTurnStore(os.environ['REDIS_URL'], database=db)
    await turns.initialize()
    owner = namespace
    checks = []

    async def check(name, fn):
        try:
            details = await fn() or {}
            checks.append({'check': name, 'passed': True, **details})
        except Exception as exc:
            import traceback
            traceback.print_exc(file=sys.stderr)
            checks.append({'check': name, 'passed': False, 'error_type': type(exc).__name__})

    def service(*, fail_answer=False, crash_answer=False):
        failed = False
        async def consume(kind):
            run = CURRENT_RUN.get()
            run.consume(kind)
            await run.persist_usage()
        async def recognize(message, history=None):
            await consume('model')
            return IntentResult(IntentCategory.SCHOOL_INFO, 1, UrgencyLevel.LOW, {}, 'fixture', 0)
        async def memory_context(*args, **kwargs):
            return None
        async def retrieve(*args, **kwargs):
            await consume('tool')
            evidence = Evidence(evidence_id='e1', content='学校设北辰校区。', content_hash='h', title='校区')
            return RetrievalOutcome(evidence=[evidence], verdict=EvidenceVerdict(sufficient=True, decision='answer',
                supported_claims=[SupportedClaim(claim='校区', evidence_ids=['e1'])]), stop_reason='sufficient')
        async def answer(request):
            nonlocal failed
            await consume('model')
            if crash_answer:
                # Deliberately bypass finally/lease release/checkpoint flushing.
                # The previous node and this call reservation are already durable.
                os._exit(73)
            if fail_answer and not failed:
                failed = True
                raise RuntimeError('injected_after_model_budget_reserved')
            return SimpleNamespace(response='学校设北辰校区。[来源1]', intent=request.intent,
                                   agent_type=AgentType.GENERAL, escalated=False)
        async def verify(*args, **kwargs):
            await consume('model')
            return AnswerVerdict(passed=True)
        memory = SimpleNamespace(turns=turns, maintenance=None, get_context=memory_context)
        rag = SimpleNamespace(retrieve=retrieve, context=AgenticRAG.context, verify_answer=verify)
        return ChatService(SimpleNamespace(run=answer), memory, None, recognizer=SimpleNamespace(recognize=recognize),
                           agentic_rag=rag, graph_enabled=True)

    if process_phase:
        original_guard = turns.guard
        def brief_guard(user, conv, **kwargs):
            return original_guard(user, conv, lease_s=2, wait_s=3)
        turns.guard = brief_guard
        try:
            result = await service(crash_answer=process_phase == 'crash').handle(
                '学校有哪些校区？', owner, 'process-crash', client_request_id='r', update_profile=False)
            return {'verified': result.verified, 'request_id': result.request_id}
        finally:
            await turns.close()

    async def boundary_recovery():
        stages = ['route', 'required_tools', 'retrieve', 'answer', 'verify', 'finalize']
        for target in stages:
            calls, failed = Counter(), set()
            def instrument(name, original):
                async def wrapped(self, data):
                    calls[name] += 1
                    if name == target and name not in failed:
                        failed.add(name)
                        raise RuntimeError('node_boundary_fixture')
                    await original(self, data)
                return wrapped
            with ExitStack() as stack:
                for name in stages:
                    stack.enter_context(patch.object(ChatWorkflow, name, instrument(name, getattr(ChatWorkflow, name))))
                try:
                    await service().handle('学校有哪些校区？', owner, target, client_request_id='r', update_profile=False)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError('node fault was not raised')
                assert await turns.receipt(owner, target, 'r', '学校有哪些校区？') is None
                result = await service().handle('学校有哪些校区？', owner, target, client_request_id='r', update_profile=False)
                assert result.verified
                assert all(calls[name] == (2 if name == target else 1) for name in stages), dict(calls)
                assert len((await turns.working_snapshot(owner, target))['messages']) == 2
        return {'boundaries': len(stages), 'previous_nodes_rerun': 0}

    async def budget_reserved_before_call():
        failing = service(fail_answer=True)
        try:
            await failing.handle('学校有哪些校区？', owner, 'budget', client_request_id='r', update_profile=False)
        except RuntimeError:
            pass
        async with db.connection() as conn:
            row = await (await conn.execute(f'SELECT usage_json FROM {db.table("graph_runs")} '
                'WHERE conversation_key=%s', (turns.identity(owner, 'budget'),))).fetchone()
        usage = json.loads(row['usage_json'])
        assert usage['model_calls'] == 2 and usage['tool_calls'] == 1
        await service().handle('学校有哪些校区？', owner, 'budget', client_request_id='r', update_profile=False)
        async with db.connection() as conn:
            row = await (await conn.execute(f'SELECT usage_json FROM {db.table("graph_runs")} '
                'WHERE conversation_key=%s', (turns.identity(owner, 'budget'),))).fetchone()
        usage = json.loads(row['usage_json'])
        assert usage['model_calls'] == 4 and usage['tool_calls'] == 1

    async def prepare(conv, token, version=WORKFLOW_VERSION):
        await turns.begin_request(owner, conv, 'r', 'q', token)
        run = RunContext()
        saver = MySQLCheckpointSaver(turns, owner, conv, 'r', 'q', token, run, version=version)
        await saver.prepare()
        return saver, run

    async def saver_contract():
        async with turns.guard(owner, 'contract') as token:
            saver, run = await prepare('contract', token)
            checkpoint = {'v': 4, 'id': 'checkpoint-01', 'ts': '2026-11-11T00:00:00+00:00',
                'channel_values': {'payload': {'ok': True}}, 'channel_versions': {}, 'versions_seen': {}}
            config = await saver.aput(saver.config, checkpoint, {'source': 'loop', 'step': 0}, {})
            await saver.aput_writes(config, [('payload', {'step': 1})], 'task-1')
            await saver.aput_writes(config, [('payload', {'step': 999})], 'task-1')
            current = await saver.aget_tuple(saver.config)
            assert current.checkpoint == checkpoint
            assert current.pending_writes == [('task-1', 'payload', {'step': 1})]
            assert len([x async for x in saver.alist(saver.config, filter={'step': 0})]) == 1
            try:
                await saver.aget_tuple({'configurable': {'thread_id': 'foreign-thread'}})
            except ValueError:
                pass
            else:
                raise AssertionError('foreign thread scope accepted')

    async def lease_fencing():
        async with turns.guard(owner, 'lease', lease_s=.2) as token:
            saver, run = await prepare('lease', token)
            await asyncio.sleep(.25)
            try:
                await saver.save_usage()
            except LeaseLost:
                pass
            else:
                raise AssertionError('expired graph lease wrote data')

    async def invalidation():
        for conv in ('version', 'expired', 'advanced'):
            async with turns.guard(owner, conv) as token:
                saver, run = await prepare(conv, token)
                run.model_calls = 5
                await saver.save_usage()
        async with db.connection() as conn:
            await conn.execute(f'UPDATE {db.table("graph_runs")} SET created_at=UTC_TIMESTAMP(6)-INTERVAL 2 HOUR '
                'WHERE conversation_key=%s', (turns.identity(owner, 'expired'),))
            await conn.execute(f'UPDATE {turns.conversations} SET revision=revision+1 '
                'WHERE conversation_key=%s', (turns.identity(owner, 'advanced'),))
        for conv in ('version', 'expired', 'advanced'):
            async with turns.guard(owner, conv) as token:
                saver, run = await prepare(conv, token, 'changed-version' if conv == 'version' else WORKFLOW_VERSION)
                assert run.model_calls == 5
                assert await saver.aget_tuple(saver.config) is None

    async def exhausted_budget_not_reset():
        async with turns.guard(owner, 'exhausted') as token:
            saver, run = await prepare('exhausted', token)
            run.model_calls = run.max_models
            await saver.save_usage()
        async with turns.guard(owner, 'exhausted') as token:
            saver, run = await prepare('exhausted', token)
            try:
                run.consume('model')
            except BudgetExceeded:
                pass
            else:
                raise AssertionError('model quota reset by retry')

    async def clear_graph():
        async with turns.guard(owner, 'clear') as token:
            saver, run = await prepare('clear', token)
            await saver.aput(saver.config, {'id': 'c1', 'channel_values': {}}, {'step': 0}, {})
        await turns.clear(owner, 'clear')
        async with db.connection() as conn:
            for table in ('graph_runs', 'graph_checkpoints', 'graph_writes'):
                row = await (await conn.execute(f'SELECT COUNT(*) AS n FROM {db.table(table)} WHERE conversation_key=%s',
                    (turns.identity(owner, 'clear'),))).fetchone()
                assert row['n'] == 0

    async def process_crash_recovery():
        script = Path.cwd()/'experiments/v2/scripts/graph_checks.py'
        async def child(phase):
            process = await asyncio.create_subprocess_exec(sys.executable, str(script), '--process-child', namespace, phase,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                output, error = await asyncio.wait_for(process.communicate(), timeout=30)
            except BaseException:
                process.kill()
                await process.wait()
                raise
            return process.returncode, output, error
        code, _, error = await child('crash')
        assert code == 73, {'exit_code': code, 'stderr': error.decode()[-500:]}
        assert await turns.receipt(owner, 'process-crash', 'r', '学校有哪些校区？') is None
        async with db.connection() as conn:
            row = await (await conn.execute(f'SELECT usage_json FROM {db.table("graph_runs")} '
                'WHERE conversation_key=%s', (turns.identity(owner, 'process-crash'),))).fetchone()
        usage = json.loads(row['usage_json'])
        assert usage['model_calls'] == 2 and usage['tool_calls'] == 1
        # The new process waits for the old server-clock lease, then resumes the
        # already checkpointed retrieval without rerunning routing or retrieval.
        code, output, error = await child('resume')
        assert code == 0, {'exit_code': code, 'stderr': error.decode()[-500:]}
        assert json.loads(output.decode().strip().splitlines()[-1])['verified']
        async with db.connection() as conn:
            row = await (await conn.execute(f'SELECT usage_json FROM {db.table("graph_runs")} '
                'WHERE conversation_key=%s', (turns.identity(owner, 'process-crash'),))).fetchone()
        usage = json.loads(row['usage_json'])
        assert usage['model_calls'] == 4 and usage['tool_calls'] == 1
        assert len((await turns.working_snapshot(owner, 'process-crash'))['messages']) == 2
        return {'crashed_process_exit_code': 73, 'resumed_model_calls': 4, 'resumed_tool_calls': 1}

    try:
        for name, fn in [('six_node_boundaries_resume_without_repeating_completed_nodes', boundary_recovery),
            ('budget_reserved_before_external_call_and_preserved_on_resume', budget_reserved_before_call),
            ('checkpoint_pending_write_contract_and_scope', saver_contract), ('lease_fences_checkpoint_writes', lease_fencing),
            ('version_ttl_and_task_revision_invalidate_old_graph', invalidation), ('retry_does_not_reset_exhausted_budget', exhausted_budget_not_reset),
            ('clear_removes_all_graph_state', clear_graph), ('hard_process_exit_resumes_fenced_checkpoint', process_crash_recovery)]:
            await check(name, fn)
        return {'mode': 'real_mysql_langgraph_isolated_fixtures', 'checks': checks,
                'passed': sum(c['passed'] for c in checks), 'total': len(checks),
                'limitations': 'Deterministic model/tool fixtures, real LangGraph and MySQL. Includes os._exit and a fresh Python process; '
                               'an interrupted external call may be repeated. No hidden reasoning or pickle serialization.'}
    finally:
        await turns.drop_test_tables()
        await turns.close()


if __name__ == '__main__':
    if '--process-child' in sys.argv:
        offset = sys.argv.index('--process-child')
        print(json.dumps(asyncio.run(inside(sys.argv[offset+1], sys.argv[offset+2])), ensure_ascii=True))
    elif '--inside' in sys.argv:
        print(json.dumps(asyncio.run(inside()), ensure_ascii=True))
    else:
        root = Path(__file__).resolve().parents[3]
        output = subprocess.check_output(['docker', 'compose', 'exec', '-T', 'hellohebut', 'python', '-c',
            Path(__file__).read_text(encoding='utf-8'), '--inside'], cwd=root, text=True, encoding='utf-8', timeout=180)
        report = json.loads(output.strip().splitlines()[-1])
        report['source_sha256'] = {str(p.relative_to(root).as_posix()): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ('workflows', 'memory', 'core') for p in sorted((root/folder).rglob('*')) if p.suffix in {'.py', '.sql'}}
        report['script_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        path = root/'experiments/v2/reports/stage6_graph.json'
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(json.dumps({k: report[k] for k in ('passed', 'total')}))
        raise SystemExit(0 if report['passed'] == report['total'] else 1)
