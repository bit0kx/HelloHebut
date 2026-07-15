"""LLM 调用兼容层：统一请求参数，并安全读取模型最终文本。"""
import logging
import os
from typing import Any, Dict, Mapping

logger = logging.getLogger(__name__)


def _block_value(block: Any, name: str) -> Any:
    if isinstance(block, Mapping):
        return block.get(name)
    return getattr(block, name, None)


def extract_text(response: Any) -> str:
    """提取所有最终文本块，明确跳过 thinking 等非文本内容块。"""
    content = getattr(response, "content", None) or []
    texts = []
    block_types = []
    for block in content:
        block_type = _block_value(block, "type")
        block_types.append(str(block_type or "unknown"))
        if block_type not in (None, "text"):
            continue
        value = _block_value(block, "text")
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())

    if texts:
        return "\n".join(texts)

    stop_reason = getattr(response, "stop_reason", None) or "unknown"
    raise ValueError(
        "模型未返回最终文本"
        f"（内容块: {', '.join(block_types) or 'empty'}，停止原因: {stop_reason}）"
    )


def _deepseek_options(client: Any) -> Dict[str, Any]:
    base_url = str(
        getattr(client, "base_url", "") or getattr(client, "_base_url", "")
    ).lower()
    if "api.deepseek.com" not in base_url:
        return {}

    mode = os.getenv("DEEPSEEK_THINKING_MODE", "disabled").strip().lower()
    if mode not in {"enabled", "disabled"}:
        logger.warning("DEEPSEEK_THINKING_MODE=%s 无效，已使用 disabled", mode)
        mode = "disabled"
    return {"extra_body": {"thinking": {"type": mode}}}


async def create_message(client: Any, **kwargs: Any) -> Any:
    """通过 Anthropic SDK 发起消息请求，并注入供应商兼容参数。"""
    options = _deepseek_options(client)
    if "extra_body" in kwargs:
        options["extra_body"] = {
            **options.get("extra_body", {}),
            **(kwargs.pop("extra_body") or {}),
        }
    return await client.messages.create(**kwargs, **options)
