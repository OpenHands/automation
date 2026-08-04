"""Regression tests for single-owner background workers (#286).

Uvicorn runs lifespan once per worker process. Without a gate, N workers start
N schedulers / dispatchers / watchdogs against the same DB. These tests pin the
``enable_background_workers`` ownership model from #286.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI

from openhands.automation.app import start_background_worker_tasks
from openhands.automation.config import ServiceSettings


async def _idle_loop(*_args, shutdown_event: asyncio.Event, **_kwargs) -> None:
    """Stand-in for scheduler/dispatcher/watchdog that exits on shutdown."""
    await shutdown_event.wait()


def _settings(*, enable: bool) -> ServiceSettings:
    return ServiceSettings(
        enable_background_workers=enable,
        base_url="https://example.test",
        scheduler_interval_seconds=60,
        dispatcher_interval_seconds=10,
        watchdog_interval_seconds=60,
    )


def _app_with_session_factory() -> FastAPI:
    app = FastAPI()
    app.state.session_factory = MagicMock(name="session_factory")
    return app


@pytest.mark.asyncio
async def test_disabled_starts_no_background_workers() -> None:
    """Request-serving workers must not start scheduler/dispatcher/watchdog."""
    app = _app_with_session_factory()
    shutdown_event = asyncio.Event()

    with (
        patch("openhands.automation.app.scheduler_loop", new=_idle_loop),
        patch("openhands.automation.app.dispatcher_loop", new=_idle_loop),
        patch("openhands.automation.app.watchdog_loop", new=_idle_loop),
    ):
        tasks = start_background_worker_tasks(
            app, _settings(enable=False), shutdown_event
        )

    assert tasks == []
    assert app.state.scheduler_task is None
    assert app.state.dispatcher_task is None
    assert app.state.watchdog_task is None


@pytest.mark.asyncio
async def test_enabled_starts_exactly_one_of_each_worker() -> None:
    """The dedicated background process owns one of each loop."""
    app = _app_with_session_factory()
    shutdown_event = asyncio.Event()

    with (
        patch("openhands.automation.app.scheduler_loop", new=_idle_loop),
        patch("openhands.automation.app.dispatcher_loop", new=_idle_loop),
        patch("openhands.automation.app.watchdog_loop", new=_idle_loop),
    ):
        tasks = start_background_worker_tasks(
            app, _settings(enable=True), shutdown_event
        )

    try:
        names = [name for name, _task in tasks]
        assert names == ["scheduler", "dispatcher", "watchdog"]
        assert app.state.scheduler_task is tasks[0][1]
        assert app.state.dispatcher_task is tasks[1][1]
        assert app.state.watchdog_task is tasks[2][1]
        assert all(not task.done() for _name, task in tasks)
    finally:
        shutdown_event.set()
        await asyncio.gather(*(task for _name, task in tasks))


@pytest.mark.asyncio
async def test_multi_worker_simulation_single_owner() -> None:
    """Simulate replicas×workers: only the process with the flag owns loops.

    Three "workers" start; two are request-serving (flag off) and one is the
    dedicated background owner (flag on). Across the deployment there must be
    exactly one scheduler, one dispatcher, and one watchdog.
    """
    worker_flags = (False, False, True)
    started: list[tuple[str, asyncio.Task]] = []
    shutdown_events: list[asyncio.Event] = []

    with (
        patch("openhands.automation.app.scheduler_loop", new=_idle_loop),
        patch("openhands.automation.app.dispatcher_loop", new=_idle_loop),
        patch("openhands.automation.app.watchdog_loop", new=_idle_loop),
    ):
        for enable in worker_flags:
            app = _app_with_session_factory()
            shutdown_event = asyncio.Event()
            shutdown_events.append(shutdown_event)
            started.extend(
                start_background_worker_tasks(
                    app, _settings(enable=enable), shutdown_event
                )
            )

    try:
        by_name: dict[str, list[asyncio.Task]] = {
            "scheduler": [],
            "dispatcher": [],
            "watchdog": [],
        }
        for name, task in started:
            by_name[name].append(task)

        assert len(by_name["scheduler"]) == 1
        assert len(by_name["dispatcher"]) == 1
        assert len(by_name["watchdog"]) == 1
        assert len(started) == 3
    finally:
        for event in shutdown_events:
            event.set()
        if started:
            await asyncio.gather(*(task for _name, task in started))


@pytest.mark.asyncio
async def test_multi_worker_all_enabled_still_duplicates() -> None:
    """Document the operator invariant: enabling on every worker still fans out.

    The gate does not elect a leader. If every uvicorn worker leaves the flag
    at its default (True), each still starts a full set of loops — same as
    before #286 for single-process deploys, but wrong for multi-worker.
    """
    started: list[tuple[str, asyncio.Task]] = []
    shutdown_events: list[asyncio.Event] = []

    with (
        patch("openhands.automation.app.scheduler_loop", new=_idle_loop),
        patch("openhands.automation.app.dispatcher_loop", new=_idle_loop),
        patch("openhands.automation.app.watchdog_loop", new=_idle_loop),
    ):
        for _ in range(3):
            app = _app_with_session_factory()
            shutdown_event = asyncio.Event()
            shutdown_events.append(shutdown_event)
            started.extend(
                start_background_worker_tasks(
                    app, _settings(enable=True), shutdown_event
                )
            )

    try:
        assert len(started) == 9  # 3 workers × 3 loops
        assert sum(1 for name, _ in started if name == "scheduler") == 3
        assert sum(1 for name, _ in started if name == "dispatcher") == 3
        assert sum(1 for name, _ in started if name == "watchdog") == 3
    finally:
        for event in shutdown_events:
            event.set()
        await asyncio.gather(*(task for _name, task in started))


def test_enable_background_workers_defaults_true() -> None:
    """Local / single-process deploys keep prior behavior without new env."""
    assert ServiceSettings().enable_background_workers is True


def test_enable_background_workers_env_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOMATION_ENABLE_BACKGROUND_WORKERS", "false")
    # ServiceSettings reads env at construction via pydantic-settings
    settings = ServiceSettings()
    assert settings.enable_background_workers is False
