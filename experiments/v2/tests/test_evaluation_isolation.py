"""Evaluation jobs may reuse logical case IDs without sharing persisted dialogs."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest

from evaluation.evaluator import EndToEndEvaluator, QualityScores


class MemoryChatService:
    def __init__(self):
        self.memory = {('shared-user', 'shared-conversation'): ['must remain untouched']}
        self.handles = []
        self.clears = []

    async def clear_conversation(self, user_id, conv_id):
        key = (user_id, conv_id)
        self.clears.append(key)
        self.memory.pop(key, None)
        await asyncio.sleep(0)

    async def handle(self, question, user_id, conv_id, **options):
        key = (user_id, conv_id)
        self.handles.append((key, question, options))
        self.memory.setdefault(key, []).append(question)
        await asyncio.sleep(0)  # Make the two jobs and their cleanup interleave.
        if question == 'raise-fixture':
            raise RuntimeError('synthetic failure')
        return SimpleNamespace(response='|'.join(self.memory.get(key, [])), intent='other',
            agent_type='general', escalated=False, citations=[], admission_data_used=False,
            knowledge_used=False, entities={}, latency_ms=1.0)


class PassingJudge:
    async def judge(self, *_args, **_kwargs):
        return QualityScores(0.8, 0.8, 0.8, 0.8)


def evaluator(service):
    instance = EndToEndEvaluator.__new__(EndToEndEvaluator)
    instance._chat_service = service
    instance._judge = PassingJudge()
    instance._history = []
    instance._baseline = None
    instance._baseline_path = None
    return instance


class EvaluationIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_jobs_with_same_explicit_ids_keep_turns_and_cleanup_isolated(self):
        service = MemoryChatService()
        runner = evaluator(service)
        cases = [{'user_id': 'shared-user', 'conv_id': 'shared-conversation',
                  'turns': [f'{label}-first', f'{label}-second'],
                  'expectations': [{'required_terms': [f'{label}-first']},
                                   {'required_terms': [f'{label}-first', f'{label}-second'],
                                    'forbidden_terms': ['other-job']}]} for label in ('job-a', 'job-b')]
        original = deepcopy(cases)
        reports = await asyncio.gather(*(runner.run(dialog_cases=[case]) for case in cases))
        keys = {key for key, _, _ in service.handles}
        self.assertEqual(len(keys), 2)
        self.assertEqual(len({key[0] for key in keys}), 2)
        self.assertEqual(len({key[1] for key in keys}), 2)
        for label, report in zip(('job-a', 'job-b'), reports):
            self.assertEqual([result.metadata['response'] for result in report.results],
                             [f'{label}-first', f'{label}-first|{label}-second'])
            self.assertEqual(len({result.metadata['conv_id'] for result in report.results}), 1)
            self.assertEqual(report.pass_rate, 1.0)
            self.assertEqual(report.avg_scores['accuracy'], 0.8)
        for key in keys:
            self.assertEqual(service.clears.count(key), 2)
            self.assertNotIn(key, service.memory)
            self.assertNotIn('shared-user', key[0])
            self.assertNotIn('shared-conversation', key[1])
        self.assertEqual(service.memory[('shared-user', 'shared-conversation')], ['must remain untouched'])
        self.assertEqual(cases, original)

    async def test_repeated_default_case_gets_new_ids_and_failed_case_cleans_only_itself(self):
        service = MemoryChatService()
        runner = evaluator(service)
        for _ in range(2):
            await runner._evaluate_dialog_case({'question': 'repeat'}, 0)
        with self.assertRaises(RuntimeError):
            await runner._evaluate_dialog_case({'user_id': 'shared-user', 'conv_id': 'shared-conversation',
                                               'question': 'raise-fixture'}, 0)
        keys = {key for key, _, _ in service.handles}
        self.assertEqual(len(keys), 3)
        self.assertTrue(all(service.clears.count(key) == 2 for key in keys))
        self.assertEqual(service.memory, {('shared-user', 'shared-conversation'): ['must remain untouched']})
