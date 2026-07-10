"""Best-effort CloudWatch custom metrics for the client (design-doc §4.2, §7).

The ``VectorVault/Client`` namespace backs the §7 alarms that the monitoring stack
(PR 5) declares: ``EmbeddingCacheHit`` / ``EmbeddingCacheMiss`` (hit-rate),
``InjectionSuspect``, ``ThrottleException`` (S3 Vectors 429s), and
``QueryLatencyMs`` (QueryVectors latency).

Emission is **opt-in and off by default** — the client uses :class:`NullMetrics`
unless built with ``enable_metrics=True`` (cost/permission opt-in, mirroring the TTL
worker's ``DRY_RUN``). Emission is always best-effort: a PutMetricData failure never
breaks a read/write.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

CLIENT_NAMESPACE = "VectorVault/Client"


@runtime_checkable
class MetricsEmitter(Protocol):
    """Minimal metric sink used by the client and embedding cache."""

    def count(self, name: str, value: float = 1.0, **dimensions: str) -> None: ...
    def timing(self, name: str, ms: float, **dimensions: str) -> None: ...


class NullMetrics:
    """No-op emitter (the default) — zero cost, zero permissions."""

    def count(self, name: str, value: float = 1.0, **dimensions: str) -> None:
        pass

    def timing(self, name: str, ms: float, **dimensions: str) -> None:
        pass


class CloudWatchMetrics:
    """Emits to CloudWatch via ``PutMetricData``. Best-effort: swallows errors so
    metric emission never blocks the memory hot path."""

    def __init__(self, cloudwatch_client: Any, namespace: str = CLIENT_NAMESPACE) -> None:
        self._cw = cloudwatch_client
        self._ns = namespace

    def count(self, name: str, value: float = 1.0, **dimensions: str) -> None:
        self._put(name, float(value), "Count", dimensions)

    def timing(self, name: str, ms: float, **dimensions: str) -> None:
        self._put(name, float(ms), "Milliseconds", dimensions)

    def _put(self, name: str, value: float, unit: str, dimensions: dict[str, str]) -> None:
        datum: dict[str, Any] = {"MetricName": name, "Value": value, "Unit": unit}
        if dimensions:
            datum["Dimensions"] = [{"Name": k, "Value": str(v)} for k, v in dimensions.items()]
        try:
            self._cw.put_metric_data(Namespace=self._ns, MetricData=[datum])
        except Exception:
            pass
