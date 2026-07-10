"""Drive real LLMs through VectorVault's memory tools via each provider's NATIVE
tool-calling, for the end-to-end suite.

Three providers, two loops:
- **Claude** (Anthropic Messages API) with ``to_anthropic`` tool schemas.
- **Grok** (xAI, OpenAI-compatible) and **Gemma** (local Ollama, OpenAI-compatible)
  with ``to_openai`` tool schemas — same loop, different ``base_url``/key.

Every call runs through ``vectorvault.tools`` (``create_memory_tools`` / ``execute_tool``),
so this exercises the exact tool surface production agents use. SDK imports are lazy so
the module loads without the optional ``e2e`` extras installed.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from vectorvault import Config, MemoryClient
from vectorvault.tools import create_memory_tools, execute_tool, memory_client_for_agent


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    kind: str  # "anthropic" | "openai"
    model: str
    role: str  # VectorVault IAM role the agent assumes (planner | researcher)
    base_url: str | None = None
    api_key_env: str | None = None  # None => no key (local Ollama)


def _providers() -> dict[str, Provider]:
    ollama = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    return {
        "claude": Provider("claude", "Claude", "anthropic",
                           os.environ.get("CLAUDE_MODEL", "claude-opus-4-8"), "planner",
                           api_key_env="ANTHROPIC_API_KEY"),
        "grok": Provider("grok", "Grok", "openai",
                         os.environ.get("GROK_MODEL", "grok-4"), "researcher",
                         base_url="https://api.x.ai/v1", api_key_env="XAI_API_KEY"),
        "gemma": Provider("gemma", "Gemma", "openai",
                          os.environ.get("GEMMA_MODEL", "gemma4:12b"), "researcher",
                          base_url=ollama + "/v1", api_key_env=None),
    }


PROVIDERS: dict[str, Provider] = _providers()


def availability(p: Provider) -> tuple[bool, str]:
    """(usable, reason-if-not). Key-based providers need their env var; the local
    Ollama provider needs the daemon reachable with its model pulled."""
    if p.api_key_env:
        return (bool(os.environ.get(p.api_key_env)), f"{p.api_key_env} not set")
    tags = (p.base_url or "").replace("/v1", "/api/tags")
    try:
        with urllib.request.urlopen(tags, timeout=3) as resp:
            names = [m.get("name") for m in json.load(resp).get("models", [])]
        return (p.model in names, f"Ollama model {p.model!r} not pulled (have {names})")
    except Exception as exc:  # daemon down / unreachable
        return (False, f"Ollama not reachable at {p.base_url}: {exc}")


def available_keys() -> list[str]:
    return [k for k, p in PROVIDERS.items() if availability(p)[0]]


@dataclass
class Turn:
    """What a provider did: the tool calls it made (with results) and its final text."""

    calls: list[dict[str, Any]] = field(default_factory=list)  # {name, args, result}
    final: str = ""

    def call_names(self) -> list[str]:
        return [c["name"] for c in self.calls]

    def results_for(self, name: str) -> list[Any]:
        return [c["result"] for c in self.calls if c["name"] == name]


def _client_for(p: Provider, config: Config, agent_id: str) -> MemoryClient:
    import boto3

    ssm = boto3.client("ssm", region_name=config.region)
    arn = ssm.get_parameter(Name=f"/vectorvault/role/{p.role}-arn")["Parameter"]["Value"]
    return memory_client_for_agent(p.role, agent_id, config, role_arn=arn)


def run(provider_key: str, config: Config, agent_id: str, system: str, user: str, max_turns: int = 6) -> Turn:
    """Drive ``provider_key`` through a tool-use loop for the ``user`` task, under the
    agent's assumed IAM role. Returns a :class:`Turn` with the tool calls + final text."""
    p = PROVIDERS[provider_key]
    client = _client_for(p, config, agent_id)
    tools = create_memory_tools(p.role, client)  # type: ignore[arg-type]
    if p.kind == "anthropic":
        return _run_anthropic(p, tools, client, system, user, max_turns)
    return _run_openai(p, tools, client, system, user, max_turns)


def _exec(tools, client, name, args, turn: Turn) -> str:
    result = execute_tool(tools, client, name, args)
    turn.calls.append({"name": name, "args": args, "result": result})
    return json.dumps(result, default=str)


def _run_anthropic(p, tools, client, system, user, max_turns) -> Turn:
    from anthropic import Anthropic

    from vectorvault.tools import to_anthropic

    api = Anthropic()  # ANTHROPIC_API_KEY from env
    schema = to_anthropic(tools)
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    turn = Turn()
    for _ in range(max_turns):
        resp = api.messages.create(model=p.model, max_tokens=1024, system=system, tools=schema, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        for block in resp.content:
            if block.type == "text" and block.text.strip():
                turn.final = block.text
        if resp.stop_reason != "tool_use":
            break
        results = [
            {"type": "tool_result", "tool_use_id": b.id, "content": _exec(tools, client, b.name, b.input, turn)}
            for b in resp.content if b.type == "tool_use"
        ]
        messages.append({"role": "user", "content": results})
    return turn


def _run_openai(p, tools, client, system, user, max_turns) -> Turn:
    from openai import OpenAI

    from vectorvault.tools import to_openai

    api = OpenAI(base_url=p.base_url, api_key=(os.environ.get(p.api_key_env) if p.api_key_env else "ollama"))
    schema = to_openai(tools)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    turn = Turn()
    for _ in range(max_turns):
        resp = api.chat.completions.create(model=p.model, messages=messages, tools=schema, tool_choice="auto", temperature=0)
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if msg.content and msg.content.strip():
            turn.final = msg.content
        if not msg.tool_calls:
            break
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            out = _exec(tools, client, tc.function.name, args, turn)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
    return turn
