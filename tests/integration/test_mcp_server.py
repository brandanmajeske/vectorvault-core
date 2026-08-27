"""Opt-in protocol smoke test for the real vectorvault-mcp stdio server.

Run with the deployed stack and MCP extra installed:

    VECTORVAULT_RUN_INTEGRATION=1 AWS_PROFILE=provider-dev \
        pytest tests/integration/test_mcp_server.py -q

The test only initializes the server, lists tools, calls ``whoami`` and the
read-only ``doctor`` tool, and checks an unknown-tool response. It performs no
embedding, memory write, or deletion (doctor runs with probe_data_plane off).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import anyio
import pytest

pytestmark = pytest.mark.integration

if not os.environ.get("VECTORVAULT_RUN_INTEGRATION"):
    pytest.skip(
        "integration tests are opt-in; set VECTORVAULT_RUN_INTEGRATION=1 to run",
        allow_module_level=True,
    )

mcp = pytest.importorskip("mcp")


def _server_command() -> str:
    venv = Path(".venv/bin/vectorvault-mcp").resolve()
    if venv.exists():
        return str(venv)
    found = shutil.which("vectorvault-mcp")
    if found:
        return found
    pytest.skip("vectorvault-mcp is not installed; install with pip install -e '.[mcp]'")


def _server_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("VECTORVAULT_ROLE", "auditor")
    env.setdefault("VECTORVAULT_AGENT_ID", "mcp-smoke")
    return env


def _text(result) -> str:
    assert result.content, "MCP tool returned no content"
    return result.content[0].text


async def _smoke() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=_server_command(),
        args=[],
        env=_server_env(),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            assert {"whoami", "retrieve_memory", "list_memories", "doctor"} <= tools.keys()
            assert all(tool.inputSchema for tool in tools.values())

            identity = json.loads(_text(await session.call_tool("whoami", {})))
            assert identity["agent_id"] == os.environ.get("VECTORVAULT_AGENT_ID", "mcp-smoke")
            assert identity["_meta"]["role"] == os.environ.get("VECTORVAULT_ROLE", "auditor")

            # Doctor is read-only; default probe_data_plane=false keeps it control-plane only.
            report = json.loads(_text(await session.call_tool("doctor", {})))
            assert "healthy" in report and "checks" in report
            assert report["context"]["agent_id"] == os.environ.get("VECTORVAULT_AGENT_ID", "mcp-smoke")
            assert report["_meta"]["role"] == os.environ.get("VECTORVAULT_ROLE", "auditor")
            data_plane = next(c for c in report["checks"] if c["name"] == "data_plane")
            assert data_plane["status"] == "skip"  # not probed → no S3 Vectors read

            unknown = json.loads(_text(await session.call_tool("__mcp_smoke_unknown__", {})))
            assert "unknown tool" in unknown["error"]


def test_mcp_stdio_protocol_smoke():
    anyio.run(_smoke)
