"""VectorVault MCP server — exposes shared memory as native tools over the Model
Context Protocol (stdio), so MCP-capable CLI agents (Claude Code, Claude Desktop,
Grok CLI, …) call the six memory verbs directly instead of shelling out to ``vv``.

**Keyless** — like the rest of VectorVault, it needs only AWS credentials (Bedrock
does the embeddings via IAM); no LLM API key. It reuses ``vectorvault.tools`` so the
tool schemas and dispatch are identical to the CLI and framework adapters.

Install the optional dependency and run over stdio::

    pip install -e '.[mcp]'
    AWS_PROFILE=<your-profile> vectorvault-mcp        # console script; == python -m vectorvault.mcp_server

Configuration (environment):
    VECTORVAULT_ROLE            planner | researcher | auditor | none   (default: planner;
                                auditor = read-only tools across all indexes)
    VECTORVAULT_AGENT_ID        CloudTrail RoleSessionName     (default: mcp-agent)
    VECTORVAULT_ENABLE_METRICS  1/true to emit VectorVault/Client metrics (default: off)
    AWS_REGION / AWS_PROFILE    standard AWS credential resolution

Example Claude Code / Desktop MCP config (``.mcp.json``)::

    {"mcpServers": {"vectorvault": {
        "command": "/path/to/repo/.venv/bin/vectorvault-mcp",
        "env": {"AWS_PROFILE": "<your-profile>", "VECTORVAULT_ROLE": "planner",
                "VECTORVAULT_AGENT_ID": "claude-vv"}}}}
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from vectorvault import Config, MemoryClient
from vectorvault.tools import (
    MemoryTool,
    _source_identity,
    create_memory_tools,
    execute_tool,
    memory_client_for_agent,
)

SERVER_NAME = "vectorvault"


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def build_from_env() -> tuple[list[MemoryTool], MemoryClient]:
    """Resolve config from SSM and build the tool set + client from environment.

    ``VECTORVAULT_ROLE`` of planner/researcher/auditor assumes that scoped IAM role
    (so CloudTrail attributes calls to ``VECTORVAULT_AGENT_ID``; auditor serves only
    the read-only tools); ``none`` uses ambient credentials with the planner tool
    surface. Imports no MCP symbols, so this is unit-testable without the optional
    ``mcp`` dependency installed.
    """
    import boto3

    region = os.environ.get("AWS_REGION", "us-west-2")
    role = os.environ.get("VECTORVAULT_ROLE", "planner").strip().lower()
    agent_id = os.environ.get("VECTORVAULT_AGENT_ID", "mcp-agent")
    enable_metrics = _truthy(os.environ.get("VECTORVAULT_ENABLE_METRICS"))

    ssm = boto3.client("ssm", region_name=region)
    config = Config.from_ssm(ssm)

    if role in ("planner", "researcher", "auditor"):
        arn = ssm.get_parameter(Name=f"/vectorvault/role/{role}-arn")["Parameter"]["Value"]
        client = memory_client_for_agent(role, agent_id, config, role_arn=arn, enable_metrics=enable_metrics)
        tool_role = role
    else:
        # Ambient credentials: still stamp the real principal on writes (design-doc §5).
        stored_by = _source_identity(boto3.client("sts", region_name=region).get_caller_identity())
        client = MemoryClient.from_config(
            config, agent_id, stored_by=stored_by, enable_metrics=enable_metrics
        )
        tool_role = "planner"

    return create_memory_tools(tool_role, client), client  # type: ignore[arg-type]


def dispatch(tools: list[MemoryTool], client: MemoryClient, name: str, arguments: dict[str, Any]) -> str:
    """Run a tool call and return its result as a JSON string (the MCP tool result)."""
    return json.dumps(execute_tool(tools, client, name, arguments), default=str)


def main() -> None:
    """Entry point (console script ``vectorvault-mcp``). Serves the tools over stdio.

    NB: stdout is the MCP protocol channel — everything human-readable goes to stderr.
    """
    import anyio
    import mcp.types as types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    tools, client = build_from_env()
    by_name = {t.name: t for t in tools}
    print(
        f"[vectorvault-mcp] serving {len(tools)} tools as agent "
        f"'{client.agent_id}' (role surface: {tools[0].allowed_indexes})",
        file=sys.stderr,
    )

    server: Server = Server(SERVER_NAME)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=t.name, description=t.description, inputSchema=t.input_schema)
            for t in tools
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
        if name not in by_name:
            text = json.dumps({"error": f"unknown tool: {name}", "available": list(by_name)})
        else:
            # boto3 is blocking; run off the event loop so protocol pings stay responsive.
            text = await anyio.to_thread.run_sync(dispatch, tools, client, name, arguments or {})
        return [types.TextContent(type="text", text=text)]

    async def _serve() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(_serve)


if __name__ == "__main__":
    main()
