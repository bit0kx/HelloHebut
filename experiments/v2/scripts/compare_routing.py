"""Counterfactual ChatService routing benchmark; scripted recognition/generation, real CSV."""
import asyncio
import json
import hashlib
from pathlib import Path
import subprocess
import sys
import types

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'experiments/v2/tests'))
from core.chat_service import ChatService
from core.intent_recognizer import IntentCategory, IntentResult, UrgencyLevel
from agents.agent_orchestrator import AgentType
from test_chat_integration import Tools


async def measure(service_class, cases):
    rows = []
    for case in cases:
        class Recognizer:
            async def recognize(self, message, history=None):
                return IntentResult(IntentCategory(case['intent']), 1.0, UrgencyLevel.LOW, {}, 'scripted', 0)
        class Orchestrator:
            async def run(self, request):
                return types.SimpleNamespace(response='fixture', intent=request.intent,
                                             agent_type=AgentType.GENERAL, escalated=False, entities=request.entities)
        tools = Tools()
        service = service_class(Orchestrator(), None, tools, recognizer=Recognizer())
        await service.handle(case['question'], 'benchmark', persist_memory=False)
        called = any(name == 'admission_data_query' for name, _ in tools.calls)
        rows.append({'case_id': case['id'], 'csv_called': called, 'passed': called == case['require_csv']})
    return {'passed': sum(r['passed'] for r in rows), 'total': len(rows), 'cases': rows}


async def main():
    baseline = types.ModuleType('baseline_chat')
    sys.modules[baseline.__name__] = baseline
    source = subprocess.check_output(['git', 'show', '8029b40:core/chat_service.py'], cwd=ROOT).decode('utf-8')
    exec(compile(source, 'git:8029b40:core/chat_service.py', 'exec'), baseline.__dict__)
    cases = json.loads((ROOT / 'experiments/v2/datasets/admission_routing.json').read_text(encoding='utf-8'))
    report = {'mode': 'routing_component_counterfactual', 'baseline_commit': '8029b40',
              'dataset_sha256': hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
              'limitations': 'Scripted intent and generation, real CSV. This measures required tool routing, not live model quality.',
              'baseline': await measure(baseline.ChatService, cases), 'v2': await measure(ChatService, cases)}
    path = ROOT / 'experiments/v2/reports/routing_comparison.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: {k: v for k, v in report[key].items() if k != 'cases'} for key in ('baseline', 'v2')}))


if __name__ == '__main__':
    asyncio.run(main())
