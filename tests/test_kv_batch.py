"""Unit tests for KV batch operations and $version functionality.

These tests focus on the batch operation logic without requiring a database.
"""

import pytest
from fastapi import HTTPException

from automation.kv_helpers import validate_key
from automation.kv_router import (
    KVOperationError,
    _execute_batch_operation,
    _get_version,
    _validate_batch_key,
)
from automation.kv_schemas import (
    KVBatchOpDecr,
    KVBatchOpDelete,
    KVBatchOpIncr,
    KVBatchOpLPop,
    KVBatchOpLPush,
    KVBatchOpPatch,
    KVBatchOpRPop,
    KVBatchOpRPush,
    KVBatchOpSet,
    KVBatchRequest,
)


class TestValidateKeyReserved:
    """Test that $ prefix keys are rejected."""

    def test_dollar_prefix_rejected(self):
        with pytest.raises(HTTPException) as exc:
            validate_key("$version")
        assert exc.value.status_code == 400
        assert "reserved" in exc.value.detail.lower()

    def test_dollar_prefix_any_name_rejected(self):
        with pytest.raises(HTTPException) as exc:
            validate_key("$anything")
        assert exc.value.status_code == 400
        assert "reserved" in exc.value.detail.lower()

    def test_dollar_in_middle_allowed(self):
        # $ in middle is fine, only prefix is reserved
        result = validate_key("my$key")
        assert result == "my$key"

    def test_dollar_at_end_allowed(self):
        result = validate_key("key$")
        assert result == "key$"


class TestValidateBatchKey:
    """Test batch key validation."""

    def test_valid_key(self):
        _validate_batch_key("mykey")  # Should not raise

    def test_empty_key_rejected(self):
        with pytest.raises(KVOperationError, match="empty"):
            _validate_batch_key("")

    def test_whitespace_key_rejected(self):
        with pytest.raises(KVOperationError, match="whitespace"):
            _validate_batch_key("   ")

    def test_dollar_prefix_rejected(self):
        with pytest.raises(KVOperationError, match="reserved"):
            _validate_batch_key("$version")

    def test_long_key_rejected(self):
        with pytest.raises(KVOperationError, match="exceeds 255"):
            _validate_batch_key("x" * 256)


class TestGetVersion:
    """Test version extraction."""

    def test_get_version_present(self):
        assert _get_version({"$version": 5, "key": "value"}) == 5

    def test_get_version_missing(self):
        assert _get_version({"key": "value"}) == 0

    def test_get_version_empty_state(self):
        assert _get_version({}) == 0


class TestBatchOpSet:
    """Test set operation in batch."""

    def test_set_new_key(self):
        state = {}
        op = KVBatchOpSet(op="set", key="foo", value="bar")
        result = _execute_batch_operation(state, op)

        assert state["foo"] == "bar"
        assert result == {"op": "set", "key": "foo", "success": True, "created": True}

    def test_set_existing_key(self):
        state = {"foo": "old"}
        op = KVBatchOpSet(op="set", key="foo", value="new")
        result = _execute_batch_operation(state, op)

        assert state["foo"] == "new"
        assert result == {"op": "set", "key": "foo", "success": True, "created": False}

    def test_set_nx_creates_new(self):
        state = {}
        op = KVBatchOpSet(op="set", key="foo", value="bar", nx=True)
        result = _execute_batch_operation(state, op)

        assert state["foo"] == "bar"
        assert result["created"] is True

    def test_set_nx_fails_if_exists(self):
        state = {"foo": "old"}
        op = KVBatchOpSet(op="set", key="foo", value="new", nx=True)

        with pytest.raises(KVOperationError, match="already exists"):
            _execute_batch_operation(state, op)

    def test_set_xx_updates_existing(self):
        state = {"foo": "old"}
        op = KVBatchOpSet(op="set", key="foo", value="new", xx=True)
        result = _execute_batch_operation(state, op)

        assert state["foo"] == "new"
        assert result["created"] is False

    def test_set_xx_fails_if_not_exists(self):
        state = {}
        op = KVBatchOpSet(op="set", key="foo", value="bar", xx=True)

        with pytest.raises(KVOperationError, match="does not exist"):
            _execute_batch_operation(state, op)

    def test_set_reserved_key_rejected(self):
        state = {}
        op = KVBatchOpSet(op="set", key="$version", value=100)

        with pytest.raises(KVOperationError, match="reserved"):
            _execute_batch_operation(state, op)


class TestBatchOpDelete:
    """Test delete operation in batch."""

    def test_delete_existing(self):
        state = {"foo": "bar"}
        op = KVBatchOpDelete(op="delete", key="foo")
        result = _execute_batch_operation(state, op)

        assert "foo" not in state
        expected = {"op": "delete", "key": "foo", "success": True, "deleted": True}
        assert result == expected

    def test_delete_nonexistent(self):
        state = {}
        op = KVBatchOpDelete(op="delete", key="foo")
        result = _execute_batch_operation(state, op)

        expected = {"op": "delete", "key": "foo", "success": True, "deleted": False}
        assert result == expected


class TestBatchOpIncr:
    """Test incr operation in batch."""

    def test_incr_creates_key(self):
        state = {}
        op = KVBatchOpIncr(op="incr", key="counter")
        result = _execute_batch_operation(state, op)

        assert state["counter"] == 1
        assert result == {"op": "incr", "key": "counter", "success": True, "value": 1}

    def test_incr_increments_existing(self):
        state = {"counter": 5}
        op = KVBatchOpIncr(op="incr", key="counter")
        result = _execute_batch_operation(state, op)

        assert state["counter"] == 6
        assert result["value"] == 6

    def test_incr_by_custom_amount(self):
        state = {"counter": 10}
        op = KVBatchOpIncr(op="incr", key="counter", by=5)
        result = _execute_batch_operation(state, op)

        assert state["counter"] == 15
        assert result["value"] == 15

    def test_incr_rejects_non_integer(self):
        state = {"counter": "not a number"}
        op = KVBatchOpIncr(op="incr", key="counter")

        with pytest.raises(KVOperationError, match="not an integer"):
            _execute_batch_operation(state, op)

    def test_incr_rejects_boolean(self):
        state = {"flag": True}
        op = KVBatchOpIncr(op="incr", key="flag")

        with pytest.raises(KVOperationError, match="boolean"):
            _execute_batch_operation(state, op)


class TestBatchOpDecr:
    """Test decr operation in batch."""

    def test_decr_creates_negative(self):
        state = {}
        op = KVBatchOpDecr(op="decr", key="counter")
        result = _execute_batch_operation(state, op)

        assert state["counter"] == -1
        assert result["value"] == -1

    def test_decr_decrements_existing(self):
        state = {"counter": 10}
        op = KVBatchOpDecr(op="decr", key="counter", by=3)
        result = _execute_batch_operation(state, op)

        assert state["counter"] == 7
        assert result["value"] == 7


class TestBatchOpLPush:
    """Test lpush operation in batch."""

    def test_lpush_creates_list(self):
        state = {}
        op = KVBatchOpLPush(op="lpush", key="queue", value="item1")
        result = _execute_batch_operation(state, op)

        assert state["queue"] == ["item1"]
        assert result == {"op": "lpush", "key": "queue", "success": True, "length": 1}

    def test_lpush_prepends(self):
        state = {"queue": ["b", "c"]}
        op = KVBatchOpLPush(op="lpush", key="queue", value="a")
        result = _execute_batch_operation(state, op)

        assert state["queue"] == ["a", "b", "c"]
        assert result["length"] == 3

    def test_lpush_rejects_non_list(self):
        state = {"queue": "not a list"}
        op = KVBatchOpLPush(op="lpush", key="queue", value="item")

        with pytest.raises(KVOperationError, match="not a list"):
            _execute_batch_operation(state, op)


class TestBatchOpRPush:
    """Test rpush operation in batch."""

    def test_rpush_creates_list(self):
        state = {}
        op = KVBatchOpRPush(op="rpush", key="queue", value="item1")
        result = _execute_batch_operation(state, op)

        assert state["queue"] == ["item1"]
        assert result["length"] == 1

    def test_rpush_appends(self):
        state = {"queue": ["a", "b"]}
        op = KVBatchOpRPush(op="rpush", key="queue", value="c")
        result = _execute_batch_operation(state, op)

        assert state["queue"] == ["a", "b", "c"]
        assert result["length"] == 3


class TestBatchOpLPop:
    """Test lpop operation in batch."""

    def test_lpop_returns_first(self):
        state = {"queue": ["a", "b", "c"]}
        op = KVBatchOpLPop(op="lpop", key="queue")
        result = _execute_batch_operation(state, op)

        assert state["queue"] == ["b", "c"]
        assert result == {"op": "lpop", "key": "queue", "success": True, "value": "a"}

    def test_lpop_empty_returns_null(self):
        state = {"queue": []}
        op = KVBatchOpLPop(op="lpop", key="queue")
        result = _execute_batch_operation(state, op)

        assert result["value"] is None

    def test_lpop_nonexistent_returns_null(self):
        state = {}
        op = KVBatchOpLPop(op="lpop", key="queue")
        result = _execute_batch_operation(state, op)

        assert result["value"] is None


class TestBatchOpRPop:
    """Test rpop operation in batch."""

    def test_rpop_returns_last(self):
        state = {"queue": ["a", "b", "c"]}
        op = KVBatchOpRPop(op="rpop", key="queue")
        result = _execute_batch_operation(state, op)

        assert state["queue"] == ["a", "b"]
        assert result["value"] == "c"


class TestBatchOpPatch:
    """Test patch operation in batch."""

    def test_patch_updates_nested(self):
        state = {"config": {"db": {"host": "localhost"}}}
        op = KVBatchOpPatch(op="patch", key="config", path="db.port", value=5432)
        result = _execute_batch_operation(state, op)

        assert state["config"]["db"]["port"] == 5432
        assert result == {"op": "patch", "key": "config", "success": True}

    def test_patch_creates_key_if_missing(self):
        state = {}
        op = KVBatchOpPatch(op="patch", key="config", path="db.host", value="localhost")
        _execute_batch_operation(state, op)

        assert state["config"]["db"]["host"] == "localhost"

    def test_patch_rejects_non_dict(self):
        state = {"config": "not a dict"}
        op = KVBatchOpPatch(op="patch", key="config", path="db.host", value="localhost")

        with pytest.raises(KVOperationError, match="not an object"):
            _execute_batch_operation(state, op)


class TestBatchRequest:
    """Test batch request validation."""

    def test_valid_batch(self):
        req = KVBatchRequest(
            operations=[
                KVBatchOpSet(op="set", key="a", value=1),
                KVBatchOpIncr(op="incr", key="b"),
            ]
        )
        assert len(req.operations) == 2

    def test_batch_with_version(self):
        req = KVBatchRequest(
            if_version=5,
            operations=[KVBatchOpSet(op="set", key="a", value=1)],
        )
        assert req.if_version == 5

    def test_empty_operations_rejected(self):
        with pytest.raises(ValueError):
            KVBatchRequest(operations=[])

    def test_too_many_operations_rejected(self):
        ops: list = [KVBatchOpIncr(op="incr", key=f"k{i}") for i in range(101)]
        with pytest.raises(ValueError):
            KVBatchRequest(operations=ops)


class TestBatchMultipleOps:
    """Test executing multiple operations in sequence."""

    def test_multiple_ops_in_order(self):
        state = {}

        ops = [
            KVBatchOpSet(op="set", key="counter", value=0),
            KVBatchOpIncr(op="incr", key="counter", by=5),
            KVBatchOpIncr(op="incr", key="counter", by=3),
            KVBatchOpRPush(op="rpush", key="log", value="started"),
            KVBatchOpRPush(op="rpush", key="log", value="finished"),
        ]

        results = []
        for op in ops:
            results.append(_execute_batch_operation(state, op))

        assert state["counter"] == 8
        assert state["log"] == ["started", "finished"]
        assert results[0]["created"] is True
        assert results[1]["value"] == 5
        assert results[2]["value"] == 8
        assert results[3]["length"] == 1
        assert results[4]["length"] == 2

    def test_early_failure_stops_batch(self):
        """Simulate what happens when an operation fails mid-batch."""
        state = {"counter": "not a number"}

        ops = [
            KVBatchOpSet(op="set", key="before", value="ok"),
            KVBatchOpIncr(op="incr", key="counter"),  # This will fail
            KVBatchOpSet(op="set", key="after", value="should not run"),
        ]

        # First op succeeds
        _execute_batch_operation(state, ops[0])
        assert state["before"] == "ok"

        # Second op fails
        with pytest.raises(KVOperationError):
            _execute_batch_operation(state, ops[1])

        # In a real batch, the transaction would rollback, so "before" wouldn't persist
        # But we're testing that the error is raised properly


class TestVersionBump:
    """Test that $version is properly managed."""

    def test_version_starts_at_zero_if_missing(self):
        state = {"key": "value"}
        assert _get_version(state) == 0

    def test_version_preserved_across_reads(self):
        state = {"$version": 5, "key": "value"}
        assert _get_version(state) == 5
        # Operations don't touch $version directly
        op = KVBatchOpSet(op="set", key="other", value="x")
        _execute_batch_operation(state, op)
        # $version unchanged by operation (bump happens in _save_state)
        assert state["$version"] == 5
