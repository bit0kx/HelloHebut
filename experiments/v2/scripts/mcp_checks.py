"""Run the real allowlisted stdio subprocess against published, read-only data.

Run inside the application environment after unified deployment:
  python experiments/v2/scripts/mcp_checks.py
Writes only experiments/v2/reports/stage6_mcp_protocol.json.
"""
import asyncio
import importlib.metadata
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tooling.mcp_client import connect_read_only


async def run_checks(*, admission_only=False):
    started = time.monotonic()
    checks = []
    async with connect_read_only(timeout_s=90) as client:
        protocol = client.protocol_version
        tools = await client.list_tools()
        before = await client.call("data_version_stats", {}) if not admission_only else None
        requests = (
            ("knowledge_search", {"query": "河北工业大学招生章程", "top_k": 3}),
            ("admission_data_query", {"query": "河北物理2024计算机科学与技术录取最低分"}),
        )
        for name, params in requests:
            if admission_only and name != "admission_data_query":
                continue
            result = await client.call(name, params)
            checks.append({"tool": name, "success": result["success"], "error": result["error"],
                           "data_type": type(result["data"]).__name__})
        if not admission_only:
            after = await client.call("data_version_stats", {})
            checks.append({"tool": "data_version_stats", "success": before["success"] and after["success"],
                           "public_data_unchanged": before["data"] == after["data"]})
        for name, params in (("knowledge_search", {"query": "q", "delete": True}),
                             ("request_evidence_read", {})):
            try:
                await client.call(name, params)
            except ValueError:
                checks.append({"rejected": name, "success": True})
            else:
                checks.append({"rejected": name, "success": False})
        # Test invalid arguments across the actual wire, bypassing our client guard.
        invalid = await client._client.call_tool("knowledge_search", {"query": "q", "delete": True})
        checks.append({"server_rejected_invalid_schema": True, "success": bool(invalid.is_error)})
    return {"sdk": importlib.metadata.version("mcp"), "transport": "stdio",
            "handshake": "initialize/initialized", "protocol_version": protocol,
            "scope": "local_admission_only" if admission_only else "all_public_tools",
            "tools": [tool.name for tool in tools], "checks": checks,
            "success": all(c["success"] and c.get("public_data_unchanged", True) for c in checks),
            "elapsed_s": round(time.monotonic() - started, 3)}


if __name__ == "__main__":
    admission_only = "--admission-only" in sys.argv
    report = asyncio.run(run_checks(admission_only=admission_only))
    filename = "stage6_mcp_stdio_local.json" if admission_only else "stage6_mcp_protocol.json"
    path = ROOT / "experiments/v2/reports" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report["success"] else 1)
