# Automations Service

Self-contained microservice that schedules and dispatches automation runs inside OpenHands Cloud sandboxes.

## Repository Structure

```
automation/
├── automation/          # Main application package
│   ├── app.py          # FastAPI app, lifespan, background tasks
│   ├── config.py       # Pydantic settings (AutomationSettings)
│   ├── dispatcher.py   # Polls PENDING runs, dispatches to sandbox
│   ├── execution.py    # SandboxExecutor — sandbox lifecycle (create/upload/execute/delete)
│   ├── models.py       # SQLAlchemy models (Automation, AutomationRun)
│   ├── router.py       # API routes (CRUD, trigger, callback, runs list)
│   ├── scheduler.py    # Cron scheduler — polls automations, creates PENDING runs
│   ├── schemas.py      # Pydantic request/response schemas
│   └── watchdog.py     # Staleness watchdog — marks hung runs as FAILED
├── migrations/          # Alembic migrations
├── scripts/
│   ├── test_automation.py       # E2E test (sandbox lifecycle with live streaming)
│   └── test_tarball/            # Tarball contents uploaded to sandbox during test
│       ├── main.py              # Test script run inside sandbox (SDK workspace test)
│       └── setup.sh             # Installs SDK inside sandbox
├── tests/
│   ├── unit/                    # Unit tests (no external deps)
│   └── integration/             # Integration tests (require OPENHANDS_API_KEY)
└── pyproject.toml
```

## Cross-Repo Coordination

Three repos work together:

| Repo | Branch | Purpose |
|------|--------|---------|
| `OpenHands/automation` | `dispatch-phase1b` | Automation service (this repo) |
| `OpenHands/deploy` (aka `All-Hands-AI/deploy`) | `dispatch-phase1b` | Deploys automation as a sidecar |
| `OpenHands/software-agent-sdk` | `feat/saas-runtime-mode` | SDK changes for in-sandbox execution |

**AUTOMATION_SHA linking**: The deploy repo references a specific automation commit in two workflow files:
- `.github/workflows/deploy.yaml` → `AUTOMATION_SHA: "<full-sha>"`
- `.github/workflows/deploy-automation.yaml` → `AUTOMATION_SHA: "<full-sha>"`

After pushing to the automation repo, update both files in the deploy repo.

## Build & Test Commands

```bash
# Pre-commit (run from repo root)
pre-commit run --files automation/**/*.py scripts/**/*.py tests/**/*.py --show-diff-on-failure --config ./dev_config/.pre-commit-config.yaml

# Unit tests (no external deps, skips Docker-dependent tests)
uv run pytest tests/ -v --ignore=tests/integration

# Integration test (requires OPENHANDS_API_KEY)
OPENHANDS_API_KEY=sk-oh-... uv run pytest tests/integration/ -v

# E2E test script (live sandbox, ~80s)
OPENHANDS_API_KEY=sk-oh-... uv run python scripts/test_automation.py --api-url https://staging.all-hands.dev
```

## Dispatch Pipeline

The dispatcher executes this sequence for each PENDING run:

1. **Create sandbox** — `POST /api/v1/sandboxes` (Cloud API, Bearer token auth)
2. **Wait for RUNNING** — Poll `GET /api/v1/sandboxes?id=<id>` until status=RUNNING
3. **Upload tarball** — `POST /api/file/upload/<path>` (agent-server, X-Session-API-Key auth)
4. **Execute command** — `POST /api/bash/start_bash_command` (agent-server)
   - Extracts tarball, runs setup.sh, exports env vars, runs entrypoint
5. **Stream output** — Poll `GET /api/bash/bash_events/search` (agent-server)
6. **Cleanup sandbox** — `DELETE /api/v1/sandboxes/<id>` (Cloud API)

### Env Vars Injected Into Sandbox

| Variable | Source | Purpose |
|----------|--------|---------|
| `OPENHANDS_API_KEY` | Per-user key issued via admin key | SDK auth for get_llm()/get_secrets() |
| `OPENHANDS_CLOUD_API_URL` | Config | Cloud API base URL |
| `SANDBOX_ID` | From sandbox creation response | SDK reads for settings API calls |
| `SESSION_API_KEY` | From sandbox creation response | SDK reads for settings API auth |
| `AUTOMATION_CALLBACK_URL` | Constructed by dispatcher | SDK posts completion status here |
| `AUTOMATION_RUN_ID` | Run ID | Included in callback payload |
| `AUTOMATION_EVENT_PAYLOAD` | Trigger context JSON | Available to user's script |

The SDK's `OpenHandsCloudWorkspace(local_agent_server_mode=True)` reads `SANDBOX_ID`, `SESSION_API_KEY`, and `AGENT_SERVER_PORT` from env vars automatically.

## Callback & Race Condition Handling

- **Callback auth**: One-time `callback_token` (secrets.token_urlsafe(32)) minted at dispatch, validated with `hmac.compare_digest`, invalidated after use.
- **Optimistic locking**: Both callback endpoint and watchdog use `UPDATE ... WHERE status = 'RUNNING'` and check `CursorResult.rowcount` to handle races. Returns 409 on conflict.

## Database

- **Engine**: SQLAlchemy async with PostgreSQL (via `DATABASE_URL` env var)
- **Migrations**: Alembic in `migrations/` directory
- **Locking patterns**: `FOR UPDATE SKIP LOCKED` in scheduler/dispatcher polling, optimistic `UPDATE WHERE status=X` for callback/watchdog
