"""FastAPI application entrypoint."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from automation.config import get_settings
from automation.db import dispose_engine, get_engine
from automation.models import Base
from automation.router import router
from automation.scheduler import start_scheduler, stop_scheduler


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Application startup/shutdown lifecycle."""
    # Startup
    settings = get_settings()
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=log_level)
    logger.info("Starting OpenHands Automations Service")

    # Create tables (for dev/SQLite; production uses Alembic migrations)
    engine = await get_engine()
    if settings.sqlite_path is not None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    # Start the background scheduler
    start_scheduler()
    logger.info("Scheduler started")

    yield

    # Shutdown
    stop_scheduler()
    await dispose_engine()
    logger.info("Automations service shut down")


app = FastAPI(
    title="OpenHands Automations Service",
    description="Scheduled and event-driven automation execution for OpenHands Cloud",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def readiness():
    """Readiness probe — checks DB connectivity."""
    try:
        engine = await get_engine()
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        return {"status": "ready"}
    except Exception as e:
        return {"status": "not_ready", "error": str(e)}
