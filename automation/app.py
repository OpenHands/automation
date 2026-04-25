"""FastAPI application entrypoint."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from automation.auth import create_http_client
from automation.config import get_settings
from automation.db import create_engine, create_session_factory
from automation.dispatcher import dispatcher_loop
from automation.event_router import router as event_router
from automation.kv_router import router as kv_router
from automation.logger import setup_all_loggers
from automation.preset_router import router as preset_router
from automation.router import router
from automation.scheduler import scheduler_loop
from automation.uploads import router as uploads_router
from automation.watchdog import watchdog_loop
from automation.webhook_router import router as webhook_router


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
    engine_result = await create_engine(settings)
    app.state.engine_result = engine_result
    app.state.engine = engine_result.engine
    app.state.session_factory = create_session_factory(engine_result.engine)

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
    await app.state.engine_result.dispose()
    logger.info("Automations service shut down")


def _build_cors_origins() -> list[str]:
    """Build the list of allowed CORS origins from settings."""
    settings = get_settings()
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if not origins:
        origins = [settings.openhands_api_base_url]
    return origins


def _create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    # Serve OpenAPI docs under the base path so they're accessible when the app
    # is mounted at /api/automation (e.g., /api/automation/docs).
    base_path = get_settings().base_path
    return FastAPI(
        title="OpenHands Automations Service",
        description=(
            "Scheduled and event-driven automation execution for OpenHands Cloud"
        ),
        version="0.1.0",
        lifespan=lifespan,
        docs_url=f"{base_path}/docs",
        openapi_url=f"{base_path}/openapi.json",
        redoc_url=f"{base_path}/redoc",
    )


app = _create_app()

app.add_middleware(
    CORSMiddleware,
    allow_origins=_build_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_base_path = get_settings().base_path

# Include specific routers BEFORE main router to avoid route conflict.
# The main router has /v1/{automation_id} which would match any /v1/<path>
# and fail UUID validation.
app.include_router(uploads_router, prefix=_base_path)
app.include_router(preset_router, prefix=_base_path)
app.include_router(event_router, prefix=_base_path)
app.include_router(webhook_router, prefix=_base_path)
app.include_router(kv_router, prefix=_base_path)
app.include_router(router, prefix=_base_path)


# Static /health and /ready paths are a convenience for k8s probes — the fixed
# path requires less templating.  Base-path endpoints are still available for
# publicly-routed traffic like integration tests.
@app.get("/health")
@app.get(f"{_base_path}/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
@app.get(f"{_base_path}/ready")
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


# ---------------------------------------------------------------------------
# Frontend static file hosting (opt-in via AUTOMATION_FRONTEND_DIR)
# ---------------------------------------------------------------------------
_settings = get_settings()
_frontend_dir = _settings.frontend_dir
if _frontend_dir:
    _frontend_path = Path(_frontend_dir)
    if not _frontend_path.is_dir():
        logger.warning(
            "AUTOMATION_FRONTEND_DIR=%s is not a directory — frontend hosting disabled",
            _frontend_dir,
        )
    else:
        _frontend_mount = _settings.frontend_path
        logger.info("Serving frontend from %s at %s", _frontend_dir, _frontend_mount)

        _index_full_path = str(_frontend_path / "index.html")
        _index_stat = os.stat(_index_full_path)

        class _SPAStaticFiles(StaticFiles):
            """StaticFiles that falls back to index.html for SPA client routes."""

            def lookup_path(self, path: str) -> tuple[str, os.stat_result | None]:
                full_path, stat_result = super().lookup_path(path)
                if stat_result is None:
                    # Unknown path → serve index.html for client-side routing
                    return _index_full_path, _index_stat
                return full_path, stat_result

            def file_response(self, full_path, stat_result, scope, status_code=200):
                response = super().file_response(
                    full_path, stat_result, scope, status_code
                )
                # Hashed assets are immutable; everything else (especially
                # index.html) must be revalidated on every request.
                if "/assets/" in str(full_path):
                    response.headers["Cache-Control"] = (
                        "public, max-age=31536000, immutable"
                    )
                else:
                    response.headers.setdefault(
                        "Cache-Control", "no-cache, must-revalidate"
                    )
                return response

        app.mount(
            _frontend_mount,
            _SPAStaticFiles(directory=_frontend_path, html=True),
            name="frontend",
        )
