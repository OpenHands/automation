"""FastAPI application entrypoint."""

import asyncio
import logging
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from openhands.automation.auth import create_http_client
from openhands.automation.capabilities_router import router as capabilities_router
from openhands.automation.config import get_config, get_settings
from openhands.automation.db import (
    create_engine,
    create_session_factory,
    set_sqlite_mode,
)
from openhands.automation.dispatcher import dispatcher_loop
from openhands.automation.draft_router import router as draft_router
from openhands.automation.event_router import router as event_router
from openhands.automation.git_sync import git_sync_loop, is_git_sync_supported
from openhands.automation.git_sync.router import router as git_sync_router
from openhands.automation.kv_router import router as kv_router
from openhands.automation.logger import setup_all_loggers
from openhands.automation.middleware import (
    ApiKeyAwareCORSMiddleware,
    TelemetryContextMiddleware,
    api_route_telemetry_middleware,
)
from openhands.automation.preset_router import router as preset_router
from openhands.automation.router import router
from openhands.automation.scheduler import scheduler_loop
from openhands.automation.streams import stream_supervisor_loop
from openhands.automation.telemetry_router import router as telemetry_router
from openhands.automation.uploads import router as uploads_router
from openhands.automation.utils.version import get_sdk_version, get_server_version_info
from openhands.automation.watchdog import watchdog_loop
from openhands.automation.webhook_router import router as webhook_router


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

    logger.info(
        "Starting OpenHands Automations Service",
        extra={"kv_store_configured": get_config().kv.enabled},
    )

    # Create shared httpx client for auth (stored in app.state for DI)
    app.state.http_client = create_http_client()

    # Create engine and session factory, store in app.state
    engine_result = await create_engine(settings)
    app.state.engine_result = engine_result
    app.state.engine = engine_result.engine
    app.state.session_factory = create_session_factory(engine_result.engine)

    # Set SQLite mode flag for scheduler/dispatcher to use
    set_sqlite_mode(engine_result.is_sqlite)

    # Auto-run migrations for SQLite on startup
    # This ensures the schema is always up-to-date for local deployments
    # For PostgreSQL, migrations are typically run separately via `alembic upgrade head`
    if engine_result.is_sqlite:
        from alembic import command
        from alembic.config import Config

        from openhands.automation.db import normalize_sqlite_url_for_alembic

        # Find migrations folder relative to this package.
        # When installed via pip/uvx, migrations are bundled inside
        # automation/migrations.
        package_dir = Path(__file__).parent
        migrations_path = package_dir / "migrations"

        if not migrations_path.is_dir():
            # Fallback: a source checkout keeps migrations at the repo root --
            # two levels up from openhands/automation, not one.
            repo_root_migrations = package_dir.parent.parent / "migrations"
            if repo_root_migrations.is_dir():
                migrations_path = repo_root_migrations
            else:
                msg = (
                    f"Migrations directory not found. "
                    f"Checked: {migrations_path}, {repo_root_migrations}"
                )
                raise RuntimeError(msg)

        alembic_cfg = Config()
        alembic_cfg.set_main_option("script_location", str(migrations_path))
        # Set the database URL for Alembic to use (sync version)
        db_url = normalize_sqlite_url_for_alembic(settings.db_url)
        alembic_cfg.set_main_option("sqlalchemy.url", db_url)

        # Run migrations synchronously (Alembic doesn't support async)
        try:
            command.upgrade(alembic_cfg, "head")
            logger.info("SQLite database migrations applied successfully")
        except Exception as e:
            logger.error(f"Failed to apply SQLite migrations: {e}")
            msg = f"SQLite migration failed. Database may be inconsistent: {e}"
            raise RuntimeError(msg) from e

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

    # Watchdog: marks stale RUNNING runs as FAILED and runs periodic janitors
    watchdog_task = asyncio.create_task(
        watchdog_loop(
            app.state.session_factory,
            settings=settings,
            shutdown_event=shutdown_event,
        )
    )
    app.state.watchdog_task = watchdog_task
    logger.info("Background watchdog started")

    # Git sync: mirrors automations to/from a git repo. Local mode only.
    git_sync_task = None
    config = get_config()
    if config.git_sync.git_sync_repo_url and not settings.is_local_mode:
        logger.warning(
            "AUTOMATION_GIT_SYNC_REPO_URL is set but the service is not in "
            "local mode (AUTOMATION_AGENT_SERVER_URL not configured); "
            "git sync will remain disabled."
        )
    if is_git_sync_supported():
        # Only once a repo exists: without one the loop is idle, and warning
        # about pushing to a repo that isn't configured is noise on the start
        # of every local deployment.
        if config.git_sync.git_sync_repo_url:
            logger.warning(
                "Git sync is enabled — automation prompts and metadata will be "
                "pushed to %s. Make sure that repo is private, since it may "
                "contain sensitive automation content.",
                config.git_sync.git_sync_repo_url,
            )
        # Started whenever the deployment could sync, not only when it is
        # already configured: the repo comes from the UI, and gating on it left
        # the loop (and mark_git_sync_dirty) off for the process lifetime,
        # reporting a healthy sync while exporting nothing.
        #
        # Started even while manual-only, since this task is what notices a
        # newly set interval. It idles without syncing while the interval is 0.
        git_sync_task = asyncio.create_task(
            git_sync_loop(app.state.session_factory, shutdown_event=shutdown_event)
        )
        app.state.git_sync_task = git_sync_task
        logger.info("Background git sync started")

    # Stream sources: long-lived inbound connections (Slack Socket Mode),
    # one supervised task each. Starts only once an app is configured.
    streams_task = None
    if config.streams.enabled:
        streams_task = asyncio.create_task(
            stream_supervisor_loop(
                app.state.session_factory,
                shutdown_event=shutdown_event,
            )
        )
        app.state.streams_task = streams_task
        logger.info("Background stream supervisor started")

    yield

    # Shutdown
    logger.info("Shutting down background tasks...")
    shutdown_event.set()

    # Wait for all tasks to exit gracefully
    shutdown_tasks: list[tuple[str, asyncio.Task]] = [
        ("scheduler", scheduler_task),
        ("dispatcher", dispatcher_task),
        ("watchdog", watchdog_task),
    ]
    if git_sync_task is not None:
        shutdown_tasks.append(("git_sync", git_sync_task))
    if streams_task is not None:
        shutdown_tasks.append(("streams", streams_task))

    for task_name, task in shutdown_tasks:
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
        version="1.10.0",  # x-release-please-version
        lifespan=lifespan,
        docs_url=f"{base_path}/docs",
        openapi_url=f"{base_path}/openapi.json",
        redoc_url=f"{base_path}/redoc",
    )


app = _create_app()


app.middleware("http")(api_route_telemetry_middleware)


app.add_middleware(TelemetryContextMiddleware)

# API-key requests (e.g. the local agent-server GUI calling directly from the
# browser) get permissive CORS; cookie/session requests keep the strict
# allowlist below. Mirrors the main cloud API's ApiKeyAwareCORSMiddleware.
app.add_middleware(
    ApiKeyAwareCORSMiddleware,
    allow_origins=_build_cors_origins(),
)

_base_path = get_settings().base_path

# Include specific routers BEFORE main router to avoid route conflict.
# The main router has /v1/{automation_id} which would match any /v1/<path>
# and fail UUID validation.
app.include_router(uploads_router, prefix=_base_path)
app.include_router(capabilities_router, prefix=_base_path)
app.include_router(preset_router, prefix=_base_path)
app.include_router(draft_router, prefix=_base_path)
app.include_router(event_router, prefix=_base_path)
app.include_router(webhook_router, prefix=_base_path)
app.include_router(telemetry_router, prefix=_base_path)
app.include_router(git_sync_router, prefix=_base_path)

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
        async with app.state.engine.connect():
            return {"status": "ready"}
    except Exception as e:
        logger.error("Readiness check failed: %s", e, exc_info=True)
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "error": "database unavailable"},
        )


@app.get("/sdk-version")
@app.get(f"{_base_path}/sdk-version")
async def sdk_version():
    """Return the SDK version this service is running.

    Called by setup.sh inside every automation sandbox to determine which
    openhands-sdk version to install.  No authentication required — the
    version string is not sensitive and must be readable before credentials
    are available in the sandbox.
    """
    try:
        version = get_sdk_version()
    except PackageNotFoundError:
        return JSONResponse(
            status_code=503,
            content={"error": "openhands-sdk package not found"},
        )
    return {"version": version}


@app.get("/server_info")
@app.get(f"{_base_path}/server_info")
async def server_info():
    """Return public metadata about the automation service."""
    try:
        return get_server_version_info()
    except PackageNotFoundError:
        return JSONResponse(
            status_code=503,
            content={"error": "openhands-sdk package not found"},
        )
