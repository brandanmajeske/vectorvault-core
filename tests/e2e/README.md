# End-to-end tests

Real agents drive VectorVault's memory tools against the **deployed stack**. There are
two complementary harnesses, both opt-in via `VECTORVAULT_RUN_E2E=1` and both proving
that **heterogeneous agents share one memory index**:

| Harness | Files | Agents | Path exercised |
|---|---|---|---|
| **CLI over MCP** (keyless) | `cli_agents.py`, `test_cli_memory.py` | Claude Code, Grok CLI | agent → `vectorvault-mcp` (MCP) → S3 Vectors |
| **API tool-loop** | `agents.py`, `test_multimodel_memory.py` | Gemma (local), + hosted Claude/Grok if keyed | model → `vectorvault.tools` → S3 Vectors |

> **VectorVault itself never needs an LLM API key.** It authenticates to AWS (IAM) and
> embeds via Bedrock — an agent reaches it over AWS alone.

---

## 1. CLI-over-MCP tests — fully keyless, production-faithful

`test_cli_memory.py` launches the **actual CLIs you run** (`claude`, `grok`) headless. Each
brings its own model runtime and reaches VectorVault **only through the `vectorvault-mcp`
server** — exactly how a real agent uses it. No LLM API key is involved anywhere.

Only the six VectorVault memory tools are pre-approved (Claude `--allowedTools`, Grok
`--allow`); the per-action approval gate stays up for everything else. The MCP server
assumes the agent's scoped IAM role (`planner` for Claude, `researcher` for Grok) with
`RoleSessionName = <agent>-writer|reader`, so writes attribute in CloudTrail.

| Test | Per agent | Asserts |
|---|---|---|
| `test_cli_store_roundtrip` | ✓ | agent stores via MCP; a fresh client finds it, attributed to the agent |
| `test_cli_retrieve_seeded` | ✓ | agent surfaces a pre-seeded nonce it could only have retrieved |
| `test_cli_cross_agent_sharing` | (≥2 CLIs) | Claude Code writes a decision; Grok retrieves it (and vice-versa) |

Assertions check the **side effect in the shared store** (writes) and a **paraphrase-proof
nonce** the agent surfaces (reads), so they're independent of any CLI's output format.

```bash
pip install -e ".[mcp]"          # the vectorvault-mcp server
VECTORVAULT_RUN_E2E=1 AWS_PROFILE=<your-profile> pytest tests/e2e/test_cli_memory.py -q
```

Each agent **auto-skips** unless its CLI is on `PATH`; the CLI must already be logged in.
Runs consume your Claude / Grok CLI session (subscription), not a per-token API key.

## 2. API tool-loop tests — Gemma keyless by default

`test_multimodel_memory.py` drives models through each provider's native tool-calling on
`vectorvault.tools` (`create_memory_tools` / `execute_tool`) — the same tool surface the
CLIs reach over MCP. **Gemma** runs locally via Ollama (keyless) and is the default path;
**Claude/Grok** here call the *hosted* APIs, so they need a key and are optional — a
convenience for exercising those models from pytest, not something VectorVault requires.

| Test | Per provider | Asserts |
|---|---|---|
| `test_store_roundtrip` | ✓ | the model calls `store_memory`; a fresh client retrieves the fact, attributed to the agent |
| `test_retrieve_seeded` | ✓ | a pre-seeded fact is found by the model via `retrieve_memory` |
| `test_cross_model_sharing` | (≥2 providers) | one model writes a decision; every other available model retrieves it |

| Provider | Needs | Model (env override) |
|---|---|---|
| **Gemma** | a running **Ollama** with the model pulled | `GEMMA_MODEL` (default `gemma4:12b`), `OLLAMA_BASE_URL` (default `http://localhost:11434`) |
| **Claude** *(optional)* | `ANTHROPIC_API_KEY` | `CLAUDE_MODEL` (default `claude-opus-4-8`) |
| **Grok** *(optional)* | `XAI_API_KEY` | `GROK_MODEL` (default `grok-4`) |

```bash
pip install -e ".[e2e]"          # anthropic + openai SDKs
VECTORVAULT_RUN_E2E=1 AWS_PROFILE=<your-profile> pytest tests/e2e/test_multimodel_memory.py -q
```

---

Neither harness runs in CI (`testpaths = ["tests/unit"]`). Both use the shared `config` /
`task` fixtures in `conftest.py`, which skip gracefully if no stack is reachable and
admin-purge everything written under the test's `task_id` in teardown.
