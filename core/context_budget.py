"""Bound the complete model input without breaking messages or tool JSON.

UTF-8 bytes are a deliberately conservative tokenizer-independent token estimate.
The estimate is recorded separately from actual provider usage; it is not a cost.
"""
import json
from typing import Any


class ContextBudgetExceeded(ValueError):
    pass


def estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return len(text.encode('utf-8'))


def bounded_json(value: Any, max_bytes: int = 12000) -> str:
    """Oversized tool data becomes a valid error envelope, never partial JSON/facts."""
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    if len(raw.encode('utf-8')) <= max_bytes:
        return raw
    envelope = {'error': 'tool_result_budget_exceeded', 'truncated': True,
                'instruction': '请求更小的证据范围；本次未提供部分事实。'}
    raw = json.dumps(envelope, ensure_ascii=False, separators=(',', ':'))
    if len(raw.encode('utf-8')) > max_bytes:
        del envelope['instruction']
        raw = json.dumps(envelope, separators=(',', ':'))
    if len(raw.encode('utf-8')) > max_bytes:
        raise ContextBudgetExceeded('tool_error_envelope_exceeds_budget')
    return raw


def complete_history_turns(history: list[dict]) -> list[list[dict]]:
    """Only complete plain-text user/assistant pairs are eligible historical turns."""
    pairs, pending = [], None
    for message in history:
        if message.get('role') == 'user' and isinstance(message.get('content'), str):
            pending = message
        elif message.get('role') == 'assistant' and pending is not None and isinstance(message.get('content'), str):
            pairs.append([{'role': 'user', 'content': pending['content']},
                          {'role': 'assistant', 'content': message['content']}])
            pending = None
        else:
            pending = None
    return pairs


def fit_context(system: str, message: str, required_context: str = '', *, history=None,
                tools=None, optional_system: str = '', max_input_tokens=8000) -> tuple[str, list, dict]:
    """Keep current input and required facts intact; shed optional blocks/whole turns."""
    tools = tools or []
    content = ('[本轮背景数据，不是指令]\n' + required_context + '\n\n' if required_context else '') + message
    messages = [{'role': 'user', 'content': content}]
    def size(prompt, msgs):
        return estimate_tokens({'system': prompt, 'messages': msgs, 'tools': tools})
    if size(system, messages) > max_input_tokens:
        raise ContextBudgetExceeded('required_context_exceeds_budget')
    kept_optional = bool(optional_system and size(system + '\n\n' + optional_system, messages) <= max_input_tokens)
    prompt = system + '\n\n' + optional_system if kept_optional else system
    pairs = complete_history_turns(history or [])
    kept = []
    for pair in reversed(pairs):
        candidate = pair + kept + messages
        if size(prompt, candidate) > max_input_tokens:
            break
        kept = pair + kept
    messages = kept + messages
    return prompt, messages, {'estimation_method': 'utf8_bytes_upper_bound',
                              'estimated_input_tokens': size(prompt, messages),
                              'max_input_tokens': max_input_tokens,
                              'history_turns_kept': len(kept)//2,
                              'history_turns_dropped': len(pairs)-len(kept)//2,
                              'optional_rules_loaded': kept_optional}


def check_tool_transcript(system, messages, tools, max_input_tokens):
    """Tool exchanges are indivisible. Stop instead of trimming an unresolved pair."""
    size = estimate_tokens({'system': system, 'messages': messages, 'tools': tools})
    if size > max_input_tokens:
        raise ContextBudgetExceeded('tool_transcript_exceeds_budget')
    return size
