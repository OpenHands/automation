#!/usr/bin/env python3
"""Live Slack routing proof for PR #446.

Uses the real Socket Mode provider, Slack Web API, ingestion pipeline, and an
in-memory SQLite database. It deliberately leaves the first run queued: the
human follow-up must be coalesced into that same subject-owning run, while a
message in any unrelated thread cannot create another run.
"""

# Environment must be quieted before importing OpenHands modules.
# ruff: noqa: E402,I001
from __future__ import annotations

import asyncio
import os
import secrets
import sys
import uuid
from typing import Any

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("AUTOMATION_LOG_LEVEL", "WARNING")

from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from openhands.automation.conversations import COALESCED_TURNS_KEY
from openhands.automation.db import set_sqlite_mode
from openhands.automation.ingest import AcceptedEvent, accept_event
from openhands.automation.models import Automation, AutomationRun, Base
from openhands.automation.streams.slack import SlackStreamProvider


REQUIRED_ENV = ("SLACK_APP_TOKEN", "SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID")
LOCAL_USER_ID = uuid.uuid5(uuid.NAMESPACE_DNS, "openhands-local-user")
LOCAL_ORG_ID = uuid.uuid5(uuid.NAMESPACE_DNS, "openhands-local-org")


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"Missing {name}", file=sys.stderr)
        raise SystemExit(2)
    return value


def slack_inner_event(event: AcceptedEvent) -> dict[str, Any]:
    inner = event.payload.get("event")
    return inner if isinstance(inner, dict) else {}


async def main() -> None:
    values = {name: require_env(name) for name in REQUIRED_ENV}
    app_token = values["SLACK_APP_TOKEN"]
    bot_token = values["SLACK_BOT_TOKEN"]
    channel_id = values["SLACK_CHANNEL_ID"]

    slack = AsyncWebClient(token=bot_token)
    identity = await slack.auth_test()
    team_id = str(identity["team_id"])
    bot_user_id = str(identity["user_id"])
    marker = f"oh-routing-e2e-{secrets.token_hex(3)}"

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    set_sqlite_mode(True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    automation = Automation(
        user_id=LOCAL_USER_ID,
        org_id=LOCAL_ORG_ID,
        name="Slack thread routing live demo",
        trigger={
            "type": "event",
            "source": "slack",
            "on": ["app_mention", "message"],
            "filter": (
                f"event.channel == '{channel_id}' && "
                f"(event.type == 'message' || contains(event.text, '{marker}'))"
            ),
            "destination": "continue_conversation",
            "subject_key_expr": (
                "join('/', [team_id, event.channel, event.thread_ts || event.ts])"
            ),
            "turn_text_expr": "event.text",
            "wake_agent": False,
        },
        tarball_path="oh-internal://uploads/live-demo.tar.gz",
        entrypoint="python main.py",
        keep_alive=True,
    )
    async with sessions() as session:
        session.add(automation)
        await session.commit()

    shutdown = asyncio.Event()
    root_ts: str | None = None
    proof: dict[str, Any] = {}

    async def emit(event: AcceptedEvent) -> None:
        nonlocal root_ts
        inner = slack_inner_event(event)

        async with sessions() as session:
            before = list((await session.execute(select(AutomationRun))).scalars())
            result = await accept_event(LOCAL_ORG_ID, event, session)
            after = list((await session.execute(select(AutomationRun))).scalars())

            if event.event_key == "app_mention" and result.run_ids:
                root_ts = str(inner["ts"])
                await slack.chat_postMessage(
                    channel=channel_id,
                    thread_ts=root_ts,
                    text=(
                        "Routing demo is active. Reply here without mentioning me; "
                        "the reply should continue this owned thread."
                    ),
                )
                print(f"\nBot replied in thread {root_ts}.")
                print("Now send a normal human reply in that thread (no @mention).")
                return

            if event.event_key != "message" or not root_ts:
                return
            if inner.get("thread_ts") != root_ts:
                if len(after) != len(before):
                    raise AssertionError("An unrelated thread created a run")
                return

            if not result.conversation_ids:
                raise AssertionError("The owned human-rooted reply was not continued")
            if len(after) != 1:
                raise AssertionError(f"Expected one run, found {len(after)}")

            turns = (after[0].event_payload or {}).get(COALESCED_TURNS_KEY, [])
            if not turns or str(inner.get("text", "")) not in turns[-1]:
                raise AssertionError("The follow-up was not stored on the owned run")

            proof.update(
                run_id=str(after[0].id),
                subject_key=after[0].subject_key,
                conversation_id=result.conversation_ids[0],
                follow_up=inner.get("text"),
                total_runs=len(after),
            )
            shutdown.set()

    provider = SlackStreamProvider(
        org_id=LOCAL_ORG_ID,
        app_token=app_token,
        bot_token=bot_token,
        team_id=team_id,
        bot_user_id=bot_user_id,
    )

    print("Slack identity verified; token values were not logged.")
    print(f"In channel {channel_id}, send: <@{bot_user_id}> {marker}")
    print("Waiting up to five minutes for the mention and follow-up...")
    try:
        await asyncio.wait_for(provider.run(emit, shutdown), timeout=300)
    except TimeoutError:
        raise SystemExit("Timed out waiting for the Slack demonstration") from None
    finally:
        await engine.dispose()
        set_sqlite_mode(False)

    print("\nPASS — live Slack thread routing is correct")
    print(f"  run_id:         {proof['run_id']}")
    print(f"  subject_key:    {proof['subject_key']}")
    print(f"  conversation:   {proof['conversation_id']}")
    print(f"  follow-up:      {proof['follow_up']!r}")
    print(f"  total runs:     {proof['total_runs']} (expected 1)")


if __name__ == "__main__":
    asyncio.run(main())
