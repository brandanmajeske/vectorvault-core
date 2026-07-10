"""TTL worker lifecycle: promote / delete / expiry / reconcile / circuit breaker."""

from __future__ import annotations

import pytest
from conftest import FakeCloudWatch, FakeS3Vectors, FakeTable

from vectorvault.ttl_worker import (
    ARCHIVED_GRACE_S,
    SUPERSEDED_GRACE_S,
    CircuitBreakerError,
    TTLWorker,
)

NOW = 2_000_000
INDEX = "shared-team-memory"


def _worker(s3v, mem, ttl=None, cw=None, *, dry_run=False, **kw):
    return TTLWorker(
        s3vectors=s3v,
        memory_index_table=mem,
        ttl_index_table=ttl,
        vector_bucket="agent-memory-store",
        indexes=[INDEX],
        cloudwatch=cw,
        clock=lambda: NOW,
        dry_run=dry_run,
        **kw,
    )


def _vec(s3v, key, **md):
    base = {"canonical_id": "c", "version": 1, "created_at": 100, "status": "active"}
    base.update(md)
    s3v.vectors[(INDEX, key)] = {"data": {"float32": [0.1]}, "metadata": base}


def test_promote_superseded_to_archived_after_grace():
    s3v, mem = FakeS3Vectors(), FakeTable("canonical_id")
    _vec(s3v, "old", status="superseded")
    mem.items["c"] = {"canonical_id": "c", "superseded_keys": ["old"], "created_at": NOW - SUPERSEDED_GRACE_S - 1}
    archived = _worker(s3v, mem).promote_superseded(NOW)
    assert archived == 1
    meta = s3v.vectors[(INDEX, "old")]["metadata"]
    assert meta["status"] == "archived"
    assert meta["archived_at"] == NOW


def test_promote_respects_7day_grace():
    s3v, mem = FakeS3Vectors(), FakeTable("canonical_id")
    _vec(s3v, "old", status="superseded")
    mem.items["c"] = {"canonical_id": "c", "superseded_keys": ["old"], "created_at": NOW - 100}  # just superseded
    assert _worker(s3v, mem).promote_superseded(NOW) == 0
    assert s3v.vectors[(INDEX, "old")]["metadata"]["status"] == "superseded"


def test_delete_archived_after_30days():
    s3v, mem = FakeS3Vectors(), FakeTable("canonical_id")
    _vec(s3v, "arch", status="archived", archived_at=NOW - ARCHIVED_GRACE_S - 1)
    mem.items["c"] = {"canonical_id": "c", "superseded_keys": ["arch"], "created_at": 0}
    deleted = _worker(s3v, mem).delete_archived(NOW)
    assert deleted == 1
    assert (INDEX, "arch") in s3v.deleted
    assert (INDEX, "arch") not in s3v.vectors


def test_archived_within_grace_not_deleted():
    s3v, mem = FakeS3Vectors(), FakeTable("canonical_id")
    _vec(s3v, "arch", status="archived", archived_at=NOW - 100)
    mem.items["c"] = {"canonical_id": "c", "superseded_keys": ["arch"], "created_at": 0}
    assert _worker(s3v, mem).delete_archived(NOW) == 0
    assert s3v.deleted == []


def test_hard_ttl_deletes_expired_via_index():
    s3v, mem, ttl = FakeS3Vectors(), FakeTable("canonical_id"), FakeTable("index_name")
    _vec(s3v, "exp", status="active", expires_at=NOW - 10)
    ttl.query_result = [{"index_name": INDEX, "expires_at": NOW - 10, "key": "exp"}]
    deleted = _worker(s3v, mem, ttl).delete_expired(NOW)
    assert deleted == 1
    assert (INDEX, "exp") in s3v.deleted


def test_dry_run_deletes_nothing_but_counts():
    s3v, mem = FakeS3Vectors(), FakeTable("canonical_id")
    _vec(s3v, "arch", status="archived", archived_at=NOW - ARCHIVED_GRACE_S - 1)
    mem.items["c"] = {"canonical_id": "c", "superseded_keys": ["arch"], "created_at": 0}
    deleted = _worker(s3v, mem, dry_run=True).delete_archived(NOW)
    assert deleted == 1  # intended count reported
    assert s3v.deleted == []  # but nothing actually deleted
    assert (INDEX, "arch") in s3v.vectors


def test_circuit_breaker_aborts_oversized_delete():
    s3v, mem, cw = FakeS3Vectors(), FakeTable("canonical_id"), FakeCloudWatch()
    for i in range(3):
        _vec(s3v, f"a{i}", status="archived", archived_at=NOW - ARCHIVED_GRACE_S - 1)
    mem.items["c"] = {"canonical_id": "c", "superseded_keys": ["a0", "a1", "a2"], "created_at": 0}
    worker = _worker(s3v, mem, cw=cw, max_delete_abs=2, max_delete_pct=0.0)

    with pytest.raises(CircuitBreakerError):
        worker.delete_archived(NOW)
    assert s3v.deleted == []  # nothing deleted once the cap is exceeded

    # run() catches the breaker and reports it (does not crash the Lambda).
    summary = _worker(s3v, mem, cw=cw, max_delete_abs=2, max_delete_pct=0.0).run()
    assert summary["circuit_breaker_tripped"] is True
    assert summary["deleted"] == 0
    assert ("CircuitBreakerTripped", 1) in cw.metrics


def test_reconcile_repairs_memory_index_drift():
    s3v, mem = FakeS3Vectors(), FakeTable("canonical_id")
    _vec(s3v, "real-latest", canonical_id="c", version=2, created_at=200, status="active")
    mem.items["c"] = {"canonical_id": "c", "latest_key": "STALE", "status": "active"}
    repaired = _worker(s3v, mem).reconcile()
    assert repaired == 1
    assert mem.items["c"]["latest_key"] == "real-latest"


def test_run_emits_metrics():
    s3v, mem, cw = FakeS3Vectors(), FakeTable("canonical_id"), FakeCloudWatch()
    summary = _worker(s3v, mem, cw=cw).run()
    names = {m[0] for m in cw.metrics}
    assert {"ArchivedCount", "DeletedCount", "DriftRepaired", "Errors"} <= names
    assert summary["circuit_breaker_tripped"] is False
