# VectorVault

Serverless **shared memory for multi-agent AI systems**, built on Amazon S3 Vectors.
Agents store and semantically retrieve persistent facts, decisions, and task state
across sessions under a **hard monthly cost cap** (configurable: `-c budgetUsd=…`,
default $20).

- **Architecture:** [`design-doc.md`](design-doc.md) (v1.6)
- **Delivery plan:** [`implementation-plan.md`](implementation-plan.md) (v1.4) — five incremental PRs
- **Open review backlog:** [`claude-review.md`](claude-review.md) (P2 items, per-PR "still to add" notes)

---

## Memory client (Python)

`src/vectorvault/` — the library agents use to read/write shared memory (design-doc
§2/§4). It resolves its config from the `/vectorvault/*` SSM parameters PR 1 publishes,
so no ARNs are hardcoded.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests
pytest tests/unit -q          # mocked boto3 — no AWS credentials needed
```

```python
import boto3
from vectorvault import Config, MemoryClient

ssm = boto3.client("ssm", region_name="us-west-2")
client = MemoryClient.from_config(Config.from_ssm(ssm), agent_id="planner")

client.store_memory(
    "Q2 revenue grew 12% YoY",
    {"team_id": "research-alpha", "task_id": "q2", "memory_type": "semantic"},
)
hits = client.retrieve_memory("how did Q2 revenue do?", filters={"task_id": "q2"})
```

Agents run under their per-role IAM identity, assumed with `roleSessionName = agent_id`
for CloudTrail attribution. Retrieved memories are **data, not
instructions** — treat `origin: external` results with elevated skepticism (design-doc §5).

### Agent tools (PR 4)

`vectorvault.tools` wraps the client as agent-callable tools — the six verbs
`retrieve_memory`, `store_memory`, `list_memories`, `restore_memory`, `get_memory`,
`archive_memory` — and renders them for each framework:

```python
from vectorvault.tools import create_memory_tools, to_anthropic, execute_tool, memory_client_for_agent

# Build the client under the agent's IAM role (roleSessionName = agent_id, for CloudTrail).
client = memory_client_for_agent("planner", "planner-1", config, role_arn=planner_role_arn)

tools = create_memory_tools("planner", client)
anthropic_tools = to_anthropic(tools)             # also: to_openai(tools), to_langchain(tools, client)
result = execute_tool(tools, client, "store_memory", {"content": "...", "metadata": {...}})
```

The system prompt in `prompts/system_memory.md` (parameterized by `{team_id}`/`{agent_id}`/
`{index}`) teaches citation of memory keys and the data-not-instructions trust model.
`scripts/smoke_test.py` runs a planner-store → researcher-retrieve check against a live stack.

### Keyless CLI for CLI agents (`vv`)

VectorVault needs **no LLM API key** — only AWS credentials (embeddings run on Bedrock
via IAM). So CLI-based agents (Claude Code, a Grok CLI, a human) share memory just by
shelling out to `scripts/vv.py` — each CLI *is* its own LLM:

```bash
AWS_PROFILE=<your-profile> .venv/bin/python scripts/vv.py --role planner --agent-id claude-code \
  store "Decision: benchmark providers on \$/kg." --team acme --task q2 --type procedural
AWS_PROFILE=<your-profile> .venv/bin/python scripts/vv.py --role researcher --agent-id grok-cli \
  retrieve "what's the plan?" --task q2
```

`--role` assumes the scoped IAM role (`RoleSessionName = agent_id`, for CloudTrail).
Full walkthrough + agent instructions: **[docs/using-with-cli-agents.md](docs/using-with-cli-agents.md)**.

### MCP server (native tools)

For MCP-capable agents (Claude Code, Claude Desktop, …), `pip install -e ".[mcp]"` adds a
`vectorvault-mcp` server that exposes the six memory verbs as **native tools** over stdio —
still keyless (AWS creds only). Register it in `.mcp.json` with `VECTORVAULT_ROLE` /
`VECTORVAULT_AGENT_ID` env; setup is in the [runbook](docs/using-with-cli-agents.md#native-tools-via-mcp-recommended-for-mcp-capable-agents).

### Memory Galaxy (visualize the vault)

`scripts/vv_galaxy.py` renders the whole shared index as an interactive **3D starfield**
— every star a memory, positioned by semantic similarity, colored by author, with a
Rust→WASM camera core (orbit/pan/fly by mouse or keyboard). Self-contained HTML, read
under the auditor role, no extra deps:

```bash
AWS_PROFILE=<your-profile> .venv/bin/python scripts/vv_galaxy.py    # → ./galaxy-out, opens browser
vv --galaxy                                               # same, with scripts/vv on your PATH
```

See [docs/memory-galaxy.md](docs/memory-galaxy.md).

### Lifecycle & TTL worker (PR 3)

`src/vectorvault/ttl_worker.py` runs on a daily EventBridge schedule (Python Lambda)
and advances the status lifecycle from the vector metadata (the source of truth):

```
superseded --7d--> archived --30d--> deleted   ;   expires_at <= now --> deleted
```

It also runs a **reconciliation sweep** to repair `memory-index` drift, and is guarded
by a **deletion circuit breaker** (aborts if a run would delete > max(1000, 5% of index)),
**`DRY_RUN`** (defaults **on** — logs intended actions, deletes nothing), and an **SQS
DLQ**. `MemoryClient.purge_memory(canonical_id)` is the admin hard-delete across vector
index + S3 content + `memory-index` (compliance / data-subject deletion; requires
`DeleteVectors` — not an agent-facing tool).

> **Enabling real deletion.** `DRY_RUN` defaults to `true`, so the first deploy is a safe
> no-op. Enable real deletion durably by redeploying with the context flag — this survives
> future deploys (unlike a one-off `aws lambda update-function-configuration`):
>
> ```bash
> cd infra && AWS_PROFILE=<your-profile> npx cdk deploy VectorVaultMemoryStack -c ttlDryRun=false
> ```
>
> The Python 3.12 runtime's bundled boto3 already speaks `s3vectors` (verified in the live
> stack), so no extra layer is needed. If a future runtime regresses, `scripts/build_boto3_layer.sh`
> publishes a `boto3>=1.43.31` layer to attach via `-c boto3LayerArn="$ARN"`.

### Monitoring & alarms (PR 5)

`infra/lib/monitoring-stack.ts` (`VectorVaultMonitoringStack`) adds the CloudWatch
dashboard + design-doc §7 alarms, all notifying the `vectorvault-alerts` SNS topic. It
references resources by name and reads the topic ARN from SSM, so it is **decoupled** from
the one-way-door `VectorVaultMemoryStack` and deploys independently:

```bash
cd infra && AWS_PROFILE=<your-profile> npx cdk deploy VectorVaultMonitoringStack
```

Alarms: TTL Lambda errors, TTL DLQ depth, TTL deletion spike (circuit-breaker floor),
Bedrock embedding errors, S3 Vectors 429 rate, QueryVectors p95 latency, injection-suspect
rate, and embedding cache hit-rate. The `VectorVault/Client` alarms consume opt-in custom
metrics — build the client with `MemoryClient.from_config(..., enable_metrics=True)` (needs
`cloudwatch:PutMetricData`, already granted to the agent roles) to activate them; they
treat missing data as non-breaching until then. The AWS budget alarm stays in PR 1.

End-to-end tests live in `tests/integration/` (opt-in, not in CI):

```bash
VECTORVAULT_RUN_INTEGRATION=1 AWS_PROFILE=<your-profile> pytest tests/integration -q
```

---

## PR 1 — Infrastructure (merged)

TypeScript CDK stack (`infra/`) that provisions everything the Python memory client
(PR 2) depends on. Single stack: `VectorVaultMemoryStack`.

| Resource | Detail |
|---|---|
| **KMS key** | `alias/vectorvault-memory`, rotation on; SSE-KMS for vector bucket, content bucket, DynamoDB; key policy grants `kms:Decrypt`/`GenerateDataKey`/`DescribeKey` to `indexing.s3vectors.amazonaws.com` |
| **Vector bucket** | `agent-memory-store` (`AWS::S3Vectors::VectorBucket`, SSE-KMS with full key ARN) |
| **Vector indexes** | `shared-team-memory`, `private-planner`, `private-researcher` — 1024-dim, cosine, float32; 7 non-filterable metadata keys |
| **Content bucket** | `agent-memory-content-<account>-<region>` (standard S3, SSE-KMS, Block Public Access, versioned, TLS-only) |
| **DynamoDB** | `memory-embed-cache` (PK `content_hash`, TTL `ttl_epoch`); `memory-index` (PK `canonical_id`, GSI `task_id-created_at-index`) — both PAY_PER_REQUEST, CMK, PITR |
| **IAM roles** | `MemoryPlannerRole`, `MemoryResearcherRole`, `MemoryAuditorRole`, `MemoryTtlRole` — index-scoped per design-doc §5 |
| **CloudTrail** | `vectorvault-audit-trail` — write-only S3 Vectors data events (`readOnly=false`) on the vector bucket + management events; authoritative audit trail (~$0.30/mo) |
| **AWS Budget** | configurable hard monthly cap (`-c budgetUsd`, default $20; auto-named); alerts at 80% ($16, ACTUAL) and 100% (FORECASTED) to a **direct email** subscriber (no confirmation needed) **and** the SNS topic |
| **SNS** | `vectorvault-alerts` topic + email subscription — fan-out hook reused by PR 5 monitoring |
| **SSM** | `/vectorvault/*` config contract read by the Python client — see below |

Security posture (design-doc §5, claude-review S1–S10): index isolation is the only
IAM boundary (metadata filters are **not**); attribution is via CloudTrail +
`roleSessionName = agent_id`, not client-asserted metadata; the Auditor role gets **no**
embedding-cache access; content-bucket access is append-only (`Put`/`Get`) and
prefix-scoped per role.

---

## Prerequisites

- **Node.js 20+** and npm (CI pins Node 20).
- **AWS account + credentials** for deploy (`aws configure` / SSO). Synth needs none.
- **Region: `us-west-2`** (design-doc §Deployment Region). Deploys follow the active profile’s Region — set your profile’s default Region to us-west-2.
- **CDK bootstrap** in the target account/Region once: `npx cdk bootstrap`.
- **Bedrock model access** for `amazon.titan-embed-text-v2:0` must be enabled in the
  account (Bedrock console → Model access) before the client (PR 2) can embed.

## Dev workflow

```bash
cd infra
npm ci                 # install pinned deps (CI uses this)
npx cdk synth --quiet  # compile + synthesize CloudFormation (no AWS creds needed)
npx cdk diff           # preview changes against the deployed stack
npx cdk deploy         # deploy (requires credentials + bootstrap)
```

CI (`.github/workflows/ci.yml`) runs `npm ci` + `cdk synth --quiet` on every PR.

### Context flags

| Flag | Default | Purpose |
|---|---|---|
| `-c retainData=true` | `false` | RETAIN data stores + KMS key on `cdk destroy` (use for prod). Default DESTROYs them for clean/cheap dev teardown. |
| `-c trustedPrincipalArn=<arn>` | account root | Who may assume the agent roles. Narrow to the specific compute role in prod (claude-review Q6). |
| `-c alertEmail=<addr>` | `alerts@example.com` | Alert address. Budget alerts email it **directly** (no confirmation). The SNS topic's separate email subscription needs a one-time confirmation click (used by PR 5 alarm fan-out). |

---

## ⚠️ One-way-door checklist — review BEFORE first `cdk deploy`

These settings are **immutable after creation**; a wrong first deploy means destroying
and recreating the vector bucket and re-ingesting all vectors (design-doc §5,
implementation-plan.md PR 1). All are encoded in `infra/lib/` and verified in `cdk synth`:

- [x] Vector-bucket encryption uses the **full KMS key ARN** (`Fn::GetAtt … Arn`), same-Region key, `sseType: aws:kms`.
- [x] KMS key policy grants `kms:Decrypt` to `indexing.s3vectors.amazonaws.com`.
- [x] `nonFilterableMetadataKeys` is final: `content`, `content_summary`, `content_ref`, `content_hash`, `provenance`, `supersedes`, `confidence` (7 of max 10).
- [x] Filterable schema is final — `origin`, `status`, `task_id`, `agent_id`, timestamps, `version`, etc. stay filterable by **not** being in the list above.
- [x] CloudTrail write-only data events enabled on the vector bucket.

Change these keys only in `infra/lib/config.ts` and only before the first deploy.

## Post-deploy verification

```bash
# Confirm each index has the expected schema (per PR 1 validation step):
aws s3vectors get-index --vector-bucket-name agent-memory-store --index-name shared-team-memory
# Expect: dimension 1024, distanceMetric cosine, dataType float32, the 7 non-filterable keys.

# Confirm the SSM config contract resolved:
aws ssm get-parameters-by-path --path /vectorvault --recursive \
  --query 'Parameters[].Name' --output text
```

## SSM config contract (`/vectorvault/*`)

The Python client (PR 2) reads config from SSM instead of hardcoding ARNs. Names are a
stable contract (claude-review Q5) — see `infra/lib/config.ts`:

```
/vectorvault/region
/vectorvault/vector-bucket-name          /vectorvault/vector-bucket-arn
/vectorvault/content-bucket-name
/vectorvault/index/{shared-team-memory,private-planner,private-researcher}-{name,arn}
/vectorvault/table/memory-embed-cache    /vectorvault/table/memory-index
/vectorvault/table/memory-index-task-gsi
/vectorvault/embed-model-id              /vectorvault/kms-key-arn
/vectorvault/alerts-topic-arn
/vectorvault/role/{planner,researcher,auditor,ttl}-arn
```

## Teardown

```bash
cd infra && npx cdk destroy
```

**Data-loss caveat:** with the default `retainData=false`, `cdk destroy` deletes the
vector bucket (all vectors), content bucket, and DynamoDB tables. The KMS key is
*scheduled* for deletion (grace window). Deploy with `-c retainData=true` for any stack
whose memory you need to keep. S3 Vectors has no undo for `DeleteVectors`; DynamoDB PITR
and content-bucket versioning are the only vector-adjacent recovery levers.

---

## Deploying to your account

Point your AWS profile at the target account (Region `us-west-2` by default — see the
one-way-door checklist first), then:

```bash
cd infra && npm ci
npx cdk bootstrap                                   # once per account/region
npx cdk deploy --all \
  -c alertEmail=you@company.com \
  -c budgetUsd=20 \
  -c ttlDryRun=true                                 # keep dry-run ON until validated
```

Then run the post-deploy verification above. Recommended hardening once validated:
narrow role assumption with `-c trustedPrincipalArn=<ARN pattern>` (see design-doc §5)
and flip `-c ttlDryRun=false` to enable real TTL deletion.
