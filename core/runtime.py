"""Per-request budgets and metadata-only traces; no prompts or personal fields on disk."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
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
        event = {'stage': name, **metadata}
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
        return {'schema_version': 1, 'run_id': self.run_id, 'status': status,
                'latency_ms': round((time.monotonic() - self.started) * 1000, 2),
                'tool_calls': self.tool_calls, 'model_calls': self.model_calls,
                'input_tokens': self.input_tokens, 'output_tokens': self.output_tokens,
                'events': self.events}

    def save(self, folder: str | None, status: str):
        if folder:
            path = Path(folder)
            path.mkdir(parents=True, exist_ok=True)
            (path / (self.run_id + '.json')).write_text(
                json.dumps(self.report(status), ensure_ascii=False, indent=2), encoding='utf-8')
