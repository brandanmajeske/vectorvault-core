"""TTL worker — status lifecycle + reconciliation (design-doc §2/§7/§9, PR 3).

Runs on an EventBridge schedule. Deliberately boto3-only (no pydantic) so the
Lambda asset stays lean — it operates on raw vector-metadata dicts.

Lifecycle (vector metadata is the source of truth):
    superseded --7d--> archived --30d--> deleted ;  expires_at <= now --> deleted

Safety rails (design-doc §7/§9, claude-review S6):
  * circuit breaker: abort the run if a delete batch exceeds max(1000, 5% of index)
  * DRY_RUN (default true on first deploy): log intended actions, delete nothing
  * idempotent: safe to re-run after a crash (every step re-derives its worklist)
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger("vectorvault.ttl")

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_ARCHIVED = "archived"

SUPERSEDED_GRACE_S = 7 * 86_400
ARCHIVED_GRACE_S = 30 * 86_400

DEFAULT_MAX_DELETE_ABS = 1_000
DEFAULT_MAX_DELETE_PCT = 0.05


class CircuitBreakerError(RuntimeError):
    """Raised when a delete batch would exceed the safety cap — aborts the run."""


class TTLWorker:
    def __init__(
        self,
        *,
        s3vectors,
        memory_index_table,
        ttl_index_table,
        vector_bucket: str,
        indexes: list[str],
        cloudwatch=None,
        clock=time.time,
        dry_run: bool = True,
        max_delete_abs: int = DEFAULT_MAX_DELETE_ABS,
        max_delete_pct: float = DEFAULT_MAX_DELETE_PCT,
        namespace: str = "VectorVault/TTL",
    ) -> None:
        self._s3v = s3vectors
        self._mem = memory_index_table
        self._ttl = ttl_index_table
        self._bucket = vector_bucket
        self._indexes = indexes
        self._cw = cloudwatch
        self._clock = clock
        self._dry_run = dry_run
        self._max_delete_abs = max_delete_abs
        self._max_delete_pct = max_delete_pct
        self._namespace = namespace
        self._index_sizes: dict[str, int] = {}

    # --- orchestration ----------------------------------------------------------

    def run(self) -> dict[str, Any]:
        now = int(self._clock())
        summary = {
            "dry_run": self._dry_run,
            "archived": 0,
            "deleted": 0,
            "drift_repaired": 0,
            "errors": 0,
            "circuit_breaker_tripped": False,
        }
        try:
            summary["archived"] = self.promote_superseded(now)
            summary["deleted"] = self.delete_archived(now) + self.delete_expired(now)
            summary["drift_repaired"] = self.reconcile()
        except CircuitBreakerError as exc:
            summary["circuit_breaker_tripped"] = True
            summary["errors"] += 1
            logger.error("circuit breaker tripped: %s", exc)
            self._emit("CircuitBreakerTripped", 1)

        self._emit("ArchivedCount", summary["archived"])
        self._emit("DeletedCount", summary["deleted"])
        self._emit("DriftRepaired", summary["drift_repaired"])
        self._emit("Errors", summary["errors"])
        logger.info("ttl run complete: %s", summary)
        return summary

    # --- superseded -> archived (7-day grace) -----------------------------------

    def promote_superseded(self, now: int) -> int:
        archived = 0
        for row in self._scan(self._mem):
            supersession_ts = int(row.get("created_at", 0))
            if now - supersession_ts < SUPERSEDED_GRACE_S:
                continue
            for key in row.get("superseded_keys", []) or []:
                found = self._find_vector(key)
                if found is None:
                    continue
                index, data, md = found
                if md.get("status") != STATUS_SUPERSEDED:
                    continue
                md = {**md, "status": STATUS_ARCHIVED, "archived_at": now}
                if not self._dry_run:
                    self._put_vector(index, key, data, md)
                archived += 1
        return archived

    # --- archived -> deleted (30-day grace) -------------------------------------

    def delete_archived(self, now: int) -> int:
        per_index: dict[str, list[str]] = {}
        for row in self._scan(self._mem):
            for key in row.get("superseded_keys", []) or []:
                found = self._find_vector(key)
                if found is None:
                    continue
                index, _data, md = found
                if md.get("status") != STATUS_ARCHIVED:
                    continue
                if now - int(md.get("archived_at", 0)) >= ARCHIVED_GRACE_S:
                    per_index.setdefault(index, []).append(key)
        return sum(self._delete(index, keys) for index, keys in per_index.items())

    # --- hard TTL: expires_at <= now (via memory-ttl-index) ---------------------

    def delete_expired(self, now: int) -> int:
        if self._ttl is None:
            return 0
        deleted = 0
        for index in self._indexes:
            rows = self._query_expired(index, now)
            confirmed: list[str] = []
            for row in rows:
                key = row.get("key")
                found = self._find_vector(key) if key else None
                if found is None:
                    self._delete_ttl_row(index, row)  # stale expiry row; clean up
                    continue
                _idx, _data, md = found
                if int(md.get("expires_at", 0)) <= now:
                    confirmed.append(key)
            deleted += self._delete(index, confirmed)
            if not self._dry_run:
                for row in rows:
                    self._delete_ttl_row(index, row)
        return deleted

    # --- reconciliation sweep ---------------------------------------------------

    def reconcile(self) -> int:
        repaired = 0
        for index in self._indexes:
            actual: dict[str, tuple[tuple[int, int], str, str]] = {}
            count = 0
            for vec in self._scan_vectors(index):
                count += 1
                md = vec.get("metadata", {})
                cid = md.get("canonical_id")
                if not cid:
                    continue
                rank = (int(md.get("version", 1)), int(md.get("created_at", 0)))
                current = actual.get(cid)
                if current is None or rank > current[0]:
                    actual[cid] = (rank, vec["key"], md.get("status", ""))
            self._index_sizes[index] = count

            for cid, (rank, key, status) in actual.items():
                row = self._mem.get_item(Key={"canonical_id": cid}).get("Item")
                if row is not None and row.get("latest_key") == key and row.get("status") == status:
                    continue
                if not self._dry_run:
                    self._mem.put_item(
                        Item={
                            "canonical_id": cid,
                            "latest_key": key,
                            "version": rank[0],
                            "created_at": rank[1],
                            "status": status,
                        }
                    )
                repaired += 1
        return repaired

    # --- delete with circuit breaker + DRY_RUN ----------------------------------

    def _delete(self, index: str, keys: list[str]) -> int:
        if not keys:
            return 0
        threshold = max(self._max_delete_abs, int(self._max_delete_pct * self._index_sizes.get(index, 0)))
        if len(keys) > threshold:
            raise CircuitBreakerError(
                f"{index}: would delete {len(keys)} vectors, exceeds cap {threshold}"
            )
        if self._dry_run:
            logger.info("[DRY_RUN] would delete %d vectors from %s", len(keys), index)
            return len(keys)
        # Batch up to 500 per DeleteVectors call (design-doc §3).
        for start in range(0, len(keys), 500):
            self._s3v.delete_vectors(
                vectorBucketName=self._bucket, indexName=index, keys=keys[start : start + 500]
            )
        return len(keys)

    # --- S3 Vectors / DynamoDB wrappers -----------------------------------------

    def _find_vector(self, key: str) -> tuple[str, list[float], dict[str, Any]] | None:
        for index in self._indexes:
            resp = self._s3v.get_vectors(
                vectorBucketName=self._bucket,
                indexName=index,
                keys=[key],
                returnData=True,
                returnMetadata=True,
            )
            vectors = resp.get("vectors", [])
            if vectors:
                v = vectors[0]
                return index, v.get("data", {}).get("float32", []), v.get("metadata", {})
        return None

    def _put_vector(self, index: str, key: str, data: list[float], metadata: dict[str, Any]) -> None:
        self._s3v.put_vectors(
            vectorBucketName=self._bucket,
            indexName=index,
            vectors=[{"key": key, "data": {"float32": data}, "metadata": metadata}],
        )

    def _scan_vectors(self, index: str) -> Iterator[dict[str, Any]]:
        token = None
        while True:
            kwargs: dict[str, Any] = {
                "vectorBucketName": self._bucket,
                "indexName": index,
                "returnMetadata": True,
                "maxResults": 500,
            }
            if token:
                kwargs["nextToken"] = token
            resp = self._s3v.list_vectors(**kwargs)
            yield from resp.get("vectors", [])
            token = resp.get("nextToken")
            if not token:
                return

    def _query_expired(self, index: str, now: int) -> list[dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        resp = self._ttl.query(
            KeyConditionExpression=Key("index_name").eq(index) & Key("expires_at").lte(now)
        )
        return resp.get("Items", [])

    def _delete_ttl_row(self, index: str, row: dict[str, Any]) -> None:
        if self._dry_run:
            return
        try:
            self._ttl.delete_item(Key={"index_name": index, "expires_at": row["expires_at"]})
        except Exception:
            pass

    @staticmethod
    def _scan(table) -> Iterator[dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        while True:
            resp = table.scan(**kwargs)
            yield from resp.get("Items", [])
            last = resp.get("LastEvaluatedKey")
            if not last:
                return
            kwargs["ExclusiveStartKey"] = last

    def _emit(self, metric: str, value: float) -> None:
        if self._cw is None:
            return
        try:
            self._cw.put_metric_data(
                Namespace=self._namespace,
                MetricData=[{"MetricName": metric, "Value": value, "Unit": "Count"}],
            )
        except Exception:
            pass


def handler(event, context):  # pragma: no cover - Lambda entrypoint (exercised in PR 5)
    """EventBridge -> Lambda entrypoint. Config comes from env vars set by the CDK stack."""
    import boto3

    region = os.environ["AWS_REGION"]
    dynamodb = boto3.resource("dynamodb", region_name=region)
    ttl_name = os.environ.get("TTL_INDEX_TABLE") or ""
    worker = TTLWorker(
        s3vectors=boto3.client("s3vectors", region_name=region),
        memory_index_table=dynamodb.Table(os.environ["MEMORY_INDEX_TABLE"]),
        ttl_index_table=dynamodb.Table(ttl_name) if ttl_name else None,
        vector_bucket=os.environ["VECTOR_BUCKET"],
        indexes=[i for i in os.environ["INDEXES"].split(",") if i],
        cloudwatch=boto3.client("cloudwatch", region_name=region),
        dry_run=os.environ.get("DRY_RUN", "true").lower() != "false",
        max_delete_abs=int(os.environ.get("MAX_DELETE_ABS", DEFAULT_MAX_DELETE_ABS)),
        max_delete_pct=float(os.environ.get("MAX_DELETE_PCT", DEFAULT_MAX_DELETE_PCT)),
    )
    return worker.run()
