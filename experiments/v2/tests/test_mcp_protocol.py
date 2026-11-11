"""Real official SDK dispatch plus application trust-boundary regressions."""
import asyncio
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from mcp import Client

from core.runtime import CURRENT_RUN, RunContext
from tooling.mcp_client import ReadOnlyMCPClient, server_parameters
from tooling.mcp_contracts import MAX_RESULT_BYTES, SERVER_ID, TOOL_SPECS, validate_call
from tooling.mcp_server import build_server
from tooling.tool_manager import CircuitBreaker, CircuitState, MCPToolManager, Tool


class Backend:
    def __init__(self):
        self.calls = []
        self.delay = 0
        self.result = {"rows": [], "data_version": "fixture"}

    async def call(self, name, params):
        self.calls.append((name, params))
        await asyncio.sleep(self.delay)
        return self.result


class ProtocolTests(IsolatedAsyncioTestCase):
    async def test_official_sdk_list_call_and_schema(self):
        backend = Backend()
        async with Client(build_server(backend), mode="legacy") as sdk:
            client = ReadOnlyMCPClient(sdk, "mcp_reader")
            self.assertEqual({t.name for t in await client.list_tools()}, set(TOOL_SPECS))
            result = await client.call("admission_data_query", {"query": "河北计算机"})
            self.assertTrue(result["success"])
            self.assertEqual(result["data"]["data_version"], "fixture")
            self.assertEqual(len(backend.calls), 1)
            # Bypass wrapper to prove the server independently rejects invalid args.
            invalid = await sdk.call_tool("knowledge_search", {"query": "q", "delete": True})
            self.assertTrue(invalid.is_error)
            invalid_type = await sdk.call_tool("knowledge_search", {"query": "q", "top_k": "8"})
            self.assertTrue(invalid_type.is_error)
            self.assertEqual(len(backend.calls), 1)

    async def test_timeout_and_oversized_output_fail_closed(self):
        backend = Backend()
        async with Client(build_server(backend, timeout_s=0.01), mode="legacy") as sdk:
            backend.delay = 0.05
            result = await sdk.call_tool("data_version_stats", {})
            self.assertEqual(result.structured_content["error"], "tool_timeout")
            backend.delay = 0
            backend.result = "x" * MAX_RESULT_BYTES
            result = await sdk.call_tool("data_version_stats", {})
            self.assertEqual(result.structured_content["error"], "tool_result_too_large")


class BoundaryTests(TestCase):
    def test_fixed_local_server_and_no_secret_inheritance(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "do-not-inherit", "MYSQL_PASSWORD": "do-not-inherit"}):
            params = server_parameters(SERVER_ID, "mcp_reader")
        self.assertEqual(params.args, ["-m", "tooling.mcp_server"])
        self.assertNotIn("ANTHROPIC_API_KEY", params.env)
        self.assertNotIn("MYSQL_PASSWORD", params.env)
        for server in ("https://evil.invalid/mcp", "cmd /c whoami", "unknown"):
            with self.assertRaises(ValueError):
                server_parameters(server, "mcp_reader")
        with self.assertRaises(ValueError):
            server_parameters(SERVER_ID, "admin")
        with self.assertRaises(ValueError):
            validate_call("request_evidence_read", {}, "mcp_reader")

    def test_half_open_allows_exactly_one_probe(self):
        breaker = CircuitBreaker(failure_threshold=1, recovery_s=0)
        breaker.record_failure()
        self.assertTrue(breaker.allow())
        self.assertEqual(breaker.state, CircuitState.HALF_OPEN)
        self.assertFalse(breaker.allow())
        breaker.record_success()
        self.assertTrue(breaker.allow())


class ManagerTests(IsolatedAsyncioTestCase):
    async def test_budget_is_durable_before_execution_and_cache(self):
        manager = MCPToolManager(api_key="test")
        events = []
        async def persist(run):
            events.append(("persist", run.tool_calls))
        async def handler(params, context):
            events.append(("execute", None))
            return {"ok": True}
        manager.register(Tool("read", "read", handler, {"type": "object"}, cache_ttl=60))
        run = RunContext(usage_sink=persist)
        token = CURRENT_RUN.set(run)
        try:
            await manager.call("read", {})
            cached = await manager.call("read", {})
            self.assertTrue(cached.cached)
            self.assertEqual(events, [("persist", 1), ("execute", None), ("persist", 2)])
            # Private request-loop calls neither read nor populate the shared cache.
            await manager.call_read_only("read", {}, "policy")
            self.assertEqual(events[-2:], [("persist", 3), ("execute", None)])
        finally:
            CURRENT_RUN.reset(token)
            await manager._client.close()

    async def test_role_override_schema_copy_and_output_limit(self):
        manager = MCPToolManager(api_key="test")
        calls = []
        async def handler(params, context):
            calls.append(context["agent_type"])
            return params["value"]
        tool = Tool("safe", "safe", handler, {"type": "object", "properties": {
            "value": {"type": "string"}}, "required": ["value"], "additionalProperties": False},
                    allowed_agents=("policy",), max_result_bytes=1024)
        manager.register(tool)
        self.assertEqual(manager.schemas_for("risk"), [])
        exported = manager.schemas_for("policy")
        exported[0]["input_schema"]["properties"].clear()
        self.assertIn("value", tool.schema["properties"])
        denied = await manager.call_read_only("safe", {"value": "ok"}, "risk", {"agent_type": "policy"})
        self.assertFalse(denied.success)
        self.assertEqual(calls, [])
        allowed = await manager.call_read_only("safe", {"value": "ok"}, "policy", {"agent_type": "risk"})
        self.assertTrue(allowed.success)
        oversized = await manager.call_read_only("safe", {"value": "x" * 2048}, "policy")
        self.assertEqual(oversized.error, "tool_result_too_large")
        await manager._client.close()
