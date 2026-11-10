"""The worklog run token: key derivation, the contract test vector, when a run
gets one, and that it never leaves the run environment.

The vector below is the contract with OpenHands/enterprise#1297: the same
service key, claims and token must verify in the Enterprise server's tests.
"""

import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from openhands.automation.config import clear_config_cache
from openhands.automation.dispatcher import _execute_run
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
    Factory,
)
from openhands.automation.utils import utcnow
from openhands.automation.utils.worklog import (
    MAX_AUTOMATION_NAME_LENGTH,
    create_worklog_token,
    derive_worklog_signing_key,
    hkdf_sha256,
    worklog_token_for_run,
)


SERVICE_KEY = "worklog-contract-test-service-key"
ORG_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
FACTORY_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
AUTOMATION_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
RUN_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
ISSUED_AT = datetime(2026, 1, 1, tzinfo=UTC)

CONTRACT_KEY_HEX = "0b0aff6f8d9323b8147f5a6ebbaee2a2e56f64749c3c146f1d775f943ec2dfd5"
CONTRACT_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJhdWQiOiJ3b3JrbG9nIiwib3JnX2lkIjoiMTExMTExMTEtMTExMS00MTExLTgxMTEtMTExMTEx"
    "MTExMTExIiwiZmFjdG9yeV9pZCI6IjIyMjIyMjIyLTIyMjItNDIyMi04MjIyLTIyMjIyMjIyMjIy"
    "MiIsImF1dG9tYXRpb25faWQiOiIzMzMzMzMzMy0zMzMzLTQzMzMtODMzMy0zMzMzMzMzMzMzMzMi"
    "LCJhdXRvbWF0aW9uX25hbWUiOiJOaWdodGx5IHRyaWFnZSIsInJ1bl9pZCI6IjQ0NDQ0NDQ0LTQ0"
    "NDQtNDQ0NC04NDQ0LTQ0NDQ0NDQ0NDQ0NCIsImlhdCI6MTc2NzIyNTYwMCwiZXhwIjoxNzY3MzEy"
    "MDAwfQ."
    "hz3p3HNfplM47XqD5wOlxwMcixO_giorPh2Bv02udbY"
)
CONTRACT_CLAIMS = {
    "aud": "worklog",
    "org_id": str(ORG_ID),
    "factory_id": str(FACTORY_ID),
    "automation_id": str(AUTOMATION_ID),
    "automation_name": "Nightly triage",
    "run_id": str(RUN_ID),
    "iat": 1767225600,
    "exp": 1767312000,
}


def _token(automation_name: str = "Nightly triage") -> str:
    return create_worklog_token(
        SERVICE_KEY,
        org_id=ORG_ID,
        factory_id=FACTORY_ID,
        automation_id=AUTOMATION_ID,
        automation_name=automation_name,
        run_id=RUN_ID,
        now=ISSUED_AT,
    )


def _config(monkeypatch, *, service_key: str | None, local: bool = False) -> None:
    for name in ("AUTOMATION_SERVICE_KEY", "AUTOMATION_AGENT_SERVER_URL"):
        monkeypatch.delenv(name, raising=False)
    if service_key:
        monkeypatch.setenv("AUTOMATION_SERVICE_KEY", service_key)
    if local:
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
    clear_config_cache()


@pytest.fixture(autouse=True)
def _reset_config() -> Iterator[None]:
    yield
    clear_config_cache()


# --- Key derivation ---


@pytest.mark.parametrize(
    ("key_material", "salt", "info", "length", "expected"),
    [
        pytest.param(
            bytes.fromhex("0b" * 22),
            bytes(range(13)),
            bytes(range(0xF0, 0xFA)),
            42,
            "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
            "34007208d5b887185865",
            id="rfc5869-a1-with-salt-and-info",
        ),
        pytest.param(
            bytes.fromhex("0b" * 22),
            b"",
            b"",
            42,
            "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d"
            "9d201395faa4b61a96c8",
            id="rfc5869-a3-no-salt-no-info",
        ),
    ],
)
def test_hkdf_matches_the_rfc_5869_vectors(key_material, salt, info, length, expected):
    assert hkdf_sha256(key_material, info, length, salt).hex() == expected


def test_signing_key_is_derived_not_the_service_key():
    key = derive_worklog_signing_key(SERVICE_KEY)

    assert key.hex() == CONTRACT_KEY_HEX
    assert key != SERVICE_KEY.encode()


# --- Contract vector ---


def test_the_contract_vector_is_reproduced_exactly():
    assert _token() == CONTRACT_TOKEN


def test_the_contract_vector_verifies_with_the_derived_key():
    claims = jwt.decode(
        CONTRACT_TOKEN,
        bytes.fromhex(CONTRACT_KEY_HEX),
        algorithms=["HS256"],
        audience="worklog",
        options={"verify_exp": False},
    )

    assert claims == CONTRACT_CLAIMS
    assert list(claims) == list(CONTRACT_CLAIMS)
    assert claims["exp"] - claims["iat"] == 24 * 3600


def test_the_raw_service_key_does_not_verify_the_token():
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(
            CONTRACT_TOKEN,
            SERVICE_KEY,
            algorithms=["HS256"],
            audience="worklog",
            options={"verify_exp": False},
        )


def test_the_automation_name_is_cut_to_256_characters():
    claims = jwt.decode(
        _token("n" * 300),
        derive_worklog_signing_key(SERVICE_KEY),
        algorithms=["HS256"],
        audience="worklog",
        options={"verify_exp": False},
    )

    assert claims["automation_name"] == "n" * MAX_AUTOMATION_NAME_LENGTH


# --- When a run gets a token ---


def _rows(factory_id: uuid.UUID | None) -> tuple[Automation, AutomationRun]:
    automation = Automation(
        id=AUTOMATION_ID,
        user_id=uuid.uuid4(),
        org_id=ORG_ID,
        factory_id=factory_id,
        name="Nightly triage",
        trigger={},
        tarball_path="x",
        entrypoint="x",
    )
    return automation, AutomationRun(id=RUN_ID, automation_id=AUTOMATION_ID)


def test_a_run_of_an_automation_in_a_factory_gets_a_token(monkeypatch):
    _config(monkeypatch, service_key=SERVICE_KEY)
    automation, run = _rows(FACTORY_ID)

    token = worklog_token_for_run(automation, run)

    assert token is not None
    claims = jwt.decode(
        token,
        derive_worklog_signing_key(SERVICE_KEY),
        algorithms=["HS256"],
        audience="worklog",
    )
    assert {k: v for k, v in claims.items() if k not in ("iat", "exp")} == {
        k: v for k, v in CONTRACT_CLAIMS.items() if k not in ("iat", "exp")
    }
    assert claims["exp"] - claims["iat"] == 24 * 3600


@pytest.mark.parametrize(
    ("factory_id", "service_key", "local"),
    [
        pytest.param(None, SERVICE_KEY, False, id="no-factory"),
        pytest.param(FACTORY_ID, SERVICE_KEY, True, id="local-mode"),
        pytest.param(FACTORY_ID, None, False, id="no-service-key"),
    ],
)
def test_no_token_without_a_factory_a_service_key_or_in_local_mode(
    monkeypatch, factory_id, service_key, local
):
    _config(monkeypatch, service_key=service_key, local=local)
    automation, run = _rows(factory_id)

    assert worklog_token_for_run(automation, run) is None


# --- Dispatch ---


async def _dispatch(
    async_session_factory, mock_settings, factory_id: uuid.UUID | None
) -> tuple[dict, uuid.UUID]:
    """Dispatch one run of an automation; returns the sandbox env and run id."""
    async with async_session_factory() as session:
        if factory_id is not None:
            session.add(Factory(id=factory_id, org_id=ORG_ID, name="F", slug="f"))
            await session.flush()
        automation = Automation(
            user_id=uuid.uuid4(),
            org_id=ORG_ID,
            factory_id=factory_id,
            name="Nightly triage",
            trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
            tarball_path="https://example.com/code.tar.gz",
            entrypoint="uv run main.py",
            enabled=True,
        )
        session.add(automation)
        await session.flush()
        run = AutomationRun(
            automation_id=automation.id,
            status=AutomationRunStatus.RUNNING,
            started_at=utcnow(),
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    async with async_session_factory() as session:
        loaded = (
            await session.execute(
                select(AutomationRun)
                .options(selectinload(AutomationRun.automation))
                .where(AutomationRun.id == run_id)
            )
        ).scalar_one()

    backend = MagicMock()
    backend.get_execution_context = AsyncMock(
        return_value=MagicMock(
            agent_url="http://agent.test",
            sandbox_id="sbx-1",
            session_key="sk-1",
            api_key="cloud-api-key",
        )
    )
    backend.build_env_vars = MagicMock(return_value={})
    backend.get_work_dir = MagicMock(return_value="/workspace")
    with (
        patch("openhands.automation.dispatcher.get_backend", return_value=backend),
        patch(
            "openhands.automation.dispatcher.execute_in_context",
            new_callable=AsyncMock,
        ) as execute,
    ):
        execute.return_value = MagicMock(
            success=True, bash_command_id="cmd-1", error=None
        )
        await _execute_run(loaded, mock_settings, async_session_factory, MagicMock())
        call = execute.await_args
        assert call is not None
        return call.kwargs["env_vars"], run_id


async def test_dispatch_injects_a_token_that_names_the_run_and_its_factory(
    async_session_factory, mock_settings, monkeypatch
):
    _config(monkeypatch, service_key=SERVICE_KEY)
    factory_id = uuid.uuid4()

    env_vars, run_id = await _dispatch(async_session_factory, mock_settings, factory_id)

    claims = jwt.decode(
        env_vars["AUTOMATION_WORKLOG_TOKEN"],
        derive_worklog_signing_key(SERVICE_KEY),
        algorithms=["HS256"],
        audience="worklog",
    )
    assert (claims["factory_id"], claims["run_id"], claims["org_id"]) == (
        str(factory_id),
        str(run_id),
        str(ORG_ID),
    )


@pytest.mark.parametrize(
    ("with_factory", "service_key", "local"),
    [
        pytest.param(False, SERVICE_KEY, False, id="no-factory"),
        pytest.param(True, SERVICE_KEY, True, id="local-mode"),
        pytest.param(True, None, False, id="no-service-key"),
    ],
)
async def test_dispatch_starts_the_run_without_a_token_when_none_applies(
    async_session_factory, mock_settings, monkeypatch, with_factory, service_key, local
):
    _config(monkeypatch, service_key=service_key, local=local)

    env_vars, run_id = await _dispatch(
        async_session_factory, mock_settings, uuid.uuid4() if with_factory else None
    )

    assert "AUTOMATION_WORKLOG_TOKEN" not in env_vars
    assert env_vars["AUTOMATION_PHASE_URL"].endswith(f"/v1/runs/{run_id}/phase")


async def test_the_token_is_not_stored_logged_or_put_in_the_event_payload(
    async_session_factory, mock_settings, monkeypatch, caplog
):
    _config(monkeypatch, service_key=SERVICE_KEY)
    caplog.set_level(logging.DEBUG)

    env_vars, run_id = await _dispatch(
        async_session_factory, mock_settings, uuid.uuid4()
    )

    token = env_vars["AUTOMATION_WORKLOG_TOKEN"]
    async with async_session_factory() as session:
        run = await session.get(AutomationRun, run_id)
        assert run is not None
        stored = json.dumps(
            {c.key: getattr(run, c.key) for c in run.__table__.columns},
            default=str,
        )
    assert token not in stored
    assert token not in caplog.text
    assert token not in env_vars["AUTOMATION_EVENT_PAYLOAD"]
