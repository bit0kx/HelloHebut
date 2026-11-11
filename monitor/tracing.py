"""Per-request metrics and opt-in metadata-only Langfuse ingestion.

No prompts, answers, identities, preference values or hidden reasoning leave this exporter.
Delivery is bounded/best effort; local JSON traces remain the diagnostic authority.
"""
import asyncio
import hashlib
import json
import os
import time
from urllib.parse import urlsplit
import uuid

import httpx
from prometheus_client import Counter, Histogram

LATENCY = Histogram('hellohebut_chat_duration_seconds', 'Duration of each chat request', ['mode', 'status'],
                    buckets=(.1, .5, 1, 2, 5, 10, 20, 30, 50, 90))
REQUESTS = Counter('hellohebut_chat_requests_total', 'Completed request attempts', ['mode', 'status'])
_exporter = None


def record_trace(report):
    mode = 'evaluation' if report.get('mode') == 'evaluation' else 'interactive'
    status = report.get('status')
    if status not in {'completed','clarification','insufficient_evidence','failed','replayed','unavailable'}:
        status = 'failed'
    LATENCY.labels(mode, status).observe(max(0, report.get('latency_ms', 0)) / 1000)
    REQUESTS.labels(mode, status).inc()
    if _exporter:
        _exporter.submit(report)


class TraceExporter:
    def __init__(self, transport=None):
        self.transport = transport
        self.base = os.getenv('LANGFUSE_HOST', '').rstrip('/')
        self.public = os.getenv('LANGFUSE_PUBLIC_KEY', '')
        self.secret = os.getenv('LANGFUSE_SECRET_KEY', '')
        self.enabled = bool(self.base and self.public and self.secret)
        if self.enabled:
            url = urlsplit(self.base)
            if not url.hostname or url.username or url.password or url.query or url.fragment or (
                    url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in {'localhost','127.0.0.1','langfuse'})):
                raise ValueError('invalid_langfuse_endpoint')
        self.queue = asyncio.Queue(maxsize=128)
        self.task = None
        self.delivered = self.failed = self.dropped = 0

    def start(self):
        global _exporter
        _exporter = self
        if self.enabled:
            self.task = asyncio.create_task(self._worker())

    def submit(self, report):
        if not self.enabled:
            return
        try:
            self.queue.put_nowait(self.batch(report))
        except asyncio.QueueFull:
            self.dropped += 1

    @staticmethod
    def batch(report):
        end = time.time_ns()
        start = end - int(max(0, report.get('latency_ms', 0)) * 1_000_000)
        run_id = hashlib.sha256(str(report['run_id']).encode()).hexdigest()[:32]
        root_id = uuid.uuid4().hex[:16]
        common = {k: report.get(k) for k in ('status','mode','tool_calls','model_calls','input_tokens','output_tokens','estimated_cost')}
        def attrs(metadata):
            return [{'key': 'langfuse.observation.metadata.' + k,
                     'value': {'stringValue': json.dumps(v, ensure_ascii=False)}}
                    for k,v in metadata.items() if v is not None] + [
                {'key': 'langfuse.observation.type', 'value': {'stringValue': 'span'}}]
        spans = [{'traceId': run_id, 'spanId': root_id, 'name': 'admissions-chat',
                  'startTimeUnixNano': str(start), 'endTimeUnixNano': str(end), 'attributes': attrs(common)}]
        for event in report.get('events', [])[:64]:
            node_start = min(end, start + int(max(0, event.get('offset_ms', 0)) * 1_000_000))
            metadata = {**common, **{k: event[k] for k in ('status','tool','read_only','sufficient','passed','reason_codes') if k in event}}
            spans.append({'traceId': run_id, 'spanId': uuid.uuid4().hex[:16], 'parentSpanId': root_id,
                'name': str(event.get('stage', 'stage'))[:80], 'startTimeUnixNano': str(node_start),
                'endTimeUnixNano': str(min(end, node_start + int(max(0, event.get('duration_ms', 0)) * 1_000_000))),
                'attributes': attrs(metadata)})
        return {'resourceSpans': [{'resource': {'attributes': [
            {'key': 'service.name', 'value': {'stringValue': 'hellohebut'}}]},
            'scopeSpans': [{'scope': {'name': 'hellohebut.metadata'}, 'spans': spans}]}]}

    async def _worker(self):
        async with httpx.AsyncClient(timeout=3, auth=(self.public, self.secret), transport=self.transport,
                                     headers={'x-langfuse-ingestion-version': '4'}) as client:
            while True:
                batch = await self.queue.get()
                try:
                    response = await client.post(self.base + '/api/public/otel/v1/traces', json=batch)
                    response.raise_for_status()
                    data = response.json() if response.content else {}
                    if int(data.get('partialSuccess', {}).get('rejectedSpans', 0)):
                        raise ValueError('trace_partial_rejection')
                    self.delivered += 1
                except Exception:
                    self.failed += 1
                finally:
                    self.queue.task_done()

    async def close(self):
        global _exporter
        _exporter = None
        if self.task:
            try:
                await asyncio.wait_for(self.queue.join(), timeout=3)
            except TimeoutError:
                pass
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
