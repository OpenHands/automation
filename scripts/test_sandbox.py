#!/usr/bin/env python3
"""Test that workflows pass Temporal sandbox validation.

Run this locally to verify workflows before deploying:
    uv run python scripts/test_sandbox.py

This validates that:
1. Workflow code doesn't import restricted modules (httpx, urllib.request, etc.)
2. All activity/workflow definitions are properly structured
3. The worker can be created without sandbox errors
"""
import asyncio
import sys


async def test_worker_sandbox():
    """Test that workflows can be validated by the Temporal sandbox."""
    from temporalio.worker import Worker
    from temporalio.testing import WorkflowEnvironment

    print("Starting test environment...")

    # Use the local environment - this creates an in-memory Temporal server
    env = await WorkflowEnvironment.start_local()

    try:
        # Import AFTER environment is ready
        from automation.temporal.workflows import ALL_WORKFLOWS
        from automation.temporal.activities import ALL_ACTIVITIES

        print(f"Testing {len(ALL_WORKFLOWS)} workflows and {len(ALL_ACTIVITIES)} activities...")

        # This is where sandbox validation happens
        # If it fails, we get RuntimeError: Failed validating workflow <name>
        worker = Worker(
            env.client,
            task_queue="test-queue",
            workflows=ALL_WORKFLOWS,
            activities=ALL_ACTIVITIES,
        )

        print("✅ Worker created successfully - workflows pass sandbox validation!")
        print(f"   Workflows: {[w.__name__ for w in ALL_WORKFLOWS]}")
        print(f"   Activities: {[a.__name__ for a in ALL_ACTIVITIES]}")
        return True

    except RuntimeError as e:
        if "Failed validating workflow" in str(e):
            print(f"❌ Sandbox validation FAILED: {e}")
            # Print the full traceback for debugging
            import traceback
            traceback.print_exc()
            return False
        raise
    finally:
        await env.shutdown()


if __name__ == "__main__":
    success = asyncio.run(test_worker_sandbox())
    sys.exit(0 if success else 1)
