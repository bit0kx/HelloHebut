import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.llm_client import create_message, extract_text


class _Messages:
    def __init__(self):
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(content=[])


class _Client:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.messages = _Messages()


class LLMClientTests(unittest.TestCase):
    def test_extract_text_skips_thinking_and_joins_text_blocks(self):
        response = SimpleNamespace(
            content=[
                SimpleNamespace(type="thinking", thinking="internal", text=None),
                SimpleNamespace(type="text", text="第一段"),
                {"type": "text", "text": "第二段"},
            ],
            stop_reason="end_turn",
        )

        self.assertEqual(extract_text(response), "第一段\n第二段")

    def test_extract_text_rejects_response_without_final_text(self):
        response = SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking="internal", text=None)],
            stop_reason="max_tokens",
        )

        with self.assertRaisesRegex(ValueError, "模型未返回最终文本.*max_tokens"):
            extract_text(response)

    def test_create_message_disables_deepseek_thinking_by_default(self):
        client = _Client("https://api.deepseek.com/anthropic/")
        env = {key: value for key, value in os.environ.items() if key != "DEEPSEEK_THINKING_MODE"}

        with patch.dict(os.environ, env, clear=True):
            asyncio.run(create_message(client, model="deepseek-v4-pro", messages=[], max_tokens=32))

        self.assertEqual(
            client.messages.kwargs["extra_body"],
            {"thinking": {"type": "disabled"}},
        )

    def test_create_message_leaves_anthropic_request_unchanged(self):
        client = _Client("https://api.anthropic.com/")

        asyncio.run(create_message(client, model="claude", messages=[], max_tokens=32))

        self.assertNotIn("extra_body", client.messages.kwargs)


if __name__ == "__main__":
    unittest.main()
