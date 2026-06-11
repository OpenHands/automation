"""Integration tests for A/B testing plugin variants.

Uses SQLite (no Docker needed) to test the full flow:
  1. Create an A/B test automation via POST /v1/preset/plugin
  2. Verify the API response
  3. Inspect the generated tarball (experiment_config.json)
  4. Simulate the sdk_main.py variant selection logic
  5. Verify backward compatibility with standard plugin automations
"""

import io
import json
import os
import random
import tarfile
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


os.environ["LOG_JSON"] = "0"

from openhands.automation.app import app  # noqa: E402
from openhands.automation.auth import (  # noqa: E402
    AuthenticatedUser,
    AuthMethod,
    authenticate_request,
    create_http_client,
)
from openhands.automation.db import get_session  # noqa: E402
from openhands.automation.models import Base  # noqa: E402
from openhands.automation.storage import get_file_store  # noqa: E402


# --- Fixtures (SQLite-based, no Docker) ---

TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")


@pytest.fixture
async def async_engine():
    """Create an in-memory SQLite engine."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def async_session_factory(async_engine):
    return async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


@pytest.fixture
async def async_session(async_session_factory) -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session


@pytest.fixture
def mock_authenticated_user():
    return AuthenticatedUser(
        user_id=TEST_USER_ID,
        org_id=TEST_ORG_ID,
        email="test@example.com",
        role="owner",
        permissions=["view_org_settings", "manage_automations"],
        auth_method=AuthMethod.API_KEY,
        api_key="test-api-key",
    )


@pytest.fixture
def mock_file_store():
    """Mock file store that captures uploaded content."""
    store = MagicMock()
    store._captured_content = None

    async def mock_write_stream(
        path: str,
        stream: AsyncIterator[bytes],
        max_size: int | None = None,
        content_type: str = "application/octet-stream",
    ) -> int:
        content = b""
        async for chunk in stream:
            content += chunk
        store._captured_content = content
        return len(content)

    store.write_stream = AsyncMock(side_effect=mock_write_stream)
    store.delete = MagicMock()
    return store


@pytest.fixture
async def client(
    async_engine,
    async_session_factory,
    async_session,
    mock_authenticated_user,
    mock_file_store,
) -> AsyncGenerator[AsyncClient, None]:
    """Async test client with SQLite DB, mock auth, and mock file store."""

    async def override_get_session():
        yield async_session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[authenticate_request] = lambda: mock_authenticated_user
    app.dependency_overrides[get_file_store] = lambda: mock_file_store

    app.state.engine = async_engine
    app.state.session_factory = async_session_factory
    app.state.http_client = create_http_client()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c

    await app.state.http_client.aclose()
    app.dependency_overrides.clear()


# --- Helpers ---


def _extract_tarball(mock_store) -> dict[str, bytes]:
    """Extract all files from the captured tarball."""
    assert mock_store._captured_content is not None
    files = {}
    with tarfile.open(
        fileobj=io.BytesIO(mock_store._captured_content), mode="r:gz"
    ) as tar:
        for member in tar.getmembers():
            f = tar.extractfile(member)
            if f:
                files[member.name] = f.read()
    return files


def _simulate_variant_selection(experiment_config: dict, seed: int = 42) -> str:
    """Select a variant using the same weighted-random logic as sdk_main.py.

    Uses a seeded RNG for deterministic test assertions; production code
    uses the global (unseeded) random module.
    """
    rng = random.Random(seed)
    variants = experiment_config["variants"]
    weights = [v["weight"] for v in variants]
    selected = rng.choices(variants, weights=weights, k=1)[0]
    return selected["name"]


# --- Tests ---


class TestABTestAutomationCreation:
    """End-to-end: create an A/B test automation via the API."""

    AB_PAYLOAD: ClassVar[dict] = {
        "name": "PR Review A/B Test",
        "experiment_id": "pr-review-v2-test",
        "variants": [
            {
                "name": "control",
                "weight": 70,
                "plugins": [
                    {
                        "source": "github:OpenHands/extensions",
                        "repo_path": "plugins/pr-review",
                        "ref": "v1.0.0",
                    },
                ],
            },
            {
                "name": "treatment",
                "weight": 30,
                "plugins": [
                    {
                        "source": "github:OpenHands/extensions",
                        "repo_path": "plugins/pr-review",
                        "ref": "v2.0.0",
                    },
                ],
            },
        ],
        "prompt": "Review this PR for code quality and potential bugs.",
        "trigger": {
            "type": "event",
            "source": "github",
            "on": "pull_request.opened",
        },
    }

    async def test_create_ab_automation_returns_201(self, client, mock_file_store):
        """POST with variants returns 201 and correct metadata."""
        resp = await client.post(
            "/api/automation/v1/preset/plugin",
            json=self.AB_PAYLOAD,
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()

        assert data["name"] == "PR Review A/B Test"
        assert data["prompt"] == self.AB_PAYLOAD["prompt"]
        assert data["trigger"]["type"] == "event"
        assert data["tarball_path"].startswith("oh-internal://uploads/")
        assert data["enabled"] is True

    async def test_tarball_contains_experiment_config(self, client, mock_file_store):
        """Generated tarball has experiment_config.json, not plugins_config.json."""
        resp = await client.post(
            "/api/automation/v1/preset/plugin",
            json=self.AB_PAYLOAD,
        )
        assert resp.status_code == 201

        files = _extract_tarball(mock_file_store)
        assert "experiment_config.json" in files
        assert "plugins_config.json" not in files
        assert "main.py" in files
        assert "bootstrap.py" in files
        assert "prompt.txt" in files

    async def test_experiment_config_matches_request(self, client, mock_file_store):
        """experiment_config.json faithfully represents the request."""
        resp = await client.post(
            "/api/automation/v1/preset/plugin",
            json=self.AB_PAYLOAD,
        )
        assert resp.status_code == 201

        files = _extract_tarball(mock_file_store)
        config = json.loads(files["experiment_config.json"])

        assert config["experiment_id"] == "pr-review-v2-test"
        assert len(config["variants"]) == 2

        control = config["variants"][0]
        assert control["name"] == "control"
        assert control["weight"] == 70
        assert control["plugins"][0]["ref"] == "v1.0.0"

        treatment = config["variants"][1]
        assert treatment["name"] == "treatment"
        assert treatment["weight"] == 30
        assert treatment["plugins"][0]["ref"] == "v2.0.0"

    async def test_main_py_has_experiment_support(self, client, mock_file_store):
        """main.py template includes experiment detection and tagging code."""
        resp = await client.post(
            "/api/automation/v1/preset/plugin",
            json=self.AB_PAYLOAD,
        )
        assert resp.status_code == 201

        files = _extract_tarball(mock_file_store)
        main_py = files["main.py"].decode("utf-8")

        assert "experiment_config.json" in main_py
        assert "experiment_id" in main_py
        assert "selected_variant" in main_py
        assert "random.choices" in main_py
        assert "experiment_tags" in main_py


class TestVariantSelectionLogic:
    """Test the runtime variant selection as it would run in sdk_main.py."""

    EXPERIMENT_CONFIG: ClassVar[dict] = {
        "experiment_id": "test-experiment",
        "variants": [
            {
                "name": "control",
                "weight": 80,
                "plugins": [{"source": "github:o/r", "ref": "v1"}],
            },
            {
                "name": "treatment",
                "weight": 20,
                "plugins": [{"source": "github:o/r", "ref": "v2"}],
            },
        ],
    }

    def test_selection_respects_weights_distribution(self):
        """Over many runs, variant selection roughly follows weight ratios."""
        counts = {"control": 0, "treatment": 0}
        for seed in range(1000):
            name = _simulate_variant_selection(self.EXPERIMENT_CONFIG, seed=seed)
            counts[name] += 1

        # 80/20 weights → expect ~800/200 with some variance
        assert counts["control"] > 600, (
            f"control selected only {counts['control']}/1000 times"
        )
        assert counts["treatment"] > 100, (
            f"treatment selected only {counts['treatment']}/1000 times"
        )

    def test_deterministic_with_same_seed(self):
        """Same seed always picks the same variant."""
        v1 = _simulate_variant_selection(self.EXPERIMENT_CONFIG, seed=12345)
        v2 = _simulate_variant_selection(self.EXPERIMENT_CONFIG, seed=12345)
        assert v1 == v2

    def test_selected_variant_has_plugins(self):
        """The selected variant carries its plugin config."""
        variants = self.EXPERIMENT_CONFIG["variants"]
        weights = [v["weight"] for v in variants]
        rng = random.Random(42)
        selected = rng.choices(variants, weights=weights, k=1)[0]

        assert "plugins" in selected
        assert len(selected["plugins"]) == 1
        assert "source" in selected["plugins"][0]

    def test_equal_weights_both_variants_appear(self):
        """With 50/50 weights, both variants should appear over many runs."""
        config = {
            "experiment_id": "equal",
            "variants": [
                {"name": "a", "weight": 50, "plugins": [{"source": "github:o/r"}]},
                {"name": "b", "weight": 50, "plugins": [{"source": "github:o/r"}]},
            ],
        }
        seen = {_simulate_variant_selection(config, seed=s) for s in range(100)}
        assert seen == {"a", "b"}


class TestBackwardCompatibility:
    """Standard plugin automations still work exactly as before."""

    STANDARD_PAYLOAD: ClassVar[dict] = {
        "name": "Standard Plugin Automation",
        "plugins": [
            {"source": "github:owner/plugin", "ref": "v1.0.0"},
        ],
        "prompt": "Do something with the plugin.",
        "trigger": {"type": "cron", "schedule": "0 9 * * *"},
    }

    async def test_standard_plugin_automation_returns_201(
        self, client, mock_file_store
    ):
        """Standard (non-experiment) request still works."""
        resp = await client.post(
            "/api/automation/v1/preset/plugin",
            json=self.STANDARD_PAYLOAD,
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["name"] == "Standard Plugin Automation"

    async def test_standard_tarball_has_plugins_config(self, client, mock_file_store):
        """Standard tarball uses plugins_config.json, no experiment_config.json."""
        resp = await client.post(
            "/api/automation/v1/preset/plugin",
            json=self.STANDARD_PAYLOAD,
        )
        assert resp.status_code == 201

        files = _extract_tarball(mock_file_store)
        assert "plugins_config.json" in files
        assert "experiment_config.json" not in files

        config = json.loads(files["plugins_config.json"])
        assert len(config) == 1
        assert config[0]["source"] == "github:owner/plugin"
        assert config[0]["ref"] == "v1.0.0"


class TestABTestValidationViaAPI:
    """Validation errors return proper 422 responses through the API."""

    async def test_both_plugins_and_variants_rejected(self, client):
        resp = await client.post(
            "/api/automation/v1/preset/plugin",
            json={
                "name": "Bad",
                "plugins": [{"source": "github:o/r"}],
                "variants": [
                    {"name": "a", "weight": 1, "plugins": [{"source": "github:o/r"}]},
                    {"name": "b", "weight": 1, "plugins": [{"source": "github:o/r"}]},
                ],
                "prompt": "Test",
                "trigger": {"type": "cron", "schedule": "0 0 * * *"},
            },
        )
        assert resp.status_code == 422

    async def test_missing_experiment_id_rejected(self, client):
        resp = await client.post(
            "/api/automation/v1/preset/plugin",
            json={
                "name": "Bad",
                "variants": [
                    {"name": "a", "weight": 1, "plugins": [{"source": "github:o/r"}]},
                    {"name": "b", "weight": 1, "plugins": [{"source": "github:o/r"}]},
                ],
                "prompt": "Test",
                "trigger": {"type": "cron", "schedule": "0 0 * * *"},
            },
        )
        assert resp.status_code == 422

    async def test_single_variant_rejected(self, client):
        resp = await client.post(
            "/api/automation/v1/preset/plugin",
            json={
                "name": "Bad",
                "experiment_id": "test",
                "variants": [
                    {
                        "name": "only",
                        "weight": 1,
                        "plugins": [{"source": "github:o/r"}],
                    },
                ],
                "prompt": "Test",
                "trigger": {"type": "cron", "schedule": "0 0 * * *"},
            },
        )
        assert resp.status_code == 422

    async def test_duplicate_names_rejected(self, client):
        resp = await client.post(
            "/api/automation/v1/preset/plugin",
            json={
                "name": "Bad",
                "experiment_id": "test",
                "variants": [
                    {
                        "name": "same",
                        "weight": 1,
                        "plugins": [{"source": "github:o/r"}],
                    },
                    {
                        "name": "same",
                        "weight": 1,
                        "plugins": [{"source": "github:o/r"}],
                    },
                ],
                "prompt": "Test",
                "trigger": {"type": "cron", "schedule": "0 0 * * *"},
            },
        )
        assert resp.status_code == 422

    async def test_zero_weight_rejected(self, client):
        resp = await client.post(
            "/api/automation/v1/preset/plugin",
            json={
                "name": "Bad",
                "experiment_id": "test",
                "variants": [
                    {"name": "a", "weight": 0, "plugins": [{"source": "github:o/r"}]},
                    {"name": "b", "weight": 1, "plugins": [{"source": "github:o/r"}]},
                ],
                "prompt": "Test",
                "trigger": {"type": "cron", "schedule": "0 0 * * *"},
            },
        )
        assert resp.status_code == 422
