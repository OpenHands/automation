"""FastAPI application with Temporal workflow execution.

This application uses Temporal for durable workflow execution:
- Temporal worker runs as a background task
- Temporal Schedules handle cron-based automation triggers
- Activities handle sandbox operations with automatic retries
- Workflows provide crash-proof execution guarantees
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from automation.config import get_settings
from automation.db import create_engine, create_session_factory
from automation.logger import setup_all_loggers
from automation.preset_router import router as preset_router
from automation.router import router
from automation.temporal.client import close_temporal_client, get_temporal_client
from automation.temporal.worker import create_worker
from automation.uploads import router as uploads_router


logger = logging.getLogger("automation.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown lifecycle with Temporal."""
    settings = get_settings()

    # Apply structured logging
    setup_all_loggers()

    # Silence noisy loggers
    for noisy_logger in (
        "ddtrace",
        "httpx",
        "httpcore",
        "sqlalchemy.engine",
    ):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    logger.info("Starting OpenHands Automations Service")

    # Create database engine and session factory
    engine_result = await create_engine(settings)
    app.state.engine_result = engine_result
    app.state.engine = engine_result.engine
    app.state.session_factory = create_session_factory(engine_result.engine)

    # Initialize Temporal client
    try:
        temporal_client = await get_temporal_client()
        app.state.temporal_client = temporal_client
        logger.info("Temporal client connected")
    except Exception as e:
        logger.error("Failed to connect to Temporal: %s", e)
        raise

    # Start Temporal worker as background task (unless skip_worker is set)
    # When running with separate worker pods, skip_worker should be True to avoid
    # conflicts between ddtrace instrumentation and Temporal's workflow sandbox
    shutdown_event = asyncio.Event()
    app.state.shutdown_event = shutdown_event
    worker_task = None

    if not settings.skip_worker:
        worker = await create_worker(temporal_client, settings)
        worker_task = asyncio.create_task(
            _run_worker_with_shutdown(worker, shutdown_event),
            name="temporal-worker",
        )
        app.state.worker_task = worker_task
        logger.info("Temporal worker started")
    else:
        logger.info("Skipping in-process worker (AUTOMATION_SKIP_WORKER=true)")

    yield

    # Shutdown
    logger.info("Shutting down...")
    shutdown_event.set()

    # Wait for worker to stop (if we started one)
    if worker_task is not None:
        try:
            await asyncio.wait_for(worker_task, timeout=10.0)
        except TimeoutError:
            logger.warning("Worker did not stop in time, cancelling")
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

    # Close Temporal client
    await close_temporal_client()

    # Close database
    await engine_result.dispose()
    logger.info("Automations service shut down")


async def _run_worker_with_shutdown(worker, shutdown_event: asyncio.Event):
    """Run worker until shutdown event is set."""
    async with worker:
        await shutdown_event.wait()
        logger.info("Worker received shutdown signal")


def _build_cors_origins() -> list[str]:
    """Build the list of allowed CORS origins."""
    settings = get_settings()
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if not origins:
        origins = [settings.openhands_api_base_url]
    return origins


def _create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()
    return FastAPI(
        title="OpenHands Automations Service",
        description="Scheduled and event-driven automation execution using Temporal",
        version="0.2.0",
        lifespan=lifespan,
        root_path=settings.root_path,
    )


app = _create_app()

app.add_middleware(
    CORSMiddleware,
    allow_origins=_build_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers (order matters - more specific routes first)
app.include_router(uploads_router)
app.include_router(preset_router)
app.include_router(router)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/ready")
async def readiness():
    """Readiness probe — checks DB and Temporal connectivity."""
    errors = []

    # Check database
    try:
        async with app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        logger.error("Database check failed: %s", e)
        errors.append("database unavailable")

    # Check Temporal
    try:
        client = app.state.temporal_client
        # Simple connectivity check - list workflows with limit 1
        async for _ in client.list_workflows(query="", page_size=1):
            break
    except Exception as e:
        logger.error("Temporal check failed: %s", e)
        errors.append("temporal unavailable")

    if errors:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "errors": errors},
        )

    return {"status": "ready"}
