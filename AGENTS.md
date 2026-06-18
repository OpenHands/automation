# Automations Service

Self-contained microservice that schedules and dispatches automation runs inside OpenHands Cloud sandboxes.

## Repository Structure

```
automation/
├── openhands/
│   └── automation/          # Main application package (openhands.automation namespace)
│       ├── app.py              # FastAPI app, lifespan, background tasks
│       ├── auth.py             # Auth via OpenHands /api/v1/users/me (API key + cookie)
│       ├── config.py           # Pydantic settings (Settings, env prefix AUTOMATION_)
│       ├── constants.py        # Timeouts, polling intervals, sandbox constants
│       ├── db.py               # Database engine and session factory (asyncpg / Cloud SQL)
│       ├── dispatcher.py       # Polls PENDING runs, dispatches to sandbox (fire-and-forget)
│       ├── execution.py        # Sandbox lifecycle: create → upload → execute → delete
│       ├── logger.py           # JSON structured logging configuration
│       ├── models.py           # SQLAlchemy models (Automation, AutomationRun, TarballUpload)
│       ├── router.py           # API routes (CRUD, trigger, callback, runs list)
│       ├── scheduler.py        # Cron scheduler — polls automations, creates PENDING runs
│       ├── schemas.py          # Pydantic request/response schemas
│       ├── uploads.py          # Tarball upload router
│       ├── watchdog.py         # Staleness watchdog — marks hung runs as FAILED
│       ├── storage/            # File storage abstraction
│       │   ├── file_store.py   # Abstract base class for file storage
│       │   └── google_cloud.py # GCS implementation
│       └── utils/              # Utility modules
│           ├── api_key.py      # Per-user API key minting via service key
│           ├── cron.py         # Cron schedule utilities (next/prev fire time)
│           ├── run.py          # Run status transitions (create, mark, update)
│           ├── sandbox.py      # Sandbox verification and cleanup
│           ├── tarball_validation.py  # Tarball path validation (internal/external)
│           └── time.py         # UTC time helpers
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

## Configuration

Configuration is centralized in `config.py` using a composed `AppConfig` with typed sections:

```python
from automation.config import get_config

config = get_config()
config.service.db_host          # ServiceSettings (AUTOMATION_ prefix)
config.storage.file_store       # StorageSettings (no prefix, SDK conventions)
config.http.auth_cache_ttl      # HttpSettings (AUTOMATION_ prefix)
config.sandbox.max_run_duration # SandboxSettings (AUTOMATION_ prefix)
config.kv.kv_secret             # KVSettings (AUTOMATION_ prefix)
config.log.log_level            # LogSettings (no prefix)
```

**Key principles:**
- Use `get_config().<section>` instead of deprecated `get_settings()`
- All environment variables documented in config class docstrings
- Protocol constants (WORK_DIR, TARBALL_PATH) in `constants.py` - these cannot be changed without breaking compatibility
- Shared logging context via `log_extra()` from `automation.utils`

## Build & Test Commands

```bash
# Pre-commit (run from repo root)
pre-commit run --files openhands/**/*.py scripts/**/*.py tests/**/*.py --show-diff-on-failure

# Unit tests (no external deps, skips Docker-dependent tests)
uv run pytest tests/ -v --ignore=tests/integration

# Integration test (requires OPENHANDS_API_KEY)
OPENHANDS_API_KEY=sk-oh-... uv run pytest tests/integration/ -v

# E2E test script (live sandbox, ~80s)
OPENHANDS_API_KEY=sk-oh-... uv run python scripts/test_automation.py --api-url https://staging.all-hands.dev
```

## PR-Specific Documents

When working on a PR that requires design documents, live-test logs, development-only scripts, or other temporary artifacts that should **not** be merged to `main`, store them in a `.pr/` directory at the repository root.

```bash
mkdir -p .pr

.pr/
├── design.md       # Design decisions and architecture notes
├── analysis.md     # Investigation or debugging notes
└── notes.md        # Any other PR-specific content
```

The `PR Artifacts` workflow warns reviewers when `.pr/` exists on a PR and automatically removes the directory with a follow-up commit when a same-repo PR is approved. Fork PRs must remove `.pr/` manually before merge.

Important notes:

- Do not put anything in `.pr/` that needs to be preserved.
- The `.pr/` check is informational during development; it posts a notice rather than blocking the PR.
- For fork PRs, remove `.pr/` manually before merging.


## Frontend Hosting

The Docker image includes the built frontend SPA and serves it via FastAPI.

- **Opt-in via `AUTOMATION_FRONTEND_DIR`** — set to the directory containing built assets. Empty = disabled (default locally). Dockerfile sets it to `/app/frontend-dist`.
- **Mount path** derived from `base_url` via `Settings.frontend_path` (same pattern as `base_path`). Defaults to `/automations`.
- **SPA fallback** via `_SPAStaticFiles.lookup_path` — unknown paths resolve to `index.html`.
- **Cache**: `immutable` for hashed `assets/*`, `no-cache` for everything else.

## Dispatch Pipeline

The dispatcher uses a **fire-and-forget** model. For each PENDING run:

1. **Fetch per-user API key** — `get_api_key_for_automation_run()` mints a key via the service key
2. **Resolve tarball** — Internal (`oh-internal://`) downloads from GCS; external (HTTP) URLs are downloaded inside the sandbox
3. **Create sandbox** — `POST /api/v1/sandboxes` (Cloud API, Bearer token auth)
4. **Wait for RUNNING** — Poll `GET /api/v1/sandboxes?id=<id>` until status=RUNNING
5. **Upload/download tarball** — `POST /api/file/upload/<path>` (agent-server) or `curl` inside sandbox
6. **Start entrypoint** — `POST /api/bash/start_bash_command` (agent-server)
   - Shell path: extracts tarball, runs `setup.sh` (if present), exports env vars, runs entrypoint
   - Cross-platform preset path: when `setup_script_path is null`, execution uses an inline Python runner to extract the tarball, inject env vars, and launch the entrypoint without POSIX shell features
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

Supports **PostgreSQL** (cloud) and **SQLite** (local/self-hosted).

| Feature | PostgreSQL | SQLite |
|---------|------------|--------|
| Config | `AUTOMATION_DB_HOST`, `AUTOMATION_DB_PORT`, etc. | `AUTOMATION_DB_URL=sqlite+aiosqlite:///path.db` |
| Driver | asyncpg | aiosqlite |
| Row locking | `FOR UPDATE SKIP LOCKED` | Skipped (single-process) |
| Migrations | `alembic upgrade head` (manual) | Auto-run on startup |

### Writing Migrations

Migrations must be **cross-database compatible**:

```python
# ✅ DO: Import and use generic SQLAlchemy types
from sqlalchemy import Column, JSON, Uuid
Column("id", Uuid, primary_key=True)
Column("data", JSON, nullable=False)

# ❌ DON'T: Use PostgreSQL-specific types
from sqlalchemy.dialects.postgresql import UUID, JSONB
Column("id", UUID(as_uuid=True), ...)  # Won't work on SQLite
Column("data", JSONB, ...)             # Won't work on SQLite
```

For PostgreSQL-only features (partial indexes, advisory locks), use conditionals:

```python
def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"

def upgrade() -> None:
    # ... create tables ...
    if not _is_sqlite():
        op.create_index("ix_partial", "table", ["col"], postgresql_where=...)
```

- **Locking patterns**: `FOR UPDATE SKIP LOCKED` in scheduler/dispatcher — check `using_sqlite()` to skip on SQLite

## Preset-Based Automation Creation

Presets are ready-to-use automation configurations where users provide arguments (like a prompt) instead of writing SDK scripts.

### Prompt Preset

The `/v1/preset/prompt` endpoint allows creating automations by simply providing a prompt, without manually creating and uploading a tarball.

#### How It Works

1. User sends `POST /v1/preset/prompt` with `name`, `prompt`, and `trigger`
2. Service generates SDK boilerplate code with the user's prompt
3. Creates a tarball containing:
   - `main.py` - SDK boilerplate that loads and executes the prompt
   - `bootstrap.py` - stdlib-only cross-platform bootstrap that creates `.venv`, installs the matching OpenHands SDK packages, and re-execs `main.py`
   - `prompt.txt` - The user's prompt text
4. Uploads the tarball to storage (creates `TarballUpload` record)
5. Creates the `Automation` record referencing the internal upload with `setup_script_path=None` and a bootstrap entrypoint (`python bootstrap.py` on POSIX, `py -3 bootstrap.py` on Windows)

#### Files

- `openhands/automation/preset_router.py` - Endpoint and tarball generation logic
- `openhands/automation/presets/prompt/sdk_main.py` - SDK boilerplate that fetches LLM, secrets, and MCP config
- `openhands/automation/presets/prompt/bootstrap.py` - stdlib-only cross-platform preset bootstrap
- `openhands/automation/presets/prompt/setup.sh` - legacy shell bootstrap retained for reference/tests but no longer used by generated presets

#### Request Schema

```json
{
  "name": "My Automation",
  "prompt": "Create a file called hello.txt with 'Hello World' inside",
  "trigger": {"type": "cron", "schedule": "0 9 * * 1", "timezone": "UTC"},
  "timeout": 300  // optional
}
```

### Notes

- The `presets/` directory is excluded from ruff and pyright linting since it contains SDK code that runs in the sandbox, not application code
- Generated presets now launch `bootstrap.py` (`python bootstrap.py` on POSIX, `py -3 bootstrap.py` on Windows) and set `setup_script_path=None`
- Future presets (e.g., plugins) can be added as additional subdirectories under `openhands/automation/presets/`

## Release Procedure

Releases publish `openhands-automation` to PyPI and retag the Docker image on GHCR. There are two paths:

### Automated (preferred) — Prepare Release workflow

1. Go to **Actions → Prepare Release** on GitHub and trigger it with the desired version (e.g. `1.0.0a3`).
2. The workflow opens a PR that bumps `pyproject.toml` and regenerates `uv.lock`.
3. Review and merge the PR.
4. After merging, pull `main` and push a tag to trigger publishing:

```bash
git checkout main && git pull origin main
git tag <version>          # e.g. git tag 1.0.0a3
git push origin <version>
```

### Manual

1. Edit `pyproject.toml` — set `version = "<new-version>"`.
2. Regenerate the lock file: `uv lock`.
3. Commit both files: `git commit -am "chore: bump version to <new-version>"`.
4. Merge (or push directly to main if you have access).
5. Tag and push (the tag can point to any commit, including a PR branch head):

```bash
git tag <version>          # e.g. git tag 1.0.0a3
git push origin <version>
```

### What the tag triggers

| Workflow | File | Action |
|----------|------|--------|
| Publish PyPI Package | `pypi-release.yml` | Builds and publishes `openhands-automation` to PyPI via OIDC trusted publishing |
| Tag Docker images | `tag-image.yml` | Aliases the existing `sha-<commit>` GHCR image to the new tag (requires the Docker build for that commit to have run first) |

### SDK dependency bumps

When bumping `openhands-sdk` / `openhands-workspace` pins:
1. Update both versions in `pyproject.toml` dependencies.
2. Run `uv lock` to regenerate `uv.lock`.
3. Bump the package version and follow the release procedure above.
4. After publishing, update `AUTOMATION_SHA` in the deploy repo:
   - `.github/workflows/deploy.yaml`
   - `.github/workflows/deploy-automation.yaml`
