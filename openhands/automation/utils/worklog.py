"""The run token that lets a run write to its factory's worklogs.

Worklogs live in the OpenHands Enterprise server (OpenHands/enterprise#1297),
which verifies this token and calls nothing here. This service is the only
component that knows which factory an automation belongs to, so it mints the
token at dispatch and injects it as ``AUTOMATION_WORKLOG_TOKEN``.

The token is a contract with Enterprise, which holds the same service key as
``AUTOMATIONS_SERVICE_KEY``:

- JWT, HS256, signed with a key derived from the service key. The derivation is
  HKDF-SHA256 (RFC 5869) with the service key's UTF-8 bytes as input keying
  material, no salt, the info string ``openhands-worklog-run-token-v1`` and a
  32-byte output. The raw service key is never the signing key.
- Claims, in this order: ``aud`` (``worklog``), ``org_id``, ``factory_id``,
  ``automation_id``, ``automation_name`` (at most 256 characters), ``run_id``,
  ``iat`` and ``exp`` (24 hours after ``iat``), both in whole seconds.

The token is a credential: it is never stored or logged.
"""

import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

import jwt

from openhands.automation.config import get_config
from openhands.automation.models import Automation, AutomationRun


WORKLOG_TOKEN_AUDIENCE: Final[str] = "worklog"
WORKLOG_TOKEN_LIFETIME: Final[timedelta] = timedelta(hours=24)
WORKLOG_KEY_INFO: Final[bytes] = b"openhands-worklog-run-token-v1"
MAX_AUTOMATION_NAME_LENGTH: Final[int] = 256

_HASH = hashlib.sha256
_HASH_LENGTH = _HASH().digest_size


def hkdf_sha256(
    key_material: bytes, info: bytes, length: int, salt: bytes = b""
) -> bytes:
    """HKDF extract-and-expand (RFC 5869) with SHA-256."""
    if length > 255 * _HASH_LENGTH:
        raise ValueError("HKDF output is too long")
    pseudo_random_key = hmac.new(
        salt or bytes(_HASH_LENGTH), key_material, _HASH
    ).digest()
    output = b""
    block = b""
    counter = 1
    while len(output) < length:
        block = hmac.new(
            pseudo_random_key, block + info + bytes([counter]), _HASH
        ).digest()
        output += block
        counter += 1
    return output[:length]


def derive_worklog_signing_key(service_key: str) -> bytes:
    return hkdf_sha256(service_key.encode(), WORKLOG_KEY_INFO, 32)


def create_worklog_token(
    service_key: str,
    *,
    org_id: uuid.UUID,
    factory_id: uuid.UUID,
    automation_id: uuid.UUID,
    automation_name: str,
    run_id: uuid.UUID,
    now: datetime | None = None,
) -> str:
    issued_at = now or datetime.now(UTC)
    payload = {
        "aud": WORKLOG_TOKEN_AUDIENCE,
        "org_id": str(org_id),
        "factory_id": str(factory_id),
        "automation_id": str(automation_id),
        "automation_name": automation_name[:MAX_AUTOMATION_NAME_LENGTH],
        "run_id": str(run_id),
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + WORKLOG_TOKEN_LIFETIME).timestamp()),
    }
    return jwt.encode(
        payload, derive_worklog_signing_key(service_key), algorithm="HS256"
    )


def worklog_token_for_run(automation: Automation, run: AutomationRun) -> str | None:
    """The token for a run of an automation in a factory, or None.

    There is no token for an automation outside a factory, in local mode (no
    Enterprise server) or without a service key. The run keeps the factory
    named in its token until the token expires, even if the automation moves to
    another factory meanwhile.
    """
    service = get_config().service
    if automation.factory_id is None or service.is_local_mode:
        return None
    if not service.service_key:
        return None
    return create_worklog_token(
        service.service_key,
        org_id=automation.org_id,
        factory_id=automation.factory_id,
        automation_id=automation.id,
        automation_name=automation.name,
        run_id=run.id,
    )
