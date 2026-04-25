"""Pydantic request/response schemas for the KV store API."""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


# --- Batch Operation Schemas ---


class KVBatchOpSet(BaseModel):
    """Set operation in a batch."""

    op: Literal["set"]
    key: str = Field(..., min_length=1, max_length=255)
    value: Any = Field(..., description="Value to set")
    nx: bool = Field(default=False, description="Only set if key does not exist")
    xx: bool = Field(default=False, description="Only set if key exists")

    @model_validator(mode="after")
    def validate_nx_xx(self):
        if self.nx and self.xx:
            raise ValueError("Cannot use both nx and xx")
        return self


class KVBatchOpDelete(BaseModel):
    """Delete operation in a batch."""

    op: Literal["delete"]
    key: str = Field(..., min_length=1, max_length=255)


class KVBatchOpIncr(BaseModel):
    """Increment operation in a batch."""

    op: Literal["incr"]
    key: str = Field(..., min_length=1, max_length=255)
    by: int = Field(default=1, description="Amount to increment by")


class KVBatchOpDecr(BaseModel):
    """Decrement operation in a batch."""

    op: Literal["decr"]
    key: str = Field(..., min_length=1, max_length=255)
    by: int = Field(default=1, description="Amount to decrement by")


class KVBatchOpLPush(BaseModel):
    """Left push operation in a batch."""

    op: Literal["lpush"]
    key: str = Field(..., min_length=1, max_length=255)
    value: Any = Field(..., description="Value to push")


class KVBatchOpRPush(BaseModel):
    """Right push operation in a batch."""

    op: Literal["rpush"]
    key: str = Field(..., min_length=1, max_length=255)
    value: Any = Field(..., description="Value to push")


class KVBatchOpLPop(BaseModel):
    """Left pop operation in a batch."""

    op: Literal["lpop"]
    key: str = Field(..., min_length=1, max_length=255)


class KVBatchOpRPop(BaseModel):
    """Right pop operation in a batch."""

    op: Literal["rpop"]
    key: str = Field(..., min_length=1, max_length=255)


class KVBatchOpPatch(BaseModel):
    """Patch operation in a batch."""

    op: Literal["patch"]
    key: str = Field(..., min_length=1, max_length=255)
    path: str = Field(..., description="Dot-notation path to update")
    value: Any = Field(..., description="Value to set at the path")


# Union of all batch operation types
KVBatchOperation = (
    KVBatchOpSet
    | KVBatchOpDelete
    | KVBatchOpIncr
    | KVBatchOpDecr
    | KVBatchOpLPush
    | KVBatchOpRPush
    | KVBatchOpLPop
    | KVBatchOpRPop
    | KVBatchOpPatch
)


class KVBatchRequest(BaseModel):
    """Request body for batch operations."""

    if_version: int | None = Field(
        default=None,
        description="Only execute if current state version matches this value",
    )
    operations: list[KVBatchOperation] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="List of operations to execute atomically",
    )


# Batch operation results are returned as dicts with the following fields:
# - op: str - The operation type
# - key: str - The key operated on
# - success: bool - Always True (batch fails atomically if any op fails)
# - Additional fields depend on operation type:
#   - set: created (bool) - True if key was newly created
#   - delete: deleted (bool) - True if key existed and was deleted
#   - incr/decr: value (int) - New value after increment/decrement
#   - lpush/rpush: length (int) - New list length
#   - lpop/rpop: value (Any) - Popped value, or null if list was empty
#   - patch: (no additional fields)


class KVBatchResponse(BaseModel):
    """Response for successful batch operation."""

    version: int = Field(description="New state version after batch")
    results: list[dict[str, Any]] = Field(
        description="Results for each operation in order"
    )


class KVVersionMismatchResponse(BaseModel):
    """Response when batch fails due to version mismatch."""

    error: Literal["version_mismatch"] = "version_mismatch"
    message: str = "State was modified by another process"
    expected_version: int
    actual_version: int


# --- Request Schemas ---


class KVSetRequest(BaseModel):
    """Request body for setting a KV value (used when body is explicit)."""

    value: Any = Field(..., description="Any JSON-serializable value")


class KVPatchRequest(BaseModel):
    """Request body for patching a nested path."""

    path: str = Field(
        ..., description="Dot-notation path to update (e.g., 'database.port')"
    )
    value: Any = Field(..., description="Value to set at the path")


class KVIncrRequest(BaseModel):
    """Request body for increment/decrement operations."""

    by: int = Field(default=1, description="Amount to increment/decrement by")


class KVListPushRequest(BaseModel):
    """Request body for list push operations."""

    value: Any = Field(..., description="Value to push onto the list")


# --- Response Schemas ---


class KVKeyResponse(BaseModel):
    """Response containing a key and its value."""

    key: str
    value: Any


class KVKeyPathResponse(BaseModel):
    """Response containing a key, path, and value."""

    key: str
    path: str
    value: Any


class KVKeyMetaResponse(BaseModel):
    """Response containing a key, value, and metadata."""

    key: str
    value: Any
    version: int
    created_at: str
    updated_at: str


class KVSetResponse(BaseModel):
    """Response for set operations."""

    key: str
    value: Any
    created: bool
    updated_at: str


class KVDeleteResponse(BaseModel):
    """Response for delete operations."""

    key: str
    deleted: bool


class KVListKeysResponse(BaseModel):
    """Response for listing keys."""

    keys: list[str]
    count: int


class KVIncrResponse(BaseModel):
    """Response for increment/decrement operations."""

    key: str
    value: int


class KVListLengthResponse(BaseModel):
    """Response for list length operations."""

    key: str
    length: int


class KVConflictResponse(BaseModel):
    """Response when a conditional operation fails."""

    key: str
    created: bool = False
    error: str
