"""Short-lived credentials for capabilities granted to one automation run."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import jwt

from openhands.automation.config import ServiceSettings


SUBMIT_SUBJECT_TURN: Final[str] = "subject_turn:submit"
RUN_TOKEN_EXPIRATION_HOURS: Final[int] = 24


class RunTokenError(Exception):
    """A run token is absent, expired, malformed, or has the wrong scope."""


@dataclass(frozen=True, slots=True)
class RunTokenClaims:
    automation_id: uuid.UUID
    run_id: uuid.UUID
    scopes: frozenset[str]


def signing_secret(settings: ServiceSettings) -> str:
    """Return the deployment secret used only to sign scoped run tokens."""
    secret = settings.service_key or settings.local_api_key
    if not secret:
        raise RunTokenError(
            "AUTOMATION_SERVICE_KEY or AUTOMATION_LOCAL_API_KEY is required"
        )
    return secret


def create_run_token(
    *,
    secret: str,
    automation_id: uuid.UUID,
    run_id: uuid.UUID,
    scopes: tuple[str, ...],
) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "automation_id": str(automation_id),
            "run_id": str(run_id),
            "scopes": list(scopes),
            "iat": now,
            "exp": now + timedelta(hours=RUN_TOKEN_EXPIRATION_HOURS),
        },
        secret,
        algorithm="HS256",
    )


def verify_run_token(secret: str, token: str, required_scope: str) -> RunTokenClaims:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        scopes = frozenset(payload.get("scopes") or ())
        if required_scope not in scopes:
            raise RunTokenError("Token does not grant the required scope")
        return RunTokenClaims(
            automation_id=uuid.UUID(payload["automation_id"]),
            run_id=uuid.UUID(payload["run_id"]),
            scopes=scopes,
        )
    except RunTokenError:
        raise
    except (KeyError, TypeError, ValueError, jwt.PyJWTError) as exc:
        raise RunTokenError("Invalid or expired run token") from exc
