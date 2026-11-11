"""A fixed local subprocess allowlist, not a generic URL/command MCP connector."""
from contextlib import asynccontextmanager
import asyncio
import os
from pathlib import Path
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from tooling.mcp_contracts import (ALLOWED_ROLES, CALL_TIMEOUT_S, MAX_RESULT_BYTES, RESULT_SCHEMA, SERVER_ID,
                                   TOOL_SPECS, bounded_json, schema_for, validate_call, validate_result)

_ENV_ALLOWLIST = ("REDIS_URL", "CHROMA_HOST", "CHROMA_PORT", "V2_KNOWLEDGE_COLLECTION",
                  "ADMISSION_DATA_PATH", "RAG_LEXICAL_MODE", "RAG_FUSION_MODE")


def server_parameters(server_id, role):
    if server_id != SERVER_ID or role not in ALLOWED_ROLES:
        raise ValueError("mcp_server_not_allowed")
    env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
    env.update(HELLOHEBUT_MCP_ROLE=role, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return StdioServerParameters(command=sys.executable, args=["-m", "tooling.mcp_server"],
                                 cwd=str(Path(__file__).resolve().parents[1]), env=env)


class ReadOnlyMCPClient:
    def __init__(self, client, role, timeout_s=CALL_TIMEOUT_S):
        self._client = client
        self._role = role
        self._timeout = timeout_s

    @property
    def protocol_version(self):
        return self._client.protocol_version

    async def list_tools(self):
        async with asyncio.timeout(self._timeout):
            response = await self._client.list_tools()
        tools = response.tools
        if {tool.name for tool in tools} != set(TOOL_SPECS):
            raise ValueError("mcp_tool_inventory_changed")
        for tool in tools:
            if (tool.input_schema != schema_for(tool.name) or tool.output_schema != RESULT_SCHEMA
                    or not tool.annotations or not tool.annotations.read_only_hint):
                raise ValueError("mcp_tool_contract_changed")
        return tools

    async def call(self, name, arguments):
        validate_call(name, arguments, self._role)
        async with asyncio.timeout(self._timeout):
            result = await self._client.call_tool(name, arguments, read_timeout_seconds=self._timeout)
        envelope = result.structured_content
        validate_result(name, envelope)
        # The SDK transports both structuredContent and its text representation.
        bounded_json(result.model_dump(mode="json"), 2 * MAX_RESULT_BYTES + 2048)
        if envelope["success"] == bool(result.is_error):
            raise ValueError("invalid_mcp_response")
        return envelope


@asynccontextmanager
async def connect_read_only(server_id=SERVER_ID, *, role="mcp_reader", timeout_s=CALL_TIMEOUT_S):
    if timeout_s <= 0 or timeout_s > 120:
        raise ValueError("invalid_mcp_timeout")
    parameters = server_parameters(server_id, role)
    # Force the standard initialize/initialized handshake; actual negotiated
    # version is available on the wrapper and recorded by the smoke report.
    client = Client(parameters, mode="legacy", read_timeout_seconds=timeout_s)
    async with asyncio.timeout(timeout_s):
        await client.__aenter__()
    try:
        wrapper = ReadOnlyMCPClient(client, role, timeout_s)
        await wrapper.list_tools()
        yield wrapper
    finally:
        await client.__aexit__(None, None, None)
