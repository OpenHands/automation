"""FastAPI application entrypoint."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from automation.auth import create_http_client
from automation.config import get_settings
from automation.db import create_engine, create_session_factory
from automation.dispatcher import dispatcher_loop
from automation.logger import setup_all_loggers
from automation.router import router
from automation.scheduler import scheduler_loop
from automation.uploads import router as uploads_router
from automation.watchdog import watchdog_loop


logger = logging.getLogger("automation.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown lifecycle."""
    # Startup
    settings = get_settings()

    # Apply the repo-wide JSON structured-logging convention
    setup_all_loggers()

    # Silence noisy third-party loggers
    for noisy_logger in (
        "ddtrace",
        "httpx",
        "httpcore",
        "sqlalchemy.engine",  # Suppress SQL statement logging
    ):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    logger.info("Starting OpenHands Automations Service")

    # Create shared httpx client for auth (stored in app.state for DI)
    app.state.http_client = create_http_client()

    # Create engine and session factory, store in app.state
    engine = await create_engine(settings)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    # Start the background scheduler and dispatcher
    shutdown_event = asyncio.Event()
    app.state.shutdown_event = shutdown_event

    # Scheduler: polls automations and creates PENDING runs
    scheduler_task = asyncio.create_task(
        scheduler_loop(
            app.state.session_factory,
            interval_seconds=settings.scheduler_interval_seconds,
            shutdown_event=shutdown_event,
        )
    )
    app.state.scheduler_task = scheduler_task
    logger.info("Background scheduler started")

    # Dispatcher: picks up PENDING runs and dispatches them
    if not settings.base_url:
        logger.warning(
            "AUTOMATION_BASE_URL not set — using localhost. "
            "Sandboxes in the cloud won't be able to reach this URL."
        )
    dispatcher_task = asyncio.create_task(
        dispatcher_loop(
            app.state.session_factory,
            settings=settings,
            interval_seconds=settings.dispatcher_interval_seconds,
            shutdown_event=shutdown_event,
        )
    )
    app.state.dispatcher_task = dispatcher_task
    logger.info("Background dispatcher started")

    # Watchdog: marks stale RUNNING runs as FAILED
    watchdog_task = asyncio.create_task(
        watchdog_loop(
            app.state.session_factory,
            settings=settings,
            shutdown_event=shutdown_event,
        )
    )
    app.state.watchdog_task = watchdog_task
    logger.info("Background watchdog started")

    yield

    # Shutdown
    logger.info("Shutting down background tasks...")
    shutdown_event.set()

    # Wait for all tasks to exit gracefully
    for task_name, task in [
        ("scheduler", scheduler_task),
        ("dispatcher", dispatcher_task),
        ("watchdog", watchdog_task),
    ]:
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except TimeoutError:
            logger.warning("%s did not exit in time, cancelling", task_name)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    await app.state.http_client.aclose()
    await app.state.engine.dispose()
    logger.info("Automations service shut down")


def _build_cors_origins() -> list[str]:
    """Build the list of allowed CORS origins from settings."""
    settings = get_settings()
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if not origins:
        origins = [settings.openhands_api_base_url]
    return origins


app = FastAPI(
    title="OpenHands Automations Service",
    description="Scheduled and event-driven automation execution for OpenHands Cloud",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_build_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include uploads_router BEFORE router to avoid route conflict.
# The main router has /v1/{automation_id} which would match /v1/uploads
# and fail UUID validation if included first.
app.include_router(uploads_router)
app.include_router(router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def readiness():
    """Readiness probe — checks DB connectivity.

    Returns 503 when the DB is unreachable so Kubernetes stops routing traffic.
    """
    try:
        async with app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception as e:
        logger.error("Readiness check failed: %s", e, exc_info=True)
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "error": "database unavailable"},
        )
