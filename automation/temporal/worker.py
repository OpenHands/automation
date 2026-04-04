"""Temporal worker setup.

Workers are long-running processes that poll Temporal for tasks and execute
workflows and activities. This module provides functions to create and run
workers.

Workers can be run:
1. In-process with the FastAPI app (for development)
2. As separate processes/pods (for production scaling)
"""

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from automation.config import Settings, get_settings
from automation.temporal.activities import ALL_ACTIVITIES
from automation.temporal.client import create_temporal_client
from automation.temporal.workflows import ALL_WORKFLOWS


logger = logging.getLogger(__name__)


async def create_worker(
    client: Client | None = None,
    settings: Settings | None = None,
) -> Worker:
    """Create a Temporal worker.

    Args:
        client: Temporal client. If None, creates a new one.
        settings: Application settings. If None, uses get_settings().

    Returns:
        Configured Temporal worker (not yet running).
    """
    if settings is None:
        settings = get_settings()

    if client is None:
        client = await create_temporal_client(settings)

    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=ALL_WORKFLOWS,
        activities=ALL_ACTIVITIES,
    )

    logger.info(
        "Worker created for task queue '%s' with %d workflows and %d activities",
        settings.temporal_task_queue,
        len(ALL_WORKFLOWS),
        len(ALL_ACTIVITIES),
    )

    return worker


async def run_worker(
    client: Client | None = None,
    settings: Settings | None = None,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Run a Temporal worker until shutdown.

    This is the main entry point for running a worker. It creates a worker
    and runs it until the shutdown_event is set or the process is interrupted.

    Args:
        client: Temporal client. If None, creates a new one.
        settings: Application settings. If None, uses get_settings().
        shutdown_event: Event to signal graceful shutdown.
    """
    worker = await create_worker(client, settings)

    logger.info("Starting Temporal worker")

    if shutdown_event is None:
        # Run forever
        await worker.run()
    else:
        # Run until shutdown event
        async with worker:
            await shutdown_event.wait()
            logger.info("Worker received shutdown signal")


async def run_worker_standalone() -> None:
    """Run a standalone worker (for separate worker processes).

    This function is meant to be called from a __main__ block or CLI.
    It sets up signal handlers for graceful shutdown.
    """
    import signal

    shutdown_event = asyncio.Event()

    def signal_handler():
        logger.info("Received shutdown signal")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await run_worker(shutdown_event=shutdown_event)
    finally:
        logger.info("Worker stopped")


# Entry point for standalone worker
if __name__ == "__main__":
    from automation.logger import setup_all_loggers

    setup_all_loggers()
    asyncio.run(run_worker_standalone())
