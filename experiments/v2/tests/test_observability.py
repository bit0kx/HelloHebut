import json
import os
import unittest
from unittest.mock import patch

import httpx

from core.runtime import RunContext
from evaluation.manifest import benchmark_manifest
from monitor.tracing import TraceExporter


class ObservabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_manifest_separates_implementation_from_benchmark(self):
        with patch.dict(os.environ, {'HELLOHEBUT_ROLE_MODE': 'single'}):
            a = benchmark_manifest([], [])
        with patch.dict(os.environ, {'HELLOHEBUT_ROLE_MODE': 'composed'}):
            b = benchmark_manifest([], [])
        self.assertEqual(a['benchmark_id'], b['benchmark_id'])
        self.assertNotEqual(a['run_manifest'], b['run_manifest'])
        self.assertNotEqual(a['benchmark_id'], benchmark_manifest([], [{'query':'different'}])['benchmark_id'])

    def test_private_data_is_not_exported_and_spans_preserve_timing(self):
        report = {'run_id':'a'*32, 'latency_ms':100, 'status':'completed', 'message':'PRIVATE',
                  'events':[{'stage':'tool', 'offset_ms':20, 'duration_ms':30, 'input':'PRIVATE'}]}
        batch = TraceExporter.batch(report)
        self.assertNotIn('PRIVATE', json.dumps(batch))
        root, child = batch['resourceSpans'][0]['scopeSpans'][0]['spans']
        self.assertEqual(child['parentSpanId'], root['spanId'])
        self.assertEqual(int(child['startTimeUnixNano']) - int(root['startTimeUnixNano']), 20_000_000)
        self.assertEqual(int(child['endTimeUnixNano']) - int(child['startTimeUnixNano']), 30_000_000)

    async def test_otlp_partial_rejection_is_not_success(self):
        requests = []
        def transport(request):
            requests.append(request)
            return httpx.Response(200, json={'partialSuccess': {'rejectedSpans':'1'}})
        with patch.dict(os.environ, {'LANGFUSE_HOST':'https://trace.example', 'LANGFUSE_PUBLIC_KEY':'test',
                                     'LANGFUSE_SECRET_KEY':'test'}):
            exporter = TraceExporter(httpx.MockTransport(transport))
        exporter.start()
        exporter.submit({'run_id':'test','latency_ms':1})
        await exporter.queue.join()
        await exporter.close()
        self.assertEqual((exporter.delivered, exporter.failed), (0,1))
        self.assertEqual(requests[0].url.path, '/api/public/otel/v1/traces')
        self.assertEqual(requests[0].headers['x-langfuse-ingestion-version'], '4')

    def test_cost_is_optional_and_currency_explicit(self):
        with patch.dict(os.environ, {'V2_INPUT_PRICE_PER_MILLION':'', 'V2_OUTPUT_PRICE_PER_MILLION':''}):
            self.assertIsNone(RunContext().report('completed')['estimated_cost'])
        with patch.dict(os.environ, {'V2_INPUT_PRICE_PER_MILLION':'2', 'V2_OUTPUT_PRICE_PER_MILLION':'4', 'V2_PRICE_CURRENCY':'CNY'}):
            cost = RunContext(input_tokens=1000, output_tokens=500).report('completed')['estimated_cost']
            self.assertEqual(cost['amount'], .004)
            self.assertEqual(cost['currency'], 'CNY')
