"""Demo for #673: a burst of runs against an org sandbox limit.

Five cron runs fire in one tick. A fake sandbox API admits at most two
concurrent sandboxes and answers the rest with the org concurrency-limit 429
(``ConcurrencyLimitReachedError``). A sandbox "finishes" (its completion
callback arrives) two dispatcher passes after it started. The real
``dispatch_pending_runs`` / ``_execute_run`` code runs against Postgres.

Copy into tests/ and run: DEMO_OUT=out.txt uv run pytest tests/demo_673.py
"""

import asyncio
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from openhands.automation.config import clear_config_cache
from openhands.automation.dispatcher import dispatch_pending_runs
from openhands.automation.exceptions import ConcurrencyLimitReachedError
from openhands.automation.models import Automation, AutomationRun, AutomationRunStatus
from openhands.automation.utils.run import mark_run_terminal


OUT = os.environ["DEMO_OUT"]
ORG = uuid.UUID("87654321-4321-8765-4321-876543218765")
USER = uuid.UUID("12345678-1234-5678-1234-567812345678")
ORG_SANDBOX_LIMIT = 2
PASSES = 9


def log(line=""):
    with open(OUT, "a") as f:
        f.write(line + "\n")


async def test_burst_against_org_limit(
    async_session_factory, mock_settings, monkeypatch
):
    for k, v in {
        "AUTOMATION_DEFER_BACKOFF_BASE_SECONDS": os.environ.get("DEMO_BACKOFF", "1"),
        "AUTOMATION_MAX_CONCURRENT_RUNS": os.environ.get("DEMO_CAP", "0"),
    }.items():
        monkeypatch.setenv(k, v)
    clear_config_cache()

    async with async_session_factory() as s:
        names = {}
        for i in range(5):
            a = Automation(
                user_id=USER, org_id=ORG, name=f"cron-{i + 1}",
                trigger={"type": "cron", "schedule": "*/5 * * * *", "timezone": "UTC"},
                tarball_path="s3://bucket/code.tar.gz", entrypoint="python3 main.py",
                enabled=True,
            )
            s.add(a)
            await s.flush()
            r = AutomationRun(automation_id=a.id, status=AutomationRunStatus.PENDING)
            s.add(r)
            await s.flush()
            names[r.id] = a.name
        await s.commit()

    sandboxes: dict[uuid.UUID, int] = {}  # run id -> pass it started in
    sandbox_api_calls = {"429": 0}
    current_pass = {"n": 0}

    async def fake_acquire(run, backend, client, session_factory, timeout, log_ctx):
        if len(sandboxes) >= ORG_SANDBOX_LIMIT:
            sandbox_api_calls["429"] += 1
            raise ConcurrencyLimitReachedError(
                "429: organization concurrency limit reached"
            )
        sandboxes[run.id] = current_pass["n"]
        return None  # sandbox started; the run stays RUNNING until its callback

    backend = MagicMock(is_local_mode=False)
    with (
        patch("openhands.automation.dispatcher.get_backend", return_value=backend),
        patch("openhands.automation.dispatcher._acquire_execution_context", fake_acquire),
        patch(
            "openhands.automation.dispatcher.capture_automation_event",
            new_callable=AsyncMock,
        ) as events,
    ):
        log(f"5 cron runs fire at once; the org sandbox limit is {ORG_SANDBOX_LIMIT}. "
            "A sandbox finishes 2 passes after it starts.")
        log(f"AUTOMATION_MAX_CONCURRENT_RUNS={os.environ.get('DEMO_CAP', '0')}  "
            f"AUTOMATION_DEFER_BACKOFF_BASE_SECONDS={os.environ.get('DEMO_BACKOFF', '1')}")
        log()
        for p in range(1, PASSES + 1):
            current_pass["n"] = p
            # Completion callbacks for sandboxes that have been running 2 passes.
            for run_id, started in list(sandboxes.items()):
                if p - started >= 2:
                    async with async_session_factory() as s:
                        run = await s.get(AutomationRun, run_id)
                    await mark_run_terminal(
                        async_session_factory, run, AutomationRunStatus.COMPLETED
                    )
                    del sandboxes[run_id]
            await dispatch_pending_runs(
                async_session_factory, mock_settings, MagicMock()
            )
            await asyncio.sleep(1.1)  # let the launched _execute_run tasks finish
            async with async_session_factory() as s:
                runs = (await s.execute(select(AutomationRun))).scalars().all()
            cells = []
            for r in sorted(runs, key=lambda r: names[r.id]):
                st = r.status.value
                if st == "PENDING" and r.deferred_until is not None:
                    st = f"DEFERRED x{r.deferral_count}"
                cells.append(f"{names[r.id]}={st}")
            log(f"pass {p}: " + "  ".join(cells))

        async with async_session_factory() as s:
            runs = (await s.execute(select(AutomationRun))).scalars().all()
        done = sum(r.status == AutomationRunStatus.COMPLETED for r in runs)
        skipped = [r for r in runs if r.status == AutomationRunStatus.SKIPPED]
        kinds = sorted({(r.status_detail or {}).get("kind") for r in skipped})
        event_names = sorted({c.args[0] for c in events.await_args_list})
        log()
        log(f"sandbox API 429s: {sandbox_api_calls['429']}   events: {', '.join(event_names)}")
        if skipped:
            log(f"RESULT: {done}/5 completed, {len(skipped)}/5 SKIPPED for good "
                f"(status_detail.kind={kinds}); their cron fire is lost")
        else:
            log(f"RESULT: {done}/5 completed, 0 dropped")
    clear_config_cache()
