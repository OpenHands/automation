"""Run token for OpenHands Enterprise worklogs.

Worklogs are stored and served by the Enterprise server. This service is the
only component that knows which factory an automation belongs to, so it mints
the credential a run uses to reach its factory's worklogs
(``AUTOMATION_WORKLOG_TOKEN``). Enterprise verifies the signature, ``aud`` and
``exp`` only. The contract is shared with OpenHands/enterprise#1297 and a fixed
test vector pins it in both repositories.

The token is a credential: never store it or log it.
"""

import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta

import jwt


WORKLOG_TOKEN_AUDIENCE = "worklog"
WORKLOG_TOKEN_HKDF_INFO = b"openhands-worklog-run-token-v1"
WORKLOG_TOKEN_EXPIRATION = timedelta(hours=24)
MAX_AUTOMATION_NAME_LENGTH = 256

_SIGNING_KEY_LENGTH = 32


def hkdf_sha256(key: bytes, info: bytes, length: int = _SIGNING_KEY_LENGTH) -> bytes:
    """HKDF-SHA256 (RFC 5869) with the default all-zero salt."""
    digest_size = hashlib.sha256().digest_size
    pseudo_random_key = hmac.digest(bytes(digest_size), key, "sha256")
    output = b""
    block = b""
    for counter in range(1, -(-length // digest_size) + 1):
        block = hmac.digest(
            pseudo_random_key, block + info + bytes([counter]), "sha256"
        )
        output += block
    return output[:length]


def derive_worklog_signing_key(service_key: str) -> bytes:
    """The HS256 key for worklog run tokens. The raw service key is never used
    to sign."""
    return hkdf_sha256(service_key.encode("utf-8"), WORKLOG_TOKEN_HKDF_INFO)


def create_worklog_token(
    service_key: str,
    *,
    org_id: uuid.UUID,
    factory_id: uuid.UUID,
    automation_id: uuid.UUID,
    automation_name: str,
    run_id: uuid.UUID,
    issued_at: datetime | None = None,
) -> str:
    """Sign a worklog run token.

    A run keeps the factory named here until the token expires, even if its
    automation is moved to another factory mid-run.
    """
    issued_at = issued_at or datetime.now(UTC)
    payload = {
        "aud": WORKLOG_TOKEN_AUDIENCE,
        "org_id": str(org_id),
        "factory_id": str(factory_id),
        "automation_id": str(automation_id),
        "automation_name": automation_name[:MAX_AUTOMATION_NAME_LENGTH],
        "run_id": str(run_id),
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + WORKLOG_TOKEN_EXPIRATION).timestamp()),
    }
    return jwt.encode(
        payload, derive_worklog_signing_key(service_key), algorithm="HS256"
    )
