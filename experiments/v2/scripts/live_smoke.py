"""Opt-in small live-model contract smoke. Fixture documents, no production memory/Chroma."""
import asyncio
import json
from pathlib import Path
import sys
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from core.runtime import CURRENT_RUN, RunContext
from tooling.tool_manager import MCPToolManager, Tool
from rag.agentic import AgenticRAG


async def main():
    config = dotenv_values(ROOT / '.env')
    if not config.get('ANTHROPIC_API_KEY'):
        raise SystemExit('No configured model credential')
    manager = MCPToolManager(config['ANTHROPIC_API_KEY'], base_url=config.get('ANTHROPIC_BASE_URL'),
                             model=config.get('ANTHROPIC_MODEL', 'claude-3-5-sonnet-20241022'))
    manager._client.max_retries = 0
    fixture = json.loads((ROOT / 'experiments/v2/datasets/policy_fixture.json').read_text(encoding='utf-8'))
    async def search(params, context):
        return [row for row in fixture if not params.get('year') or row['effective_year'] == params['year']]
    manager.register(Tool('knowledge_search', '合成测试资料，仅用于软件测试', search,
                          {'type': 'object', 'required': ['query']}, supports_rerank=True))
    rows = []
    try:
        for question, expected in [('测试专业甲2025年和2026年选科要求有哪些变化？', True),
                                   ('测试专业甲2024年选科要求是什么？', False)]:
            run = RunContext(timeout_s=55, max_models=8)
            token = CURRENT_RUN.set(run)
            try:
                async with asyncio.timeout(55):
                    outcome = await AgenticRAG(manager).retrieve(question)
                rows.append({'question': question, 'expected_sufficient': expected,
                             'sufficient': outcome.sufficient, 'stop_reason': outcome.stop_reason,
                             'rounds': outcome.rounds, 'queries': outcome.queries,
                             'passed': outcome.sufficient == expected, 'trace': run.report('complete')})
            except Exception as exc:
                rows.append({'question': question, 'passed': False, 'error_type': type(exc).__name__})
            finally:
                CURRENT_RUN.reset(token)
    finally:
        await manager._client.close()
    report = {'mode': 'live_model_fixture_smoke', 'model': config.get('ANTHROPIC_MODEL'),
              'limitations': 'Two synthetic-document cases; not full Chroma/API startup or a statistical quality evaluation.',
              'cases': rows, 'passed': sum(row['passed'] for row in rows), 'total': len(rows)}
    path = ROOT / 'experiments/v2/reports/live_smoke.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'total': report['total'],
                      'stop_reasons': [row.get('stop_reason', row.get('error_type')) for row in rows]}))
    return 0 if report['passed'] == report['total'] else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
