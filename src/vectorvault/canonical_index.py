"""DynamoDB ``memory-index`` — best-effort lookup/listing index (design-doc §2/§10).

NEVER a correctness dependency: the vector metadata is the source of truth. If
this table drifts (crash between the vector write and the index write), retrieval
is unaffected and the PR 3 TTL reconciliation sweep repairs it. It exists to back
``list_memories`` exact lookups (``canonical_id``) and scoped listings
(``task_id`` GSI), which ``ListVectors`` cannot do (no metadata filters).
"""

from __future__ import annotations

from typing import Any

from boto3.dynamodb.conditions import Key


class CanonicalIndex:
    def __init__(self, table, task_gsi_name: str) -> None:
        self._table = table  # boto3 DynamoDB Table resource (or None to disable)
        self._task_gsi = task_gsi_name

    def upsert(
        self,
        *,
        canonical_id: str,
        latest_key: str,
        version: int,
        task_id: str,
        agent_id: str,
        memory_type: str,
        status: str,
        created_at: int,
        superseded_keys: list[str] | None = None,
    ) -> None:
        """Best-effort write of the canonical row. Swallows errors — drift is
        repaired by the TTL sweep and never affects retrieval correctness."""
        if self._table is None:
            return
        item: dict[str, Any] = {
            "canonical_id": canonical_id,
            "latest_key": latest_key,
            "version": version,
            "task_id": task_id,
            "agent_id": agent_id,
            "memory_type": memory_type,
            "status": status,
            "created_at": created_at,
        }
        if superseded_keys:
            item["superseded_keys"] = superseded_keys
        try:
            self._table.put_item(Item=item)
        except Exception:
            pass

    def record_use(self, canonical_id: str, now: int) -> None:
        """Best-effort: bump use_count and stamp last_used_at (hydration signal)."""
        if self._table is None:
            return
        try:
            self._table.update_item(
                Key={"canonical_id": canonical_id},
                UpdateExpression="ADD use_count :one SET last_used_at = :now",
                ExpressionAttributeValues={":one": 1, ":now": now},
            )
        except Exception:
            pass

    def get_usage(self, canonical_ids: list[str]) -> dict[str, tuple[int, int]]:
        """Best-effort batch read of (use_count, last_used_at); {} on error."""
        if self._table is None:
            return {}
        try:
            ids = [c for c in dict.fromkeys(canonical_ids) if c]
            if not ids:
                return {}
            resp = self._table.meta.client.batch_get_item(
                RequestItems={self._table.name: {"Keys": [{"canonical_id": c} for c in ids]}}
            )
            rows = resp.get("Responses", {}).get(self._table.name, [])
        except Exception:
            return {}
        out: dict[str, tuple[int, int]] = {}
        for r in rows:
            cid = r.get("canonical_id")
            if cid:
                out[cid] = (int(r.get("use_count", 0)), int(r.get("last_used_at", 0)))
        return out

    def get(self, canonical_id: str) -> dict[str, Any] | None:
        """Exact lookup by canonical_id (backs ``list_memories(canonical_id=...)``)."""
        if self._table is None:
            return None
        try:
            return self._table.get_item(Key={"canonical_id": canonical_id}).get("Item")
        except Exception:
            return None

    def delete(self, canonical_id: str) -> None:
        """Remove the canonical row (best-effort; used by purge_memory)."""
        if self._table is None:
            return
        try:
            self._table.delete_item(Key={"canonical_id": canonical_id})
        except Exception:
            pass

    def query_by_task(
        self,
        task_id: str,
        *,
        memory_type: str | None = None,
        status: str | None = None,
        page_size: int = 100,
        start_key: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Query the ``task_id`` GSI, newest first (created_at desc). Returns
        ``(items, next_start_key)`` for pagination."""
        if self._table is None:
            return [], None
        kwargs: dict[str, Any] = {
            "IndexName": self._task_gsi,
            "KeyConditionExpression": Key("task_id").eq(task_id),
            "ScanIndexForward": False,  # created_at descending -> latest first
            "Limit": page_size,
        }
        filters = []
        if memory_type is not None:
            filters.append(Key("memory_type").eq(memory_type))
        if status is not None:
            filters.append(Key("status").eq(status))
        if filters:
            expr = filters[0]
            for f in filters[1:]:
                expr = expr & f
            kwargs["FilterExpression"] = expr
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key

        resp = self._table.query(**kwargs)
        return resp.get("Items", []), resp.get("LastEvaluatedKey")
