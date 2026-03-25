# Automations Service

Self-contained microservice that schedules and dispatches automation runs inside OpenHands Cloud sandboxes.

## Repository Structure

```
automation/
├── automation/              # Main application package
│   ├── app.py              # FastAPI app, lifespan, background tasks
│   ├── auth.py             # API key auth via OpenHands /api/keys/current
│   ├── config.py           # Pydantic settings (Settings, env prefix AUTOMATION_)
│   ├── constants.py        # Timeouts, polling intervals, sandbox constants
│   ├── db.py               # Database engine and session factory (asyncpg / Cloud SQL)
│   ├── dispatcher.py       # Polls PENDING runs, dispatches to sandbox (fire-and-forget)
│   ├── execution.py        # Sandbox lifecycle: create → upload → execute → delete
│   ├── logger.py           # JSON structured logging configuration
│   ├── models.py           # SQLAlchemy models (Automation, AutomationRun, TarballUpload)
│   ├── router.py           # API routes (CRUD, trigger, callback, runs list)
│   ├── scheduler.py        # Cron scheduler — polls automations, creates PENDING runs
│   ├── schemas.py          # Pydantic request/response schemas
│   ├── uploads.py          # Tarball upload router
│   ├── watchdog.py         # Staleness watchdog — marks hung runs as FAILED
│   ├── storage/            # File storage abstraction
│   │   ├── file_store.py   # Abstract base class for file storage
│   │   └── google_cloud.py # GCS implementation
│   └── utils/              # Utility modules
│       ├── api_key.py      # Per-user API key minting via service key
│       ├── cron.py         # Cron schedule utilities (next/prev fire time)
│       ├── run.py          # Run status transitions (create, mark, update)
│       ├── sandbox.py      # Sandbox verification and cleanup
│       ├── tarball_validation.py  # Tarball path validation (internal/external)
│       └── time.py         # UTC time helpers
├── containers/
│   └── Dockerfile          # Container image definition
├── migrations/              # Alembic migrations
├── scripts/
│   ├── test_automation.py  # E2E test (sandbox lifecycle with live streaming)
│   └── test_tarball/       # Tarball contents uploaded to sandbox during test
│       ├── main.py         # Test script run inside sandbox (SDK workspace test)
│       └── setup.sh        # Installs SDK inside sandbox
├── tests/                   # Unit tests (flat structure, no external deps)
│   ├── integration/        # Integration tests (require OPENHANDS_API_KEY)
│   ├── test_auth.py
│   ├── test_dispatcher.py
│   ├── test_execution.py
│   ├── test_router.py
│   ├── test_scheduler.py
│   └── ...
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
pre-commit run --files automation/**/*.py scripts/**/*.py tests/**/*.py --show-diff-on-failure

# Unit tests (no external deps, skips Docker-dependent tests)
uv run pytest tests/ -v --ignore=tests/integration

# Integration test (requires OPENHANDS_API_KEY)
OPENHANDS_API_KEY=sk-oh-... uv run pytest tests/integration/ -v

# E2E test script (live sandbox, ~80s)
OPENHANDS_API_KEY=sk-oh-... uv run python scripts/test_automation.py --api-url https://staging.all-hands.dev
```

## Dispatch Pipeline

The dispatcher uses a **fire-and-forget** model. For each PENDING run:

1. **Fetch per-user API key** — `get_api_key_for_automation_run()` mints a key via the service key
2. **Resolve tarball** — Internal (`oh-internal://`) downloads from GCS; external (HTTP) URLs are downloaded inside the sandbox
3. **Create sandbox** — `POST /api/v1/sandboxes` (Cloud API, Bearer token auth)
4. **Wait for RUNNING** — Poll `GET /api/v1/sandboxes?id=<id>` until status=RUNNING
5. **Upload/download tarball** — `POST /api/file/upload/<path>` (agent-server) or `curl` inside sandbox
6. **Start entrypoint** — `POST /api/bash/start_bash_command` (agent-server)
   - Extracts tarball, runs setup.sh (if present), exports env vars, runs entrypoint
7. **Return immediately** — Dispatcher does not wait for completion

Completion is handled asynchronously:
- **Happy path**: SDK inside sandbox POSTs to `POST /api/v1/automations/runs/{id}/complete`
- **Fallback**: Watchdog scans for runs past their `timeout_at` deadline, verifies status via sandbox bash history, and marks as COMPLETED or FAILED

### Env Vars Injected Into Sandbox

| Variable | Source | Purpose |
|----------|--------|---------|
| `OPENHANDS_API_KEY` | Per-user key issued via service key | SDK auth for get_llm()/get_secrets() |
| `OPENHANDS_CLOUD_API_URL` | Config (`openhands_api_base_url`) | Cloud API base URL |
| `SANDBOX_ID` | From sandbox creation response | SDK reads for settings API calls |
| `SESSION_API_KEY` | From sandbox creation response | SDK reads for settings API auth |
| `AUTOMATION_CALLBACK_URL` | Constructed by dispatcher | SDK posts completion status here |
| `AUTOMATION_RUN_ID` | Run ID | Included in callback payload |
| `AUTOMATION_EVENT_PAYLOAD` | Trigger context JSON | Available to user's script |

The SDK's `OpenHandsCloudWorkspace(local_agent_server_mode=True)` reads `SANDBOX_ID`, `SESSION_API_KEY`, and `AGENT_SERVER_PORT` from env vars automatically.

## Callback & Race Condition Handling

- **Callback auth**: The completion endpoint (`/runs/{id}/complete`) uses standard API key auth — the per-user `OPENHANDS_API_KEY` passed into the sandbox is validated via `authenticate_request`, and ownership is verified against the run's parent automation.
- **Optimistic locking**: Both callback endpoint and watchdog use `UPDATE ... WHERE status = 'RUNNING'` and check `CursorResult.rowcount` to handle races. Returns 409 on conflict.
- **Sandbox cleanup**: On callback, sandbox is deleted in a fire-and-forget background task (unless `keep_alive=True`). On dispatch failure, the dispatcher deletes the sandbox immediately.

## Database

- **Engine**: SQLAlchemy async with asyncpg; supports direct PostgreSQL (`AUTOMATION_DB_HOST`, `AUTOMATION_DB_PORT`, etc.) or GCP Cloud SQL connector (`AUTOMATION_GCP_DB_INSTANCE`)
- **Migrations**: Alembic in `migrations/` directory
- **Locking patterns**: `FOR UPDATE SKIP LOCKED` in scheduler/dispatcher polling, optimistic `UPDATE WHERE status=X` for callback/watchdog
