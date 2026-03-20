"""End-to-end integration tests for the Automations CRUD API.

These tests run against a **live** deployment of the automation service and
require two environment variables:

    OPENHANDS_API_KEY    – valid OpenHands API key for staging
    AUTOMATION_BASE_URL  – e.g. https://automation-automations-feature.staging.all-hands.dev

Run
---
    # single-process
    pytest automation/tests/integration/ -v

    # parallel (needs pytest-xdist)
    pytest automation/tests/integration/ -v -n auto
"""

import uuid

import httpx
import pytest
from conftest import make_automation_payload

# ── marks applied to every test in this module ──────────────────────────
pytestmark = pytest.mark.timeout(60)


# ======================================================================
# Health checks (independent – safe to parallelise)
# ======================================================================


class TestHealthChecks:
    def test_health_endpoint(self, client: httpx.Client, base_url: str):
        resp = client.get(f'{base_url}/health')
        assert resp.status_code == 200

    def test_ready_endpoint(self, client: httpx.Client, base_url: str):
        resp = client.get(f'{base_url}/ready')
        assert resp.status_code == 200


# ======================================================================
# Authentication (independent – safe to parallelise)
# ======================================================================


class TestAuthentication:
    def test_missing_auth_header_returns_401(self, client: httpx.Client, base_url: str):
        resp = client.get(f'{base_url}/api/v1/automations')
        assert resp.status_code == 401

    def test_invalid_api_key_returns_401(self, client: httpx.Client, base_url: str):
        resp = client.get(
            f'{base_url}/api/v1/automations',
            headers={'Authorization': 'Bearer totally-bogus-key'},
        )
        assert resp.status_code == 401

    def test_malformed_auth_header_returns_401(
        self, client: httpx.Client, base_url: str
    ):
        resp = client.get(
            f'{base_url}/api/v1/automations',
            headers={'Authorization': 'Token some-key'},
        )
        assert resp.status_code == 401


# ======================================================================
# Input validation (independent – safe to parallelise)
# ======================================================================


class TestValidation:
    """Verify the server rejects obviously bad payloads with 422."""

    def test_invalid_cron_expression(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        payload = make_automation_payload()
        payload['trigger']['schedule'] = 'not-a-cron'
        resp = client.post(
            f'{base_url}/api/v1/automations',
            json=payload,
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_empty_name(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        payload = make_automation_payload(name='')
        resp = client.post(
            f'{base_url}/api/v1/automations',
            json=payload,
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_invalid_tarball_path_scheme(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        payload = make_automation_payload()
        payload['tarball_path'] = '/local/path/code.tar.gz'
        resp = client.post(
            f'{base_url}/api/v1/automations',
            json=payload,
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_shell_metachar_in_entrypoint(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        payload = make_automation_payload()
        payload['entrypoint'] = 'python main.py; rm -rf /'
        resp = client.post(
            f'{base_url}/api/v1/automations',
            json=payload,
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_absolute_entrypoint_rejected(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        payload = make_automation_payload()
        payload['entrypoint'] = '/usr/bin/python main.py'
        resp = client.post(
            f'{base_url}/api/v1/automations',
            json=payload,
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_path_traversal_in_setup_script(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        payload = make_automation_payload()
        payload['setup_script_path'] = '../../etc/passwd'
        resp = client.post(
            f'{base_url}/api/v1/automations',
            json=payload,
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_missing_required_fields(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        resp = client.post(
            f'{base_url}/api/v1/automations',
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 422


# ======================================================================
# CRUD lifecycle (sequential – order matters)
# ======================================================================


class TestCRUDLifecycle:
    """Full create → read → update → delete lifecycle in order.

    Uses a class-scoped automation so tests share the same resource.
    Pytest runs tests within a class in definition order by default.
    """

    @pytest.fixture(autouse=True, scope='class')
    def _shared_state(self):
        """Mutable dict shared across all tests in this class."""
        self.__class__._state = {}
        yield

    # -- Create ----------------------------------------------------------

    def test_create_automation(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        payload = {
            'name': 'QA Lifecycle Test',
            'trigger': {
                'type': 'cron',
                'schedule': '*/2 * * * *',
                'timezone': 'UTC',
            },
            'tarball_path': 's3://bucket/path/to/code.tar.gz',
            'entrypoint': 'uv run main.py',
        }
        resp = client.post(
            f'{base_url}/api/v1/automations',
            json=payload,
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text

        data = resp.json()
        assert data['name'] == 'QA Lifecycle Test'
        assert data['triggers']['schedule'] == '*/2 * * * *'
        assert data['tarball_path'] == 's3://bucket/path/to/code.tar.gz'
        assert data['entrypoint'] == 'uv run main.py'
        assert data['enabled'] is True
        assert data['setup_script_path'] is None
        assert data['last_triggered_at'] is None
        assert 'id' in data
        assert 'user_id' in data
        assert 'created_at' in data
        assert 'updated_at' in data

        # Stash for subsequent tests
        self.__class__._state['automation_id'] = data['id']
        self.__class__._state['user_id'] = data['user_id']

    # -- Read single -----------------------------------------------------

    def test_get_automation_by_id(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        aid = self.__class__._state['automation_id']
        resp = client.get(
            f'{base_url}/api/v1/automations/{aid}',
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data['id'] == aid
        assert data['name'] == 'QA Lifecycle Test'

    # -- List ------------------------------------------------------------

    def test_list_automations_contains_created(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        aid = self.__class__._state['automation_id']
        resp = client.get(
            f'{base_url}/api/v1/automations',
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert 'automations' in body
        assert 'total' in body
        assert body['total'] >= 1
        ids = [a['id'] for a in body['automations']]
        assert aid in ids

    # -- Update ----------------------------------------------------------

    def test_update_automation_name(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        aid = self.__class__._state['automation_id']
        resp = client.patch(
            f'{base_url}/api/v1/automations/{aid}',
            json={'name': 'QA Lifecycle Test – Renamed'},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data['name'] == 'QA Lifecycle Test – Renamed'
        # Other fields unchanged
        assert data['entrypoint'] == 'uv run main.py'

    def test_update_automation_schedule(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        aid = self.__class__._state['automation_id']
        resp = client.patch(
            f'{base_url}/api/v1/automations/{aid}',
            json={
                'trigger': {
                    'type': 'cron',
                    'schedule': '0 9 * * 1-5',
                    'timezone': 'America/New_York',
                }
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data['triggers']['schedule'] == '0 9 * * 1-5'
        assert data['triggers']['timezone'] == 'America/New_York'

    def test_update_automation_disable(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        aid = self.__class__._state['automation_id']
        resp = client.patch(
            f'{base_url}/api/v1/automations/{aid}',
            json={'enabled': False},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['enabled'] is False

    def test_update_automation_reenable(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        aid = self.__class__._state['automation_id']
        resp = client.patch(
            f'{base_url}/api/v1/automations/{aid}',
            json={'enabled': True},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()['enabled'] is True

    # -- Delete ----------------------------------------------------------

    def test_delete_automation(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        aid = self.__class__._state['automation_id']
        resp = client.delete(
            f'{base_url}/api/v1/automations/{aid}',
            headers=auth_headers,
        )
        assert resp.status_code == 204

    def test_get_deleted_automation_returns_404(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        aid = self.__class__._state['automation_id']
        resp = client.get(
            f'{base_url}/api/v1/automations/{aid}',
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_list_excludes_deleted_automation(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        aid = self.__class__._state['automation_id']
        resp = client.get(
            f'{base_url}/api/v1/automations',
            headers=auth_headers,
        )
        assert resp.status_code == 200
        ids = [a['id'] for a in resp.json()['automations']]
        assert aid not in ids


# ======================================================================
# Edge cases (independent – safe to parallelise)
# ======================================================================


class TestEdgeCases:
    def test_get_nonexistent_automation_returns_404(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        fake_id = str(uuid.uuid4())
        resp = client.get(
            f'{base_url}/api/v1/automations/{fake_id}',
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_delete_nonexistent_automation_returns_404(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        fake_id = str(uuid.uuid4())
        resp = client.delete(
            f'{base_url}/api/v1/automations/{fake_id}',
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_update_nonexistent_automation_returns_404(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        fake_id = str(uuid.uuid4())
        resp = client.patch(
            f'{base_url}/api/v1/automations/{fake_id}',
            json={'name': 'ghost'},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_invalid_uuid_returns_422(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        resp = client.get(
            f'{base_url}/api/v1/automations/not-a-uuid',
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_create_with_optional_setup_script(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
        create_automation,
    ):
        payload = make_automation_payload(setup_script_path='setup.sh')
        data = create_automation(payload)
        assert data['setup_script_path'] == 'setup.sh'

    def test_create_with_s3_tarball(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
        create_automation,
    ):
        payload = make_automation_payload(
            tarball_path='s3://my-bucket/automations/v1.tar.gz'
        )
        data = create_automation(payload)
        assert data['tarball_path'] == 's3://my-bucket/automations/v1.tar.gz'

    def test_create_with_gs_tarball(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
        create_automation,
    ):
        payload = make_automation_payload(
            tarball_path='gs://my-bucket/automations/v1.tar.gz'
        )
        data = create_automation(payload)
        assert data['tarball_path'] == 'gs://my-bucket/automations/v1.tar.gz'


# ======================================================================
# Pagination (independent)
# ======================================================================


class TestPagination:
    def test_list_with_limit_and_offset(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
        create_automation,
    ):
        # Create a few automations to test pagination
        for _ in range(3):
            create_automation()

        resp = client.get(
            f'{base_url}/api/v1/automations',
            params={'limit': 2, 'offset': 0},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body['automations']) <= 2
        assert body['total'] >= 3

    def test_list_with_large_offset_returns_empty(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        resp = client.get(
            f'{base_url}/api/v1/automations',
            params={'limit': 10, 'offset': 99999},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body['automations'] == []


# ======================================================================
# Manual Dispatch (independent – safe to parallelise)
# ======================================================================


class TestManualDispatch:
    """Tests for manually dispatching automation runs."""

    def test_dispatch_automation_creates_pending_run(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
        create_automation,
    ):
        """Dispatching an automation creates a PENDING run."""
        auto = create_automation()
        resp = client.post(
            f'{base_url}/api/v1/automations/{auto["id"]}/dispatch',
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data['automation_id'] == auto['id']
        assert data['status'] == 'PENDING'
        assert data['error_detail'] is None
        assert 'id' in data
        assert 'created_at' in data
        assert data['started_at'] is None
        assert data['completed_at'] is None

    def test_dispatch_nonexistent_automation_returns_404(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        """Dispatching a nonexistent automation returns 404."""
        fake_id = str(uuid.uuid4())
        resp = client.post(
            f'{base_url}/api/v1/automations/{fake_id}/dispatch',
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_dispatch_invalid_uuid_returns_422(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        """Dispatching with an invalid UUID returns 422."""
        resp = client.post(
            f'{base_url}/api/v1/automations/not-a-uuid/dispatch',
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_dispatch_requires_authentication(
        self,
        client: httpx.Client,
        base_url: str,
    ):
        """Dispatching without authentication returns 401."""
        fake_id = str(uuid.uuid4())
        resp = client.post(
            f'{base_url}/api/v1/automations/{fake_id}/dispatch',
        )
        assert resp.status_code == 401

    def test_dispatch_deleted_automation_returns_404(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        """Dispatching a deleted automation returns 404."""
        # Create and immediately delete an automation
        payload = make_automation_payload()
        create_resp = client.post(
            f'{base_url}/api/v1/automations',
            json=payload,
            headers=auth_headers,
        )
        assert (
            create_resp.status_code == 201
        ), f'Failed to create automation: {create_resp.status_code} - {create_resp.text}'
        auto_id = create_resp.json()['id']

        delete_resp = client.delete(
            f'{base_url}/api/v1/automations/{auto_id}',
            headers=auth_headers,
        )
        assert delete_resp.status_code == 204

        # Attempt to dispatch the deleted automation
        resp = client.post(
            f'{base_url}/api/v1/automations/{auto_id}/dispatch',
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_dispatch_multiple_runs_for_same_automation(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
        create_automation,
    ):
        """Multiple dispatches create multiple independent runs."""
        auto = create_automation()

        # Dispatch twice
        resp1 = client.post(
            f'{base_url}/api/v1/automations/{auto["id"]}/dispatch',
            headers=auth_headers,
        )
        resp2 = client.post(
            f'{base_url}/api/v1/automations/{auto["id"]}/dispatch',
            headers=auth_headers,
        )

        assert resp1.status_code == 201
        assert resp2.status_code == 201

        run1 = resp1.json()
        run2 = resp2.json()

        # Each dispatch creates a unique run
        assert run1['id'] != run2['id']
        assert run1['automation_id'] == run2['automation_id'] == auto['id']
        assert run1['status'] == run2['status'] == 'PENDING'

    @pytest.mark.timeout(30)
    def test_dispatch_run_transitions_to_running(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
        create_automation,
    ):
        """After dispatch, the run transitions to RUNNING status."""
        import time

        auto = create_automation()
        dispatch_resp = client.post(
            f'{base_url}/api/v1/automations/{auto["id"]}/dispatch',
            headers=auth_headers,
        )
        assert dispatch_resp.status_code == 201
        run_id = dispatch_resp.json()['id']

        # Wait for the dispatcher to pick up and process the run
        time.sleep(10)

        # Fetch the runs and verify status changed to RUNNING
        runs_resp = client.get(
            f'{base_url}/api/v1/automations/{auto["id"]}/runs',
            headers=auth_headers,
        )
        assert runs_resp.status_code == 200
        runs = runs_resp.json()['runs']
        run = next((r for r in runs if r['id'] == run_id), None)
        assert run is not None
        assert run['status'] == 'RUNNING'
        assert run['started_at'] is not None


# ======================================================================
# Automation Runs Listing (independent – safe to parallelise)
# ======================================================================


class TestListAutomationRuns:
    """Tests for listing automation runs."""

    def test_list_runs_empty(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
        create_automation,
    ):
        """Listing runs for an automation with no runs returns empty list."""
        auto = create_automation()
        resp = client.get(
            f'{base_url}/api/v1/automations/{auto["id"]}/runs',
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data['runs'] == []
        assert data['total'] == 0

    def test_list_runs_after_dispatch(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
        create_automation,
    ):
        """Listing runs after dispatch shows the created run."""
        auto = create_automation()

        # Dispatch a run
        dispatch_resp = client.post(
            f'{base_url}/api/v1/automations/{auto["id"]}/dispatch',
            headers=auth_headers,
        )
        assert dispatch_resp.status_code == 201
        run_id = dispatch_resp.json()['id']

        # List runs
        resp = client.get(
            f'{base_url}/api/v1/automations/{auto["id"]}/runs',
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 1
        assert len(data['runs']) == 1
        assert data['runs'][0]['id'] == run_id

    def test_list_runs_ordered_by_latest(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
        create_automation,
    ):
        """Runs are returned in descending order by creation time (latest first)."""
        import time

        auto = create_automation()

        # Dispatch multiple runs with small delays to ensure ordering
        run_ids = []
        for _ in range(3):
            resp = client.post(
                f'{base_url}/api/v1/automations/{auto["id"]}/dispatch',
                headers=auth_headers,
            )
            assert resp.status_code == 201
            run_ids.append(resp.json()['id'])
            time.sleep(0.1)  # Small delay to ensure different timestamps

        # List runs
        resp = client.get(
            f'{base_url}/api/v1/automations/{auto["id"]}/runs',
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 3

        # Verify order: latest first (reverse of creation order)
        returned_ids = [r['id'] for r in data['runs']]
        assert returned_ids == list(reversed(run_ids))

    def test_list_runs_pagination(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
        create_automation,
    ):
        """Pagination works correctly for listing runs."""
        auto = create_automation()

        # Dispatch 5 runs
        for _ in range(5):
            resp = client.post(
                f'{base_url}/api/v1/automations/{auto["id"]}/dispatch',
                headers=auth_headers,
            )
            assert resp.status_code == 201

        # Get first page (limit 2)
        resp = client.get(
            f'{base_url}/api/v1/automations/{auto["id"]}/runs',
            params={'limit': 2, 'offset': 0},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 5
        assert len(data['runs']) == 2

        # Get second page
        resp = client.get(
            f'{base_url}/api/v1/automations/{auto["id"]}/runs',
            params={'limit': 2, 'offset': 2},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 5
        assert len(data['runs']) == 2

        # Get last page (partial)
        resp = client.get(
            f'{base_url}/api/v1/automations/{auto["id"]}/runs',
            params={'limit': 2, 'offset': 4},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 5
        assert len(data['runs']) == 1

    def test_list_runs_nonexistent_automation_returns_404(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        """Listing runs for a nonexistent automation returns 404."""
        fake_id = str(uuid.uuid4())
        resp = client.get(
            f'{base_url}/api/v1/automations/{fake_id}/runs',
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_list_runs_requires_authentication(
        self,
        client: httpx.Client,
        base_url: str,
    ):
        """Listing runs without authentication returns 401."""
        fake_id = str(uuid.uuid4())
        resp = client.get(
            f'{base_url}/api/v1/automations/{fake_id}/runs',
        )
        assert resp.status_code == 401

    def test_list_runs_invalid_uuid_returns_422(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
    ):
        """Listing runs with an invalid UUID returns 422."""
        resp = client.get(
            f'{base_url}/api/v1/automations/not-a-uuid/runs',
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_list_runs_limit_exceeds_max_returns_422(
        self,
        client: httpx.Client,
        base_url: str,
        auth_headers: dict[str, str],
        create_automation,
    ):
        """Requesting more than 100 results returns 422."""
        auto = create_automation()
        resp = client.get(
            f'{base_url}/api/v1/automations/{auto["id"]}/runs',
            params={'limit': 101},
            headers=auth_headers,
        )
        assert resp.status_code == 422
