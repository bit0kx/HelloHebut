"""Reproducible role/composer fixture comparison and opt-in provider capability probe.

Offline mode makes no model calls. --live-probe uses container environment only,
two bounded provider requests and synthetic evidence; it never reads .env files.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from agents.agent_orchestrator import AgentOrchestrator, AgentResponse, AgentType, Request
from agents.composer import Claim, RoleResult, validate_role_result
from agents.read_tools import assistant_exchange_blocks, register_read_tools
from core.context_budget import bounded_json, fit_context
from core.intent_recognizer import IntentCategory, UrgencyLevel
from core.llm_client import create_message, extract_text
from core.runtime import CURRENT_RUN, RunContext
from tooling.tool_manager import MCPToolManager


def fact(statement, key, source=1):
    return {'statement': statement, 'fact_key': key, 'evidence_ids': [f'source:{source}']}


FIXTURES = [
    {'id': 'simple_policy', 'question': '测试规则是什么', 'intent': 'admission_policy',
     'roles': {'policy': {'status': 'completed', 'claims': [fact('合成规则要求核对年度', 'rule')]}},
     'duplicate': '', 'conflicting': []},
    {'id': 'duplicate', 'question': '位次匹配与就业规划', 'intent': 'score_risk',
     'roles': {'risk': {'status': 'completed', 'claims': [fact('合成专业位于测试校区', 'campus')]},
               'planning': {'status': 'completed', 'claims': [fact('合成专业位于测试校区', 'campus', 2)]}},
     'duplicate': '合成专业位于测试校区', 'conflicting': []},
    {'id': 'conflicting_numbers', 'question': '位次匹配与就业规划', 'intent': 'score_risk',
     'roles': {'risk': {'status': 'completed', 'claims': [fact('合成数据最低分为500', 'fixture:min_score')]},
               'planning': {'status': 'completed', 'claims': [fact('合成数据最低分为600', 'fixture:min_score', 2)]}},
     'duplicate': '', 'conflicting': ['最低分为500', '最低分为600']},
    {'id': 'helper_failure', 'question': '位次匹配与就业规划', 'intent': 'score_risk',
     'roles': {'risk': {'status': 'completed', 'claims': [fact('历史数据仅供参考', 'limitation')]},
               'planning': {'status': 'failed', 'errors': ['synthetic_provider_failure']}},
     'duplicate': '', 'conflicting': []},
    {'id': 'missing_fact', 'question': '位次匹配与就业规划', 'intent': 'score_risk',
     'roles': {'risk': {'status': 'partial', 'missing_facts': ['目标年份没有证据']},
               'planning': {'status': 'partial', 'missing_facts': ['目标年份没有证据']}},
     'duplicate': '', 'conflicting': []},
    {'id': 'complementary', 'question': '位次匹配与就业规划', 'intent': 'score_risk',
     'roles': {'risk': {'status': 'completed', 'claims': [fact('合成记录需要位次', 'requirement')]},
               'planning': {'status': 'completed', 'claims': [
                   {'statement': '结合兴趣选择专业', 'kind': 'advice'}]}},
     'duplicate': '', 'conflicting': []},
]


def render_fixture(role):
    lines = [claim.statement + ''.join(f'[来源{ref.split(":")[1]}]' for ref in claim.evidence_ids)
             for claim in role.claims]
    if role.missing_facts:
        lines.append('尚待核实：' + '；'.join(role.missing_facts))
    return '\n\n'.join(lines)


async def measure_mode(mode):
    rows = []
    orchestrator = AgentOrchestrator('offline-placeholder', role_mode=mode)
    for case in FIXTURES:
        calls = []
        evidence = [{'source_excerpt': '合成测试证据甲'}, {'source_excerpt': '合成测试证据乙'}]

        async def execute(request, agent_type):
            calls.append({'role': agent_type.value, 'structured': request.structured_output})
            role = RoleResult.model_validate(case['roles'][agent_type.value])
            if request.structured_output:
                validate_role_result(role, request.evidence)
            return AgentResponse(agent_type, render_fixture(role), role.status != 'failed',
                                 role_result=role if request.structured_output else None, errors=role.errors)

        orchestrator._execute = execute
        started = time.perf_counter()
        result = await orchestrator.run(Request(case['question'], 'fixture', case['id'],
            intent=IntentCategory(case['intent']), urgency=UrgencyLevel.LOW, evidence=evidence))
        duplicates = max(0, result.response.count(case['duplicate']) - 1) if case['duplicate'] else 0
        visible_conflicts = sum(value in result.response for value in case['conflicting'])
        invalid_refs = [n for n in re.findall(r'\[来源(\d+)\]', result.response) if not 1 <= int(n) <= len(evidence)]
        rows.append({'case_id': case['id'], 'roles': calls, 'logical_role_executions': len(calls),
                     'status': result.collaboration_status, 'response': result.response,
                     'duplicate_occurrences': duplicates, 'conflicting_values_exposed': visible_conflicts,
                     'invalid_reference_count': len(invalid_refs), 'output_chars': len(result.response),
                     'component_latency_ms': round((time.perf_counter()-started)*1000, 3)})
    # No requests were made, but close the constructors' HTTP clients explicitly.
    clients = {id(agent._client): agent._client for pool in orchestrator._pool.values() for agent in pool}
    clients[id(orchestrator._intent_recognizer.client)] = orchestrator._intent_recognizer.client
    for client in clients.values():
        await client.close()
    return {'cases': rows, 'logical_role_executions': sum(r['logical_role_executions'] for r in rows),
            'duplicate_occurrences': sum(r['duplicate_occurrences'] for r in rows),
            'conflicting_values_exposed': sum(r['conflicting_values_exposed'] for r in rows),
            'actual_model_calls': 0, 'actual_tool_calls': 0}


async def offline_report():
    modes = {mode: await measure_mode(mode) for mode in ('single', 'legacy_concat', 'composed')}
    composed = modes['composed']['cases']
    _, messages, budget = fit_context('rules', 'current', 'required CSV row intact',
        history=[{'role': 'user', 'content': 'old '*200}, {'role': 'assistant', 'content': 'answer '*200},
                 {'role': 'user', 'content': 'recent'}, {'role': 'assistant', 'content': 'response'}],
        max_input_tokens=400)
    checks = {'at_most_two_roles': all(len(row['roles']) <= 2 for row in composed),
              'composed_duplicates_removed': modes['composed']['duplicate_occurrences'] == 0,
              'conflict_values_withheld': modes['composed']['conflicting_values_exposed'] == 0,
              'conflict_explicit': next(r for r in composed if r['case_id'] == 'conflicting_numbers')['status'] == 'conflict',
              'helper_failure_explicit': next(r for r in composed if r['case_id'] == 'helper_failure')['status'] == 'partial',
              'references_within_current_evidence': all(row['invalid_reference_count'] == 0 for row in composed),
              'required_context_preserved': 'required CSV row intact' in messages[-1]['content'],
              'old_history_turn_dropped': budget['history_turns_dropped'] == 1}
    return {'mode': 'offline_role_component_ablation', 'benchmark_id': 'roles-synthetic-v1',
            'dataset_sha256': hashlib.sha256(json.dumps(FIXTURES, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            'limitations': ('Scripted role results exercise routing, Composer and context limits only. '
                            'No real model, retrieval, final answer verifier, model quality or production latency is measured. '
                            'Logical role executions are not token costs; conflict detection is by explicit shared fact_key.'),
            'modes': modes, 'context_budget': budget, 'checks': checks, 'passed': all(checks.values())}


async def live_probe():
    """Check the exact Anthropic tool wire protocol and typed JSON on synthetic data."""
    key = os.getenv('ANTHROPIC_API_KEY')
    model = os.getenv('ANTHROPIC_MODEL', 'claude-3-5-sonnet-20241022')
    result = {'mode': 'live_model_capability_probe', 'model': model, 'tool_use': False,
              'tool_result_round_trip': False, 'structured_role_json': False, 'passed': False,
              'limitations': 'One synthetic two-request protocol probe; not a quality benchmark or provider-wide guarantee.',
              'fallback_configuration': {'HELLOHEBUT_TOOL_LOOP': 'false', 'HELLOHEBUT_ROLE_MODE': 'single'}}
    if not key:
        return {**result, 'error_type': 'MissingContainerCredential'}
    manager = MCPToolManager(key, base_url=os.getenv('ANTHROPIC_BASE_URL') or None, model=model)
    manager._client.max_retries = 0
    register_read_tools(manager)
    tools = [tool for tool in manager.schemas_for('general') if tool['name'] == 'request_evidence_read']
    evidence = [{'source_excerpt': '合成专业甲位于测试校区甲。此为软件能力测试，不是真实招生资料。'}]
    schema = json.dumps(RoleResult.model_json_schema(), ensure_ascii=False)
    system = ('这是协议测试。先调用指定工具，收到结果后只输出符合此Schema的JSON，不要Markdown。'
              '事实claim必须引用本轮source:1；fact_key需标明对象和字段且不得为空；'
              '无依据内容进入missing_facts，不得把学校事实包装为advice。Schema：' + schema)
    messages = [{'role': 'user', 'content': '读取source:1（sources=[1]），回答合成专业甲所在校区，事实引用source:1。'}]
    run = RunContext(timeout_s=50, max_models=2, max_tools=1)
    token = CURRENT_RUN.set(run)
    try:
        async with asyncio.timeout(50):
            response = await create_message(manager._client, model=model, max_tokens=600, system=system,
                messages=messages, tools=tools, tool_choice={'type': 'tool', 'name': 'request_evidence_read'})
            blocks = assistant_exchange_blocks(response)
            uses = [block for block in blocks if block['type'] == 'tool_use']
            result['tool_use'] = len(uses) == 1 and uses[0]['name'] == 'request_evidence_read'
            if not result['tool_use']:
                raise ValueError('tool_use_not_supported')
            use = uses[0]
            tool_result = await manager.call_read_only(use['name'], use['input'], 'general', context={'evidence': evidence})
            if not tool_result.success or use['input'] != {'sources': [1]}:
                raise ValueError('tool_schema_contract_failed')
            messages.extend([{'role': 'assistant', 'content': blocks}, {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': use['id'], 'content': bounded_json(tool_result.data)}]}])
            final = await create_message(manager._client, model=model, max_tokens=1200, system=system,
                messages=messages, tools=tools, tool_choice={'type': 'none'})
            result['tool_result_round_trip'] = not any(block['type'] == 'tool_use' for block in assistant_exchange_blocks(final))
            raw = extract_text(final).strip()
            if raw.startswith('```'):
                raw = raw.removeprefix('```json').removeprefix('```').rsplit('```', 1)[0].strip()
            parsed = validate_role_result(RoleResult.model_validate_json(raw), evidence)
            result['structured_role_json'] = (parsed.status == 'completed' and any(
                '测试校区甲' in claim.statement and claim.evidence_ids == ['source:1'] for claim in parsed.claims))
            result['passed'] = all(result[k] for k in ('tool_use', 'tool_result_round_trip', 'structured_role_json'))
    except Exception as exc:
        result['error_type'] = type(exc).__name__  # Do not log provider body/headers or credentials.
        if str(exc) in {'unknown_role_evidence','fact_claim_without_evidence','fact_claim_without_key',
                        'factual_claim_disguised_as_advice','tool_use_not_supported','tool_schema_contract_failed'}:
            result['failure_code'] = str(exc)
    finally:
        result['usage'] = {'model_calls': run.model_calls, 'tool_calls': run.tool_calls,
                           'input_tokens': run.input_tokens, 'output_tokens': run.output_tokens}
        CURRENT_RUN.reset(token)
        await manager._client.close()
    return result


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-probe', action='store_true')
    parser.add_argument('--report')
    args = parser.parse_args()
    report = await (live_probe() if args.live_probe else offline_report())
    report['commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip() if (ROOT/'.git').exists() else 'container'
    report['source_hashes'] = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in [Path(__file__), ROOT/'agents/agent_orchestrator.py', ROOT/'agents/composer.py',
                                          ROOT/'agents/read_tools.py', ROOT/'core/context_budget.py', ROOT/'core/skill_loader.py']}
    default = 'stage6_model_capability.json' if args.live_probe else 'stage6_roles.json'
    path = (ROOT/'experiments/v2/reports'/ (args.report or default)).resolve()
    if not path.is_relative_to((ROOT/'experiments/v2/reports').resolve()):
        raise ValueError('report_must_be_inside_experiment_reports')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'mode': report['mode'], 'passed': report['passed'], 'report': path.name,
                      'error_type': report.get('error_type')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
