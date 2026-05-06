"""Tests for webhook utility functions."""

import hashlib
import hmac
import uuid
from datetime import UTC

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from automation.db import set_sqlite_mode
from automation.models import Automation, Base
from automation.utils.webhook import get_event_automations, verify_signature


class TestVerifySignature:
    """Tests for HMAC signature verification."""

    def test_valid_signature(self):
        """Valid signature should return True."""
        payload = b'{"event": "test"}'
        secret = "test-secret-key"

        # Generate valid signature
        expected_sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        signature = f"sha256={expected_sig}"

        assert verify_signature(payload, signature, secret) is True

    def test_invalid_signature(self):
        """Invalid signature should return False."""
        payload = b'{"event": "test"}'
        secret = "test-secret-key"

        # Wrong signature (64 hex chars)
        signature = "sha256=" + "0" * 64

        assert verify_signature(payload, signature, secret) is False

    def test_signature_formats(self):
        """Both raw hex and sha256= prefixed signatures should work."""
        payload = b'{"event": "test"}'
        secret = "test-secret-key"

        # Generate valid hash
        expected_sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

        # Raw hex without prefix should work (e.g., Linear's format)
        assert verify_signature(payload, expected_sig, secret) is True

        # With sha256= prefix should also work (GitHub's format)
        assert verify_signature(payload, f"sha256={expected_sig}", secret) is True

        # Wrong prefix should fail (sha1= is not supported)
        assert verify_signature(payload, f"sha1={expected_sig}", secret) is False

    def test_empty_signature(self):
        """Empty signature should return False."""
        payload = b'{"event": "test"}'
        secret = "test-secret-key"

        assert verify_signature(payload, "", secret) is False

    def test_different_secret(self):
        """Signature with different secret should return False."""
        payload = b'{"event": "test"}'
        secret1 = "secret-one"
        secret2 = "secret-two"

        # Sign with secret1
        expected_sig = hmac.new(secret1.encode(), payload, hashlib.sha256).hexdigest()
        signature = f"sha256={expected_sig}"

        # Verify with secret2 - should fail
        assert verify_signature(payload, signature, secret2) is False

    def test_modified_payload(self):
        """Signature should fail if payload is modified."""
        original_payload = b'{"event": "test"}'
        modified_payload = b'{"event": "modified"}'
        secret = "test-secret-key"

        # Sign original
        expected_sig = hmac.new(
            secret.encode(), original_payload, hashlib.sha256
        ).hexdigest()
        signature = f"sha256={expected_sig}"

        # Verify modified - should fail
        assert verify_signature(modified_payload, signature, secret) is False

    def test_unicode_payload(self):
        """Signature verification should work with unicode payloads."""
        payload = '{"message": "こんにちは"}'.encode()
        secret = "test-secret-key"

        expected_sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        signature = f"sha256={expected_sig}"

        assert verify_signature(payload, signature, secret) is True

    def test_empty_payload(self):
        """Empty payload should still verify correctly."""
        payload = b""
        secret = "test-secret-key"

        expected_sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        signature = f"sha256={expected_sig}"

        assert verify_signature(payload, signature, secret) is True


@pytest.fixture
async def sqlite_session():
    """Create a test SQLite database session."""
    # Use in-memory SQLite for tests
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )

    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Create session factory
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with session_factory() as session:
        yield session

    await engine.dispose()


class TestGetEventAutomationsSqliteJsonFiltering:
    """Tests for get_event_automations with SQLite json_extract filtering."""

    @pytest.mark.asyncio
    async def test_filters_by_event_source(self, sqlite_session):
        """SQLite json_extract correctly filters events by source."""
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()

        # Set SQLite mode
        set_sqlite_mode(True)

        try:
            # Create an automation with github event trigger
            github_automation = Automation(
                id=uuid.uuid4(),
                name="GitHub Automation",
                org_id=org_id,
                user_id=user_id,
                trigger={"type": "event", "source": "github", "on": "push"},
                tarball_path="test.tar.gz",
                entrypoint="main.py",
                enabled=True,
            )

            # Create an automation with linear event trigger
            linear_automation = Automation(
                id=uuid.uuid4(),
                name="Linear Automation",
                org_id=org_id,
                user_id=user_id,
                trigger={
                    "type": "event",
                    "source": "linear",
                    "on": "issue.created",
                },
                tarball_path="test.tar.gz",
                entrypoint="main.py",
                enabled=True,
            )

            # Create a cron automation (should not be returned)
            cron_automation = Automation(
                id=uuid.uuid4(),
                name="Cron Automation",
                org_id=org_id,
                user_id=user_id,
                trigger={"type": "cron", "schedule": "0 * * * *", "timezone": "UTC"},
                tarball_path="test.tar.gz",
                entrypoint="main.py",
                enabled=True,
            )

            sqlite_session.add_all(
                [github_automation, linear_automation, cron_automation]
            )
            await sqlite_session.commit()

            # Query for github source
            result = await get_event_automations(org_id, "github", sqlite_session)

            # Should only return github automation
            assert len(result) == 1
            assert result[0][0].id == github_automation.id
            assert result[0][1].source == "github"
        finally:
            set_sqlite_mode(False)

    @pytest.mark.asyncio
    async def test_handles_missing_keys_gracefully(self, sqlite_session):
        """SQLite json_extract handles missing keys without crashing."""
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()

        set_sqlite_mode(True)

        try:
            # Create an automation with incomplete trigger data (missing source)
            incomplete_automation = Automation(
                id=uuid.uuid4(),
                name="Incomplete Automation",
                org_id=org_id,
                user_id=user_id,
                trigger={"type": "event"},  # Missing source key
                tarball_path="test.tar.gz",
                entrypoint="main.py",
                enabled=True,
            )

            sqlite_session.add(incomplete_automation)
            await sqlite_session.commit()

            # Query should not crash, should return empty result
            result = await get_event_automations(org_id, "github", sqlite_session)
            assert len(result) == 0
        finally:
            set_sqlite_mode(False)

    @pytest.mark.asyncio
    async def test_excludes_disabled_automations(self, sqlite_session):
        """Disabled automations are not returned."""
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()

        set_sqlite_mode(True)

        try:
            # Create enabled automation
            enabled_automation = Automation(
                id=uuid.uuid4(),
                name="Enabled Automation",
                org_id=org_id,
                user_id=user_id,
                trigger={"type": "event", "source": "github", "on": "push"},
                tarball_path="test.tar.gz",
                entrypoint="main.py",
                enabled=True,
            )

            # Create disabled automation
            disabled_automation = Automation(
                id=uuid.uuid4(),
                name="Disabled Automation",
                org_id=org_id,
                user_id=user_id,
                trigger={"type": "event", "source": "github", "on": "push"},
                tarball_path="test.tar.gz",
                entrypoint="main.py",
                enabled=False,
            )

            sqlite_session.add_all([enabled_automation, disabled_automation])
            await sqlite_session.commit()

            result = await get_event_automations(org_id, "github", sqlite_session)

            assert len(result) == 1
            assert result[0][0].id == enabled_automation.id
        finally:
            set_sqlite_mode(False)

    @pytest.mark.asyncio
    async def test_excludes_deleted_automations(self, sqlite_session):
        """Deleted automations are not returned."""
        from datetime import datetime

        org_id = uuid.uuid4()
        user_id = uuid.uuid4()

        set_sqlite_mode(True)

        try:
            # Create active automation
            active_automation = Automation(
                id=uuid.uuid4(),
                name="Active Automation",
                org_id=org_id,
                user_id=user_id,
                trigger={"type": "event", "source": "github", "on": "push"},
                tarball_path="test.tar.gz",
                entrypoint="main.py",
                enabled=True,
            )

            # Create deleted automation
            deleted_automation = Automation(
                id=uuid.uuid4(),
                name="Deleted Automation",
                org_id=org_id,
                user_id=user_id,
                trigger={"type": "event", "source": "github", "on": "push"},
                tarball_path="test.tar.gz",
                entrypoint="main.py",
                enabled=True,
                deleted_at=datetime.now(UTC),
            )

            sqlite_session.add_all([active_automation, deleted_automation])
            await sqlite_session.commit()

            result = await get_event_automations(org_id, "github", sqlite_session)

            assert len(result) == 1
            assert result[0][0].id == active_automation.id
        finally:
            set_sqlite_mode(False)
