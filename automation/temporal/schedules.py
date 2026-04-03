"""Temporal Schedule management for cron automations.

Temporal Schedules are first-class citizens that replace the need for
custom cron polling loops. This module provides functions to create,
update, and delete schedules for automations.

Each automation with a cron trigger gets a corresponding Temporal Schedule
that starts AutomationWorkflow executions at the specified times.
"""

import logging
import uuid
from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
)

from automation.config import get_settings
from automation.models import Automation
from automation.temporal.types import (
    AutomationConfig,
    TriggerContext,
    WorkflowInput,
)
from automation.temporal.workflows import AutomationWorkflow


logger = logging.getLogger(__name__)


def _make_schedule_id(automation_id: uuid.UUID) -> str:
    """Generate a Temporal schedule ID for an automation."""
    return f"automation-{automation_id}"


def _make_workflow_id(automation_id: uuid.UUID) -> str:
    """Generate a workflow ID prefix for an automation's runs."""
    return f"automation-run-{automation_id}"


def _automation_to_config(automation: Automation) -> AutomationConfig:
    """Convert an Automation model to AutomationConfig dataclass."""
    from automation.constants import MAX_RUN_DURATION_SECONDS

    return AutomationConfig(
        automation_id=str(automation.id),
        user_id=str(automation.user_id),
        org_id=str(automation.org_id),
        name=automation.name,
        tarball_path=automation.tarball_path,
        entrypoint=automation.entrypoint,
        timeout_seconds=automation.timeout or MAX_RUN_DURATION_SECONDS,
        trigger=automation.trigger,
        setup_script_path=automation.setup_script_path,
    )


async def create_schedule(
    client: Client,
    automation: Automation,
) -> str:
    """Create a Temporal Schedule for an automation.

    Args:
        client: Temporal client.
        automation: The automation to schedule.

    Returns:
        The schedule ID.

    Raises:
        ValueError: If the automation doesn't have a cron trigger.
    """
    trigger = automation.trigger
    if trigger.get("type") != "cron":
        raise ValueError(f"Unsupported trigger type: {trigger.get('type')}")

    cron_expression = trigger.get("schedule")
    if not cron_expression:
        raise ValueError("Cron trigger missing 'schedule' field")

    timezone = trigger.get("timezone", "UTC")
    schedule_id = _make_schedule_id(automation.id)
    settings = get_settings()

    # Build workflow input
    automation_config = _automation_to_config(automation)
    trigger_context = TriggerContext(
        trigger_type="cron",
        # scheduled_time will be filled by Temporal at runtime
    )

    # Note: run_id will be generated per execution using workflow ID
    # The actual WorkflowInput is built at schedule execution time
    # For now, we use a placeholder that will be replaced

    logger.info(
        "Creating schedule for automation: schedule_id=%s cron=%s timezone=%s",
        schedule_id,
        cron_expression,
        timezone,
    )

    # Create the schedule
    await client.create_schedule(
        schedule_id,
        Schedule(
            action=ScheduleActionStartWorkflow(
                AutomationWorkflow.run,
                args=[
                    WorkflowInput(
                        automation=automation_config,
                        trigger_context=trigger_context,
                        run_id="",  # Will be set by workflow ID
                        callback_url=f"{settings.resolved_base_url}/v1/runs/{{workflow_id}}/complete",
                    )
                ],
                id=_make_workflow_id(automation.id),
                task_queue=settings.temporal_task_queue,
            ),
            spec=ScheduleSpec(
                cron_expressions=[cron_expression],
                time_zone_name=timezone,
            ),
            policy=SchedulePolicy(
                overlap=ScheduleOverlapPolicy.SKIP,  # Skip if previous run still active
                catchup_window=timedelta(minutes=5),  # Catch up missed runs within 5 min
            ),
            state=ScheduleState(
                paused=not automation.enabled,
            ),
        ),
    )

    logger.info("Schedule created: %s", schedule_id)
    return schedule_id


async def update_schedule(
    client: Client,
    automation: Automation,
) -> None:
    """Update an existing Temporal Schedule.

    Updates the schedule's cron expression, timezone, and enabled state.

    Args:
        client: Temporal client.
        automation: The automation with updated configuration.
    """
    schedule_id = _make_schedule_id(automation.id)
    trigger = automation.trigger

    if trigger.get("type") != "cron":
        # If trigger type changed, delete the schedule
        await delete_schedule(client, automation.id)
        return

    cron_expression = trigger.get("schedule")
    timezone = trigger.get("timezone", "UTC")
    settings = get_settings()

    logger.info(
        "Updating schedule: schedule_id=%s cron=%s timezone=%s enabled=%s",
        schedule_id,
        cron_expression,
        timezone,
        automation.enabled,
    )

    handle = client.get_schedule_handle(schedule_id)

    # Update the schedule
    automation_config = _automation_to_config(automation)
    trigger_context = TriggerContext(trigger_type="cron")

    async def update_fn(input: Schedule) -> Schedule:
        input.action = ScheduleActionStartWorkflow(
            AutomationWorkflow.run,
            args=[
                WorkflowInput(
                    automation=automation_config,
                    trigger_context=trigger_context,
                    run_id="",
                    callback_url=f"{settings.resolved_base_url}/v1/runs/{{workflow_id}}/complete",
                )
            ],
            id=_make_workflow_id(automation.id),
            task_queue=settings.temporal_task_queue,
        )
        input.spec = ScheduleSpec(
            cron_expressions=[cron_expression] if cron_expression else [],
            time_zone_name=timezone,
        )
        input.state.paused = not automation.enabled
        return input

    await handle.update(update_fn)
    logger.info("Schedule updated: %s", schedule_id)


async def delete_schedule(
    client: Client,
    automation_id: uuid.UUID,
) -> bool:
    """Delete a Temporal Schedule.

    Args:
        client: Temporal client.
        automation_id: The automation ID.

    Returns:
        True if the schedule was deleted, False if it didn't exist.
    """
    schedule_id = _make_schedule_id(automation_id)

    logger.info("Deleting schedule: %s", schedule_id)

    try:
        handle = client.get_schedule_handle(schedule_id)
        await handle.delete()
        logger.info("Schedule deleted: %s", schedule_id)
        return True
    except Exception as e:
        # Schedule might not exist
        logger.warning("Failed to delete schedule %s: %s", schedule_id, e)
        return False


async def pause_schedule(
    client: Client,
    automation_id: uuid.UUID,
) -> None:
    """Pause a Temporal Schedule.

    Args:
        client: Temporal client.
        automation_id: The automation ID.
    """
    schedule_id = _make_schedule_id(automation_id)
    handle = client.get_schedule_handle(schedule_id)
    await handle.pause(note="Automation disabled")
    logger.info("Schedule paused: %s", schedule_id)


async def unpause_schedule(
    client: Client,
    automation_id: uuid.UUID,
) -> None:
    """Unpause a Temporal Schedule.

    Args:
        client: Temporal client.
        automation_id: The automation ID.
    """
    schedule_id = _make_schedule_id(automation_id)
    handle = client.get_schedule_handle(schedule_id)
    await handle.unpause(note="Automation enabled")
    logger.info("Schedule unpaused: %s", schedule_id)


async def trigger_schedule(
    client: Client,
    automation_id: uuid.UUID,
) -> None:
    """Manually trigger a schedule (run immediately).

    Args:
        client: Temporal client.
        automation_id: The automation ID.
    """
    schedule_id = _make_schedule_id(automation_id)
    handle = client.get_schedule_handle(schedule_id)
    await handle.trigger()
    logger.info("Schedule triggered manually: %s", schedule_id)


async def get_schedule_info(
    client: Client,
    automation_id: uuid.UUID,
) -> dict | None:
    """Get information about a schedule.

    Args:
        client: Temporal client.
        automation_id: The automation ID.

    Returns:
        Schedule info dict, or None if schedule doesn't exist.
    """
    schedule_id = _make_schedule_id(automation_id)

    try:
        handle = client.get_schedule_handle(schedule_id)
        desc = await handle.describe()
        return {
            "schedule_id": schedule_id,
            "paused": desc.schedule.state.paused,
            "num_actions": desc.info.num_actions,
            "last_action_time": desc.info.recent_actions[-1].scheduled_at
            if desc.info.recent_actions
            else None,
            "next_action_times": [
                t.isoformat() for t in (desc.info.next_action_times or [])[:3]
            ],
        }
    except Exception as e:
        logger.warning("Failed to get schedule info for %s: %s", schedule_id, e)
        return None
