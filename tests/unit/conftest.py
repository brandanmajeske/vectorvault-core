"""Shared fakes + fixtures for the unit suite. No AWS, no network — everything
is an in-memory double with the same call surface the client uses."""

from __future__ import annotations

import io
import json

import pytest
from botocore.exceptions import ClientError

from vectorvault.canonical_index import CanonicalIndex
from vectorvault.config import Config
from vectorvault.embedding_cache import EmbeddingCache
from vectorvault.memory_client import MemoryClient

FIXED_NOW = 1_000_000


class FakeBedrock:
    """Returns a deterministic embedding and counts invocations (to assert cache hits)."""

    def __init__(self) -> None:
        self.invoke_count = 0

    def invoke_model(self, modelId, contentType, accept, body):  # noqa: N803 (AWS param casing)
        self.invoke_count += 1
        text = json.loads(body)["inputText"]
        # Small deterministic vector; dimension is irrelevant to the client.
        vec = [float(len(text) % 5), 0.1, 0.2, 0.3]
        return {"body": io.BytesIO(json.dumps({"embedding": vec}).encode())}


class FakeS3Vectors:
    """In-memory vector store. ``query_hits`` is what the next query returns (tests
    set it directly — filtering/ranking is S3 Vectors' job, covered in integration)."""

    def __init__(self) -> None:
        self.vectors: dict[tuple[str, str], dict] = {}
        self.query_hits: list[dict] = []
        self.put_calls: list[tuple[str, list]] = []
        self.query_calls: list[dict] = []
        self.deleted: list[tuple[str, str]] = []

    def put_vectors(self, vectorBucketName, indexName, vectors):  # noqa: N803
        for v in vectors:
            self.vectors[(indexName, v["key"])] = {"data": v["data"], "metadata": v["metadata"]}
        self.put_calls.append((indexName, vectors))
        return {}

    def get_vectors(self, vectorBucketName, indexName, keys, returnData=True, returnMetadata=True):  # noqa: N803
        out = []
        for k in keys:
            rec = self.vectors.get((indexName, k))
            if rec is not None:
                out.append({"key": k, "data": rec["data"], "metadata": rec["metadata"]})
        return {"vectors": out}

    def query_vectors(self, vectorBucketName, indexName, queryVector, topK, returnMetadata=True, returnDistance=True, filter=None):  # noqa: N803, A002
        self.query_calls.append({"index": indexName, "filter": filter, "topK": topK})
        return {"vectors": self.query_hits[:topK]}

    def delete_vectors(self, vectorBucketName, indexName, keys):  # noqa: N803
        for k in keys:
            self.vectors.pop((indexName, k), None)
            self.deleted.append((indexName, k))
        return {}

    def list_vectors(self, vectorBucketName, indexName, returnMetadata=True, returnData=False, maxResults=500, nextToken=None):  # noqa: N803
        vecs = [
            {"key": key, "metadata": rec["metadata"]}
            for (idx, key), rec in self.vectors.items()
            if idx == indexName
        ]
        return {"vectors": vecs, "nextToken": None}


class FakeS3:
    """Content bucket double; records get_object keys to prove derived-key fetches."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.get_calls: list[tuple[str, str]] = []
        self.put_calls: list[tuple[str, str]] = []

    def put_object(self, Bucket, Key, Body, ContentType=None):  # noqa: N803
        self.objects[(Bucket, Key)] = Body
        self.put_calls.append((Bucket, Key))
        return {}

    def get_object(self, Bucket, Key):  # noqa: N803
        self.get_calls.append((Bucket, Key))
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.objects.pop((Bucket, Key), None)
        return {}


class FakeTable:
    """DynamoDB Table double. ``query`` returns the preset ``query_result``."""

    def __init__(self, pk: str) -> None:
        self.pk = pk
        self.items: dict[str, dict] = {}
        self.query_result: list[dict] = []
        self.get_calls = 0
        self.put_calls = 0

    def get_item(self, Key):  # noqa: N803
        self.get_calls += 1
        val = self.items.get(Key[self.pk])
        return {"Item": val} if val is not None else {}

    def put_item(self, Item):  # noqa: N803
        self.put_calls += 1
        self.items[Item[self.pk]] = Item
        return {}

    def delete_item(self, Key):  # noqa: N803
        self.items.pop(Key[self.pk], None)
        return {}

    def scan(self, **kwargs):
        return {"Items": list(self.items.values()), "LastEvaluatedKey": None}

    def query(self, **kwargs):
        return {"Items": list(self.query_result), "LastEvaluatedKey": None}


class FakeCloudWatch:
    def __init__(self) -> None:
        self.metrics: list[tuple[str, float]] = []

    def put_metric_data(self, Namespace, MetricData):  # noqa: N803
        for m in MetricData:
            self.metrics.append((m["MetricName"], m["Value"]))
        return {}


@pytest.fixture
def config() -> Config:
    return Config(
        region="us-west-2",
        vector_bucket="agent-memory-store",
        content_bucket="content-bkt",
        shared_index="shared-team-memory",
        planner_index="private-planner",
        researcher_index="private-researcher",
        embed_cache_table="memory-embed-cache",
        memory_index_table="memory-index",
        memory_index_task_gsi="task_id-created_at-index",
    )


@pytest.fixture
def fakes():
    return {
        "bedrock": FakeBedrock(),
        "s3v": FakeS3Vectors(),
        "s3": FakeS3(),
        "embed_table": FakeTable("content_hash"),
        "canon_table": FakeTable("canonical_id"),
        "ttl_table": FakeTable("index_name"),
        "cloudwatch": FakeCloudWatch(),
    }


@pytest.fixture
def client(config, fakes) -> MemoryClient:
    cache = EmbeddingCache(fakes["bedrock"], fakes["embed_table"], config.embed_model_id)
    canonical = CanonicalIndex(fakes["canon_table"], config.memory_index_task_gsi)
    return MemoryClient(
        config=config,
        agent_id="planner",
        s3vectors=fakes["s3v"],
        s3=fakes["s3"],
        embedding_cache=cache,
        canonical_index=canonical,
        ttl_index_table=fakes["ttl_table"],
        clock=lambda: FIXED_NOW,
    )
