"""Per-request budgets and metadata-only traces; no prompts or personal fields on disk."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import time
import uuid


class BudgetExceeded(RuntimeError):
    pass


class RequestConflict(ValueError):
    pass


class ConversationBusy(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


CURRENT_RUN: ContextVar['RunContext | None'] = ContextVar('hellohebut_run', default=None)
PROGRESS_SINK: ContextVar[object] = ContextVar('hellohebut_progress', default=None)
RUN_MODE: ContextVar[str] = ContextVar('hellohebut_mode', default='interactive')


@dataclass
class RunContext:
    timeout_s: float = 50.0
    max_tools: int = 8
    max_models: int = 16
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started: float = field(default_factory=time.monotonic)
    tool_calls: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    events: list = field(default_factory=list)
    knowledge_snapshots: dict = field(default_factory=dict, repr=False)
    memory_revision: int | None = field(default=None, repr=False)
    allow_long_term: bool = True
    usage_sink: object = field(default=None, repr=False)
    mode: str = field(default_factory=lambda: RUN_MODE.get())

    async def persist_usage(self):
        if self.usage_sink:
            await self.usage_sink(self)

    async def emit_progress(self, stage):
        sink = PROGRESS_SINK.get()
        if sink:
            await sink(stage)

    def check_deadline(self):
        if time.monotonic() - self.started >= self.timeout_s:
            raise BudgetExceeded('request_deadline')

    def consume(self, kind: str):
        self.check_deadline()
        attr, limit = ('tool_calls', self.max_tools) if kind == 'tool' else ('model_calls', self.max_models)
        if getattr(self, attr) >= limit:
            raise BudgetExceeded(kind + '_budget')
        setattr(self, attr, getattr(self, attr) + 1)

    @contextmanager
    def span(self, name: str, **metadata):
        started = time.monotonic()
        event = {'stage': name, 'offset_ms': round((started - self.started) * 1000, 2), **metadata}
        try:
            self.check_deadline()
            yield event
            self.check_deadline()
            event.setdefault('status', 'ok')
        except BaseException as exc:
            event['status'] = type(exc).__name__
            raise
        finally:
            event['duration_ms'] = round((time.monotonic() - started) * 1000, 2)
            self.events.append(event)

    def report(self, status: str):
        cost = None
        try:
            input_rate = float(os.getenv('V2_INPUT_PRICE_PER_MILLION', ''))
            output_rate = float(os.getenv('V2_OUTPUT_PRICE_PER_MILLION', ''))
            if all(math.isfinite(v) and v >= 0 for v in (input_rate, output_rate)):
                cost = {'amount': round((self.input_tokens * input_rate + self.output_tokens * output_rate) / 1_000_000, 8),
                        'currency': os.getenv('V2_PRICE_CURRENCY', 'CNY'), 'basis': 'configured_token_rates',
                        'excludes': 'cache discounts, provider adjustments and unreported interrupted calls'}
        except ValueError:
            pass
        return {'schema_version': 1, 'run_id': self.run_id, 'status': status,
                'mode': self.mode if self.allow_long_term else 'evaluation',
                'latency_ms': round((time.monotonic() - self.started) * 1000, 2),
                'tool_calls': self.tool_calls, 'model_calls': self.model_calls,
                'input_tokens': self.input_tokens, 'output_tokens': self.output_tokens, 'estimated_cost': cost,
                'events': self.events}

    def save(self, folder: str | None, status: str):
        from monitor.tracing import record_trace
        record_trace(self.report(status))
        if folder:
            path = Path(folder)
            path.mkdir(parents=True, exist_ok=True)
            (path / (self.run_id + '.json')).write_text(
                json.dumps(self.report(status), ensure_ascii=False, indent=2), encoding='utf-8')
