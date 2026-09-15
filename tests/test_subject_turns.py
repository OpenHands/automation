"""Programmatic subject work stays scoped, idempotent, and service-owned."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from openhands.automation.conversations import submit_subject_turn
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
    AutomationSubjectTurn,
)
from openhands.automation.subjects import conversation_id_for
from openhands.automation.utils import utcnow
from openhands.automation.utils.run_token import (
    SUBMIT_SUBJECT_TURN,
    create_run_token,
)


async def _requester(session, user, *, profile: bool = True) -> AutomationRun:
    automation = Automation(
        user_id=user.user_id,
        org_id=user.org_id,
        name="GitHub selector",
        trigger={"type": "cron", "schedule": "*/5 * * * *", "timezone": "UTC"},
        tarball_path="https://example.com/selector.tar.gz",
        entrypoint="python3 worker.py",
        agent_profile_id=uuid.uuid4() if profile else None,
    )
    session.add(automation)
    await session.flush()
    run = AutomationRun(
        automation=automation,
        agent_profile_id=automation.agent_profile_id,
        status=AutomationRunStatus.RUNNING,
    )
    session.add(run)
    await session.commit()
    return run


def _turn_request(requester: AutomationRun) -> dict:
    return {
        "requester": requester,
        "source": "github",
        "subject_key": "repository-42/issue-7",
        "turn": "Implement issue 7",
        "idempotency_key": "issue-7-ready-v1",
        "wake_agent": True,
    }


@pytest.mark.asyncio
async def test_first_turn_creates_profile_backed_subject_run(
    async_session, mock_authenticated_user
):
    requester = await _requester(async_session, mock_authenticated_user)

    result = await submit_subject_turn(
        async_session,
        requester=requester,
        source="github",
        subject_key="repository-42/issue-7",
        turn="Implement issue 7",
        idempotency_key="issue-7-ready-v1",
        wake_agent=True,
    )
    await async_session.commit()

    child = await async_session.get(AutomationRun, result.run_id)
    assert result.disposition == "created"
    assert child is not None
    assert child.agent_profile_id == requester.agent_profile_id
    assert child.execution_scope == "conversation"
    assert child.conversation_turn == "Implement issue 7"
    expected_conversation_id = conversation_id_for(
        requester.automation.org_id,
        requester.automation_id,
        "github",
        "repository-42/issue-7",
    )
    assert result.conversation_id == expected_conversation_id
    assert child.conversation_id is None


@pytest.mark.asyncio
async def test_same_idempotency_key_returns_the_original_run(
    async_session, mock_authenticated_user
):
    requester = await _requester(async_session, mock_authenticated_user)
    request = {
        "requester": requester,
        "source": "github",
        "subject_key": "repository-42/pr-8",
        "turn": "Review PR 8",
        "idempotency_key": "head-deadbeef",
        "wake_agent": True,
    }
    first = await submit_subject_turn(async_session, **request)
    await async_session.commit()
    second = await submit_subject_turn(async_session, **request)
    await async_session.commit()

    count = await async_session.scalar(select(func.count(AutomationSubjectTurn.id)))
    assert second.disposition == "deduplicated"
    assert second.run_id == first.run_id
    assert count == 1


@pytest.mark.asyncio
async def test_new_turn_is_queued_on_the_subject_run_before_dispatch(
    async_session, mock_authenticated_user
):
    requester = await _requester(async_session, mock_authenticated_user)
    first = await submit_subject_turn(async_session, **_turn_request(requester))
    await async_session.commit()

    second = await submit_subject_turn(
        async_session,
        **{
            **_turn_request(requester),
            "turn": "Acceptance criteria changed",
            "idempotency_key": "issue-7-ready-v2",
        },
    )
    await async_session.commit()

    assert second.disposition == "queued"
    assert second.run_id == first.run_id
    run = await async_session.get(AutomationRun, first.run_id)
    assert run is not None
    assert run.event_payload == {
        "_automation_follow_up_turns": ["Acceptance criteria changed"]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_status",
    [
        AutomationRunStatus.FAILED,
        AutomationRunStatus.CANCELLED,
        AutomationRunStatus.SKIPPED,
    ],
)
async def test_same_idempotency_key_retries_an_unsuccessful_released_run(
    async_session, mock_authenticated_user, failed_status
):
    requester = await _requester(async_session, mock_authenticated_user)
    request = _turn_request(requester)
    first = await submit_subject_turn(async_session, **request)
    await async_session.commit()
    failed = await async_session.get(AutomationRun, first.run_id)
    assert failed is not None
    failed.status = failed_status
    failed.subject_released_at = utcnow()
    await async_session.commit()

    second = await submit_subject_turn(async_session, **request)
    await async_session.commit()

    record = (await async_session.execute(select(AutomationSubjectTurn))).scalar_one()
    assert second.disposition == "created"
    assert second.run_id != first.run_id
    assert second.conversation_id == first.conversation_id
    assert record.subject_run_id == second.run_id


@pytest.mark.asyncio
async def test_same_idempotency_key_retries_a_skipped_run_that_never_started(
    async_session, mock_authenticated_user
):
    requester = await _requester(async_session, mock_authenticated_user)
    request = _turn_request(requester)
    first = await submit_subject_turn(async_session, **request)
    await async_session.commit()
    skipped = await async_session.get(AutomationRun, first.run_id)
    assert skipped is not None
    skipped.status = AutomationRunStatus.SKIPPED
    skipped.started_at = None
    await async_session.commit()

    second = await submit_subject_turn(async_session, **request)
    await async_session.commit()

    assert second.disposition == "created"
    assert second.run_id != first.run_id
    assert second.conversation_id == first.conversation_id
    assert skipped.subject_released_at is not None


@pytest.mark.asyncio
async def test_same_idempotency_key_waits_for_failed_run_release(
    async_session, mock_authenticated_user
):
    requester = await _requester(async_session, mock_authenticated_user)
    request = _turn_request(requester)
    first = await submit_subject_turn(async_session, **request)
    await async_session.commit()
    failed = await async_session.get(AutomationRun, first.run_id)
    assert failed is not None
    failed.status = AutomationRunStatus.FAILED
    failed.started_at = utcnow()
    await async_session.commit()

    second = await submit_subject_turn(async_session, **request)
    await async_session.commit()

    assert second.disposition == "deduplicated"
    assert second.run_id == first.run_id


@pytest.mark.asyncio
async def test_source_is_part_of_subject_identity(
    async_session, mock_authenticated_user
):
    requester = await _requester(async_session, mock_authenticated_user)
    first = await submit_subject_turn(
        async_session,
        requester=requester,
        source="github",
        subject_key="42",
        turn="GitHub work",
        idempotency_key="one",
        wake_agent=True,
    )
    await async_session.commit()
    second = await submit_subject_turn(
        async_session,
        requester=requester,
        source="linear",
        subject_key="42",
        turn="Linear work",
        idempotency_key="one",
        wake_agent=True,
    )
    await async_session.commit()

    assert first.run_id != second.run_id
    assert first.conversation_id != second.conversation_id


@pytest.mark.asyncio
async def test_endpoint_rejects_a_token_for_another_run(
    async_client, async_session, mock_authenticated_user
):
    requester = await _requester(async_session, mock_authenticated_user)
    token = create_run_token(
        secret="test-secret",
        automation_id=requester.automation_id,
        run_id=uuid.uuid4(),
        scopes=(SUBMIT_SUBJECT_TURN,),
    )
    with patch(
        "openhands.automation.subject_router.signing_secret",
        return_value="test-secret",
    ):
        response = await async_client.post(
            f"/api/automation/v1/runs/{requester.id}/subject-turns",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "source": "github",
                "subject_key": "repo/issue-1",
                "turn": "Implement it",
                "idempotency_key": "ready-v1",
            },
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_endpoint_accepts_only_its_running_automation(
    async_client, async_session, mock_authenticated_user
):
    requester = await _requester(async_session, mock_authenticated_user)
    token = create_run_token(
        secret="test-secret",
        automation_id=requester.automation_id,
        run_id=requester.id,
        scopes=(SUBMIT_SUBJECT_TURN,),
    )
    with patch(
        "openhands.automation.subject_router.signing_secret",
        return_value="test-secret",
    ):
        response = await async_client.post(
            f"/api/automation/v1/runs/{requester.id}/subject-turns",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "source": "github",
                "subject_key": "repo/issue-1",
                "turn": "Implement it",
                "idempotency_key": "ready-v1",
            },
        )

    assert response.status_code == 202
    assert response.json()["disposition"] == "created"


@pytest.mark.asyncio
async def test_completed_subject_turn_gets_a_new_tracked_run(
    async_session, mock_authenticated_user, monkeypatch
):
    requester = await _requester(async_session, mock_authenticated_user)
    first = await submit_subject_turn(
        async_session,
        requester=requester,
        source="github",
        subject_key="repository-42/pr-8",
        turn="Review head one",
        idempotency_key="head-one",
        wake_agent=True,
    )
    await async_session.commit()
    child = await async_session.get(AutomationRun, first.run_id)
    assert child is not None
    child.status = AutomationRunStatus.COMPLETED
    child.started_at = requester.created_at
    await async_session.commit()
    delivered = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "openhands.automation.conversations.send_conversation_turn", delivered
    )

    second = await submit_subject_turn(
        async_session,
        requester=requester,
        source="github",
        subject_key="repository-42/pr-8",
        turn="Review head two",
        idempotency_key="head-two",
        wake_agent=True,
    )
    await async_session.commit()

    assert second.disposition == "created"
    assert second.run_id != first.run_id
    assert second.conversation_id == first.conversation_id
    delivered.assert_not_awaited()
    previous = await async_session.get(AutomationRun, first.run_id)
    replacement = await async_session.get(AutomationRun, second.run_id)
    assert previous is not None
    assert previous.subject_released_at is not None
    assert replacement is not None
    assert replacement.status == AutomationRunStatus.PENDING
    assert replacement.conversation_turn == "Review head two"
