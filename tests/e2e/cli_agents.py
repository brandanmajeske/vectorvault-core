"""Drive real CLI coding agents (Claude Code, Grok) through VectorVault's tools over
MCP — the fully **keyless** path.

Unlike ``agents.py`` (which calls hosted model APIs directly), each agent here is the
actual CLI the user runs. It brings its own model runtime and reaches VectorVault only
through the ``vectorvault-mcp`` server (AWS/IAM + Bedrock, no LLM API key). So this
exercises the exact production path: CLI agent -> MCP -> shared S3 Vectors memory.

Each agent is launched headless (Claude ``-p``, Grok ``-p/--single``) with **only the six
VectorVault memory tools pre-approved** (Claude ``--allowedTools``, Grok ``--allow``) — the
per-action approval gate stays up for everything else; no blanket bypass. The MCP server
assumes the agent's scoped IAM role (``VECTORVAULT_ROLE``) with ``RoleSessionName`` =
``VECTORVAULT_AGENT_ID``, so writes attribute in CloudTrail.

Availability is by binary presence on PATH; the CLI must already be logged in.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# The six memory verbs create_memory_tools() exposes; pre-approved on each runner.
TOOLS = (
    "retrieve_memory", "store_memory", "list_memories",
    "get_memory", "restore_memory", "archive_memory",
)
MCP_NAME = "vectorvault"  # server name in the MCP config; tool ids are mcp__vectorvault__*


@dataclass(frozen=True)
class CliAgent:
    key: str
    label: str
    bin: str
    role: str  # VectorVault IAM role the MCP server assumes (planner | researcher)


CLI_AGENTS: dict[str, CliAgent] = {
    "claude": CliAgent("claude", "Claude Code", "claude", "planner"),
    "grok": CliAgent("grok", "Grok CLI", "grok", "researcher"),
}


def _server_command() -> str:
    """Absolute path to the vectorvault-mcp console script (prefer the repo venv)."""
    venv = Path(".venv/bin/vectorvault-mcp").resolve()
    if venv.exists():
        return str(venv)
    found = shutil.which("vectorvault-mcp")
    if found:
        return found
    raise FileNotFoundError("vectorvault-mcp not found; install the extra: pip install -e '.[mcp]'")


def availability(a: CliAgent) -> tuple[bool, str]:
    """(usable, reason-if-not). Needs the CLI on PATH and the MCP server installed."""
    if shutil.which(a.bin) is None:
        return (False, f"{a.bin} CLI not on PATH")
    try:
        _server_command()
    except FileNotFoundError as exc:
        return (False, str(exc))
    return (True, "")


def available_agents() -> list[str]:
    return [k for k, a in CLI_AGENTS.items() if availability(a)[0]]


@dataclass
class CliRun:
    """Result of one headless agent run."""

    returncode: int
    stdout: str
    stderr: str

    def said(self, needle: str) -> bool:
        """Did the agent's output contain this value (case-insensitive)? For retrieval
        checks: the value is never in the prompt, so it can only appear if the agent
        pulled it back through retrieve_memory."""
        return needle.lower() in self.stdout.lower()


def _server_env(role: str, agent_id: str) -> dict[str, str]:
    """Env for the MCP server subprocess: role + attribution + AWS credential passthrough."""
    env = {"VECTORVAULT_ROLE": role, "VECTORVAULT_AGENT_ID": agent_id}
    for k in ("AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def run_cli(agent_key: str, agent_id: str, system: str, user: str, timeout: int = 240) -> CliRun:
    """Run ``agent_key`` headless on ``user`` with the memory tools wired over MCP."""
    a = CLI_AGENTS[agent_key]
    server = _server_command()
    srv_env = _server_env(a.role, agent_id)
    if a.key == "claude":
        return _run_claude(server, srv_env, system, user, timeout)
    return _run_grok(server, srv_env, system, user, timeout)


def _run(cmd: list[str], cwd: str, timeout: int) -> CliRun:
    p = subprocess.run(
        cmd, cwd=cwd, env=os.environ.copy(), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=timeout,
    )
    return CliRun(p.returncode, p.stdout, p.stderr)


def _run_claude(server: str, srv_env: dict[str, str], system: str, user: str, timeout: int) -> CliRun:
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / ".mcp.json"
        cfg.write_text(json.dumps({"mcpServers": {MCP_NAME: {"command": server, "env": srv_env}}}))
        allowed = [f"mcp__{MCP_NAME}__{t}" for t in TOOLS]
        cmd = [
            "claude", "-p", user,
            "--mcp-config", str(cfg), "--strict-mcp-config",
            "--append-system-prompt", system,
            "--allowedTools", *allowed,  # variadic — keep last
        ]
        return _run(cmd, d, timeout)


def _run_grok(server: str, srv_env: dict[str, str], system: str, user: str, timeout: int) -> CliRun:
    with tempfile.TemporaryDirectory() as d:
        add = ["grok", "mcp", "add", "--scope", "project"]
        for k, v in srv_env.items():
            add += ["-e", f"{k}={v}"]
        add += [MCP_NAME, "--", server]
        reg = subprocess.run(add, cwd=d, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        if reg.returncode != 0:
            return CliRun(reg.returncode, reg.stdout, f"grok mcp add failed: {reg.stderr}")
        cmd = ["grok", "-p", user, "--output-format", "plain", "--system-prompt-override", system]
        for t in TOOLS:
            cmd += ["--allow", f"mcp__{MCP_NAME}__{t}"]
        return _run(cmd, d, timeout)
