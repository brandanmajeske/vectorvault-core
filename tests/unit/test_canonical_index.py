from unittest.mock import MagicMock

from vectorvault.canonical_index import CanonicalIndex


def _idx():
    table = MagicMock()
    return CanonicalIndex(table=table, task_gsi_name="task-gsi"), table


def test_record_use_increments_and_stamps():
    idx, table = _idx()
    idx.record_use("dec:1", now=1000)
    args, kwargs = table.update_item.call_args
    assert kwargs["Key"] == {"canonical_id": "dec:1"}
    assert "use_count" in kwargs["UpdateExpression"]
    assert "last_used_at" in kwargs["UpdateExpression"]


def test_record_use_swallows_errors():
    idx, table = _idx()
    table.update_item.side_effect = RuntimeError("dynamo down")
    idx.record_use("dec:1", now=1000)  # must not raise


def test_get_usage_returns_empty_on_error():
    idx, table = _idx()
    table.meta.client.batch_get_item.side_effect = RuntimeError("dynamo down")
    assert idx.get_usage(["dec:1"]) == {}
