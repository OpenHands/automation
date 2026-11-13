"""The worklog run token: key derivation, the shared test vector, and what a
dispatched run gets.

The vector below is the contract with OpenHands/enterprise#1297, whose tests
commit the same key, claims and token. If the two ever differ, the enterprise
issue is authoritative.
"""

import json
import logging
import uuid
from datetime import UTC, datetime

import jwt
import pytest
from sqlalchemy import select

from openhands.automation.config import Settings
from openhands.automation.execution import build_tarball
from openhands.automation.models import AutomationRun
from openhands.automation.utils.worklog import (
    MAX_AUTOMATION_NAME_LENGTH,
    create_worklog_token,
    derive_worklog_signing_key,
    hkdf_sha256,
)
from tests.test_cloud_run_env import SCRIPT_FILES, _dispatch


VECTOR_SERVICE_KEY = "worklog-contract-service-key"
VECTOR_ISSUED_AT = datetime(2026, 1, 1, tzinfo=UTC)
VECTOR_CLAIMS = {
    "aud": "worklog",
    "org_id": "11111111-1111-4111-8111-111111111111",
    "factory_id": "22222222-2222-4222-8222-222222222222",
    "automation_id": "33333333-3333-4333-8333-333333333333",
    "automation_name": "Release orchestrator",
    "run_id": "44444444-4444-4444-8444-444444444444",
    "iat": 1767225600,
    "exp": 1767312000,
}
VECTOR_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJhdWQiOiJ3b3JrbG9nIiwib3JnX2lkIjoiMTExMTExMTEtMTExMS00MTExLTgxMTEtMTExMTEx"
    "MTExMTExIiwiZmFjdG9yeV9pZCI6IjIyMjIyMjIyLTIyMjItNDIyMi04MjIyLTIyMjIyMjIyMjIy"
    "MiIsImF1dG9tYXRpb25faWQiOiIzMzMzMzMzMy0zMzMzLTQzMzMtODMzMy0zMzMzMzMzMzMzMzMi"
    "LCJhdXRvbWF0aW9uX25hbWUiOiJSZWxlYXNlIG9yY2hlc3RyYXRvciIsInJ1bl9pZCI6IjQ0NDQ0"
    "NDQ0LTQ0NDQtNDQ0NC04NDQ0LTQ0NDQ0NDQ0NDQ0NCIsImlhdCI6MTc2NzIyNTYwMCwiZXhwIjox"
    "NzY3MzEyMDAwfQ."
    "uf3R42EbWDlV0HrrVke_eJzQvW8vBFvCORKeBxFfua8"
)
FACTORY_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")


def _token(**overrides) -> str:
    values = dict(
        org_id=uuid.UUID(VECTOR_CLAIMS["org_id"]),
        factory_id=FACTORY_ID,
        automation_id=uuid.UUID(VECTOR_CLAIMS["automation_id"]),
        automation_name="Release orchestrator",
        run_id=uuid.UUID(VECTOR_CLAIMS["run_id"]),
        issued_at=VECTOR_ISSUED_AT,
    )
    values.update(overrides)
    return create_worklog_token(VECTOR_SERVICE_KEY, **values)


# --- Key derivation and the contract vector ---


def test_hkdf_matches_rfc_5869_test_case_3():
    okm = hkdf_sha256(bytes([0x0B] * 22), b"", 42)

    assert okm.hex() == (
        "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d"
        "9d201395faa4b61a96c8"
    )


def test_signing_key_is_derived_not_the_service_key():
    key = derive_worklog_signing_key(VECTOR_SERVICE_KEY)

    assert len(key) == 32
    assert key != VECTOR_SERVICE_KEY.encode()


def test_the_vector_token_is_what_this_service_mints():
    assert _token() == VECTOR_TOKEN


def test_the_vector_token_verifies_with_the_derived_key():
    claims = jwt.decode(
        VECTOR_TOKEN,
        derive_worklog_signing_key(VECTOR_SERVICE_KEY),
        algorithms=["HS256"],
        audience="worklog",
        options={"verify_exp": False},
    )

    assert claims == VECTOR_CLAIMS


def test_the_raw_service_key_does_not_verify_a_token():
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(
            VECTOR_TOKEN,
            VECTOR_SERVICE_KEY,
            algorithms=["HS256"],
            options={"verify_exp": False, "verify_aud": False},
        )


def test_claims_are_exactly_the_contract_and_expire_after_24_hours():
    claims = jwt.decode(
        _token(), options={"verify_signature": False, "verify_exp": False}
    )

    assert list(claims) == list(VECTOR_CLAIMS)
    assert claims["exp"] - claims["iat"] == 24 * 60 * 60


def test_automation_name_is_truncated_to_256_characters():
    claims = jwt.decode(
        _token(automation_name="n" * 1000),
        options={"verify_signature": False, "verify_exp": False},
    )

    assert claims["automation_name"] == "n" * MAX_AUTOMATION_NAME_LENGTH
    assert MAX_AUTOMATION_NAME_LENGTH == 256


def test_a_different_service_key_gives_a_token_that_does_not_verify():
    other = create_worklog_token(
        "another-key",
        org_id=uuid.uuid4(),
        factory_id=FACTORY_ID,
        automation_id=uuid.uuid4(),
        automation_name="x",
        run_id=uuid.uuid4(),
    )

    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(
            other,
            derive_worklog_signing_key(VECTOR_SERVICE_KEY),
            algorithms=["HS256"],
            audience="worklog",
        )


# --- What a dispatched run gets ---


def _settings(**overrides) -> Settings:
    values = dict(
        openhands_api_base_url="https://test.example.com",
        service_key="test-service-key",
        base_url="http://localhost:8000",
    )
    values.update(overrides)
    return Settings(**values)


async def test_a_factory_run_gets_a_token_naming_its_factory(
    async_session_factory, mock_client
):
    runs: list[AutomationRun] = []

    env = await _dispatch(
        async_session_factory,
        _settings(),
        mock_client,
        build_tarball(SCRIPT_FILES),
        factory_id=FACTORY_ID,
        run_out=runs,
    )

    claims = jwt.decode(
        env["AUTOMATION_WORKLOG_TOKEN"],
        derive_worklog_signing_key("test-service-key"),
        algorithms=["HS256"],
        audience="worklog",
    )
    assert set(claims) == set(VECTOR_CLAIMS)
    assert claims["factory_id"] == str(FACTORY_ID)
    assert claims["automation_id"] == str(runs[0].automation_id)
    assert claims["run_id"] == str(runs[0].id)
    assert claims["automation_name"] == "Stored tarball"
    assert claims["exp"] - claims["iat"] == 24 * 60 * 60


async def test_a_run_without_a_factory_gets_no_token_and_starts(
    async_session_factory, mock_client
):
    env = await _dispatch(
        async_session_factory,
        _settings(),
        mock_client,
        build_tarball(SCRIPT_FILES),
    )

    assert "AUTOMATION_WORKLOG_TOKEN" not in env
    assert env["AUTOMATION_RUN_ID"]


async def test_a_run_without_a_service_key_gets_no_token_and_starts(
    async_session_factory, mock_client
):
    env = await _dispatch(
        async_session_factory,
        _settings(service_key=""),
        mock_client,
        build_tarball(SCRIPT_FILES),
        factory_id=FACTORY_ID,
    )

    assert "AUTOMATION_WORKLOG_TOKEN" not in env
    assert env["AUTOMATION_RUN_ID"]


async def test_a_local_mode_run_gets_no_token_and_starts(
    async_session_factory, mock_client
):
    env = await _dispatch(
        async_session_factory,
        _settings(agent_server_url="http://localhost:3000"),
        mock_client,
        build_tarball(SCRIPT_FILES),
        mode="local",
        factory_id=FACTORY_ID,
    )

    assert "AUTOMATION_WORKLOG_TOKEN" not in env
    assert env["AUTOMATION_RUN_ID"]


async def test_the_token_is_not_stored_or_logged(
    async_session_factory, mock_client, caplog
):
    runs: list[AutomationRun] = []
    caplog.set_level(logging.DEBUG)

    env = await _dispatch(
        async_session_factory,
        _settings(),
        mock_client,
        build_tarball(SCRIPT_FILES),
        factory_id=FACTORY_ID,
        run_out=runs,
    )

    token = env["AUTOMATION_WORKLOG_TOKEN"]
    assert token not in caplog.text
    assert token not in env["AUTOMATION_EVENT_PAYLOAD"]
    async with async_session_factory() as session:
        stored = (
            await session.execute(
                select(AutomationRun).where(AutomationRun.id == runs[0].id)
            )
        ).scalar_one()
    columns = {c.name: getattr(stored, c.name) for c in AutomationRun.__table__.columns}
    assert token not in json.dumps(columns, default=str)
