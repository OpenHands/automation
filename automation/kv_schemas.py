"""Pydantic request/response schemas for the KV store API."""

from typing import Any

from pydantic import BaseModel, Field


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
