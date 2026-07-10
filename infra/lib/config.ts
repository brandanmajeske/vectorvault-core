/**
 * Shared infrastructure constants for VectorVault (PR 1).
 *
 * These names form a **stable contract** between the CDK infra (TypeScript) and
 * the Python memory client (PR 2), which reads them back from SSM Parameter Store
 * (claude-review.md Q5). Treat every value here as append-only: renaming a bucket,
 * index, table, or SSM path is a breaking change for the client.
 *
 * One-way-door values (immutable after first deploy — implementation-plan.md PR 1
 * checklist) are called out inline.
 */

/** Applied as a resource tag on every resource for Cost Explorer verification of the $20 cap. */
export const PROJECT_TAG_KEY = 'project';
export const PROJECT_TAG_VALUE = 'vectorvault';

/** S3 Vectors bucket. Vector-bucket names are unique per account per Region (not global). */
export const VECTOR_BUCKET_NAME = 'agent-memory-store';

/**
 * Standard S3 content bucket (dual-store overflow, design-doc §2). Standard S3 bucket
 * names are *globally* unique, so the physical name is suffixed with account+Region at
 * deploy time; the Python client resolves it from SSM rather than hardcoding it.
 */
export const CONTENT_BUCKET_NAME_PREFIX = 'agent-memory-content';
export const TRAIL_LOG_BUCKET_NAME_PREFIX = 'vectorvault-cloudtrail-logs';

/** Vector indexes (design-doc §2). Names are part of the client contract. */
export const INDEX_NAMES = {
  shared: 'shared-team-memory',
  planner: 'private-planner',
  researcher: 'private-researcher',
} as const;

/** All three indexes share one schema (design-doc §2 "Index Schema"). */
export const VECTOR_DIMENSION = 1024;
export const DISTANCE_METRIC = 'cosine';
export const DATA_TYPE = 'float32';

/**
 * ONE-WAY DOOR — non-filterable metadata keys are fixed at index creation and can
 * never be changed (implementation-plan.md PR 1 checklist; verified July 2026,
 * claude-review.md appendix #8). Max 10 keys per index; this list has 7.
 *
 * Everything NOT in this list is filterable by default — that is how `origin`,
 * `status`, `task_id`, timestamps, `version`, etc. stay queryable (design-doc §2).
 * Do not add a key here that the read/write paths need to filter on.
 */
export const NON_FILTERABLE_METADATA_KEYS = [
  'content',
  'content_summary',
  'content_ref',
  'content_hash',
  'provenance',
  'supersedes',
  'confidence',
];

/** DynamoDB tables (design-doc §2, plan PR 1). */
export const EMBED_CACHE_TABLE = 'memory-embed-cache';
export const EMBED_CACHE_TTL_ATTRIBUTE = 'ttl_epoch'; // design-doc §4.2 (DynamoDB TTL, 24h)
export const MEMORY_INDEX_TABLE = 'memory-index';
/** GSI backing `list_memories` by task (design-doc §4 — ListVectors has no metadata filters). */
export const MEMORY_INDEX_TASK_GSI = 'task_id-created_at-index';
/** Hard-TTL expiry index (PR 3): PK `index_name`, SK `expires_at` — lets the TTL worker
 *  find expired keys without a full index scan (design-doc §2). */
export const TTL_INDEX_TABLE = 'memory-ttl-index';

/** Bedrock Titan Embeddings v2 — 1024-dim (design-doc §2; verified id, claude-review.md appendix #11). */
export const EMBED_MODEL_ID = 'amazon.titan-embed-text-v2:0';

/**
 * Named resources + metric namespaces shared with the PR 5 monitoring stack.
 * MonitoringStack references these by name (not cross-stack export) so it stays
 * decoupled from the deployed one-way-door MemoryStack (design-doc §7).
 */
export const TTL_WORKER_FUNCTION_NAME = 'vectorvault-ttl-worker';
export const TTL_DLQ_NAME = 'vectorvault-ttl-dlq';
export const ALERTS_TOPIC_NAME = 'vectorvault-alerts';
export const DASHBOARD_NAME = 'VectorVault';

/** CloudWatch custom-metric namespaces. TTL is emitted by ttl_worker.py; CLIENT by
 *  the memory client when built with enable_metrics=True (design-doc §4.2/§7). */
export const METRIC_NAMESPACE_TTL = 'VectorVault/TTL';
export const METRIC_NAMESPACE_CLIENT = 'VectorVault/Client';

/** SSM Parameter Store contract. The Python client reads config from these paths. */
export const SSM_PREFIX = '/vectorvault';

/** Builds a fully-qualified SSM parameter name under the VectorVault prefix. */
export function ssmParam(...segments: string[]): string {
  return [SSM_PREFIX, ...segments].join('/');
}
