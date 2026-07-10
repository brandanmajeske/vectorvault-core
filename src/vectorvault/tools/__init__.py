"""Agent-facing tool adapters over :class:`vectorvault.MemoryClient` (PR 4).

``from vectorvault.tools import create_memory_tools`` builds the memory tool set
for an agent role; the format adapters (:func:`to_anthropic`, :func:`to_openai`,
:func:`to_langchain`) render those tools for each framework, and
:func:`execute_tool` dispatches a tool call back to the client.
"""

from __future__ import annotations

from vectorvault.tools.memory_tools import (
    MemoryTool,
    _source_identity,
    create_memory_tools,
    execute_tool,
    memory_client_for_agent,
    to_anthropic,
    to_langchain,
    to_openai,
)

__all__ = [
    "MemoryTool",
    "_source_identity",
    "create_memory_tools",
    "execute_tool",
    "memory_client_for_agent",
    "to_anthropic",
    "to_langchain",
    "to_openai",
]
