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
│       ├── git_sync/           # Bidirectional git sync (local mode only, see below)
│       │   ├── client.py       # Async `git` CLI wrapper (clone/pull/commit/push)
│       │   ├── loop.py         # Sync cycle, background loop, mark_git_sync_dirty hook
│       │   ├── router.py       # Git sync status/trigger API
│       │   ├── schemas.py      # Git sync request/response schemas
│       │   └── serializer.py   # Automation <-> git file-tree (de)serializer
│       ├── streams/            # Stream sources (Slack Socket Mode, see below)
│       │   ├── base.py         # StreamProvider protocol, per-source health
│       │   ├── slack.py        # Slack Socket Mode provider
│       │   └── supervisor.py   # Source registry, supervised task per source
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
config.git_sync.git_sync_repo_url # GitSyncSettings (AUTOMATION_ prefix)
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

## Product Telemetry Identity

- Cloud product events use the server-authoritative Cloud user ID as the PostHog `distinct_id`; never trust a client telemetry header as Cloud identity.
- Local Agent Canvas requests carry their consented PostHog identity in `X-OpenHands-Telemetry-Distinct-Id`. Store that value on newly created automations and runs so asynchronous lifecycle events retain the same identity.
- Local events with no Canvas attribution fall back to the DB-backed automation backend ID. Keep `automation_backend_id` as an event property for installation analysis rather than substituting it for a known person identity.
- Local consent is stored per frontend distinct ID. Attributed events require consent for their resolved identity; only unattributed backend-level events use the aggregate installation consent.

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
| `AUTOMATION_EVENT_PAYLOAD` | Trigger context JSON | Available to user's script; preset scripts also use it to set a descriptive conversation title |

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
   - `prompt.txt` - The user's prompt text
   - `setup.sh` - SDK installation script
4. Uploads the tarball to storage (creates `TarballUpload` record)
5. Creates the `Automation` record referencing the internal upload

#### Files

- `openhands/automation/preset_router.py` - Endpoint and tarball generation logic
- `openhands/automation/presets/prompt/sdk_main.py` - SDK boilerplate that fetches LLM, secrets, and MCP config
- `openhands/automation/presets/prompt/setup.sh` - SDK installation script (installs from PyPI)

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
- The generated tarball uses `python main.py` as the entrypoint and `setup.sh` as the setup script
- Future presets (e.g., plugins) can be added as additional subdirectories under `openhands/automation/presets/`

## Catalog Bundles

A catalog entry in `OpenHands/extensions` normally ships a prompt, created
through a preset endpoint. An entry whose automation is deterministic machinery
— polling, dedupe, state, fixed API calls — ships a **script tarball** instead,
which the host uploads (`POST /v1/uploads`) and creates from (`POST /v1`).

Execution is unchanged: a bundle run is an ordinary tarball run. What the raw
path gained is the catalog contract the presets already had:

- **`template` provenance** (`TemplateProvenance` in `schemas.py`), accepted by
  all three creation endpoints and stored under `preset_metadata["template"]`.
  `find_existing_template_automation` (`utils/templates.py`) is consulted first,
  so enabling an entry twice returns the existing automation with a 200.
  `record_first_run_outcome` keys on the same value, so bundles get first-run
  telemetry for free.
- **Preflight**: `POST /v1/validate` accepts `"endpoint": "/v1"`. It validates
  the body, not the upload — `tarball_path` ownership is checked at creation.
- **`customTarball`** in `GET /v1/capabilities` `features`, so an entry can
  require it and a client can tell whether a bundle card is runnable here.

`CreateAutomationRequest` has no `repos`, so a bundle cannot ask the service to
clone; it fetches what it needs itself.

## Git Sync

Local/self-hosted deployments can mirror their automations to a git repo for
visibility, versioning, and backup — see issue #300. **Only active in local
mode** (`AUTOMATION_AGENT_SERVER_URL` set): a single repo maps to a single
agent server, which doesn't make sense for the multi-tenant SaaS deployment.

- Configuring a repo is what enables sync — there is no separate feature
  flag. The repo URL may come either from `AUTOMATION_GIT_SYNC_REPO_URL` or
  from the Git Sync page, and nothing syncs until one is set. The background
  loop and the `dirty` CRUD hook start in every local-mode deployment
  (`is_git_sync_supported`), idle, so a repo configured from the UI syncs
  without a restart. See `GitSyncSettings` in
  `config.py` for the full list of env vars.
- Each automation is stored under `{git_sync_path}/{slug}/` as `automation.yaml`
  (metadata) plus its tarball contents extracted under `tarball/**`. Those are
  the only paths the exporter owns (`is_generated_path`): other files committed
  in the same directory are left alone. `automation.yaml` carries a
  `tarball_executables` list when any tarball member is executable, since the
  extracted files are committed as plain content and the mode would otherwise
  be lost on the way back in.
- `git_sync_path` is validated by `normalize_git_sync_path`: repo-relative, no
  `..`, no leading/trailing slashes, never empty. It is joined onto the local
  checkout and passed to `git add -- <path>`, and each slug directory under it
  is pruned during export, so a traversing value would delete host directories.
- `git_sync/router.py` exposes `GET /v1/git-sync/status`,
  `PUT /v1/git-sync/config`, `POST /v1/git-sync/sync` (manual trigger), and
  `POST /v1/git-sync/check`.
- `POST /v1/git-sync/check` takes the same body as `PUT /config` and reports
  whether *that* configuration can reach its repo, without saving it or
  syncing: `resolve_candidate_git_sync_settings` merges the update in memory,
  and `check_remote_access` runs a single `git ls-remote`. Keep it that way —
  validating by running a cycle would clone, import whatever the repo holds
  into the local automations, and push every dirty automation to a URL nobody
  has vetted yet, which for a mistyped URL is the damage rather than the
  diagnosis. It proves read access only: a token with no write scope passes
  and still fails at push time, and the encryption key is never exercised. A
  branch that doesn't exist yet is reported as `branch_exists: false`, not a
  failure — the first cycle creates it. A bad configuration comes back as
  `200 {"ok": false, "detail": ...}`, since the request itself succeeded.
- **Sync is manual by default**: the interval defaults to `0`, meaning a
  cycle runs only when `POST /v1/git-sync/sync` is called. Set a positive
  `interval_seconds` via `PUT /v1/git-sync/config` (the UI) to also sync
  automatically. Unlike every other git-sync setting it has **no environment
  variable** — it is runtime-only config, stored with the other overrides
  (see `DEFAULT_SYNC_INTERVAL_SECONDS` in `git_sync/config_override.py`).
  The background loop always runs so a newly-set interval takes effect
  without a restart; while the interval is 0 it idles without syncing.
  Either way one cycle is the same bidirectional pull → import → export →
  push; there's no way to run a single direction.
- Conflict policy: an automation is marked `dirty` (in `AutomationGitSyncState`)
  on every create/update/delete via the API; the sync loop treats a dirty
  automation as authoritative over a conflicting git-side change for the same
  cycle — the VM always wins until its change has been pushed. The import skips
  a dirty slug and the export then compares against what is actually on disk,
  not against `state.content_hash`: a git-side edit leaves the DB row untouched,
  so a hash comparison would skip the write and strand the two versions.
- Automations that predate git sync being switched on have no state row, and the
  export only reads dirty ones. `_backfill_missing_states` creates them (dirty)
  at the start of every cycle, so the first sync exports everything rather than
  reporting success against an empty repo.
- Automations created directly in git (e.g. via a PR) are imported and
  stamped with the deterministic local-mode user/org IDs from `auth.py`'s
  `_get_local_user()`.
- **Encryption**: set `AUTOMATION_GIT_SYNC_ENCRYPTION_KEY` to encrypt file
  contents (via the SDK's Fernet-based `Cipher`, same primitive as the KV
  store) before they're committed. Reading a repo written before encryption
  was turned on still works — plaintext files pass through unchanged.
- **Runtime config**: `PUT /v1/git-sync/config` reconfigures or pauses/resumes
  an already-running sync (repo/branch/path/token/encryption key/author)
  without a restart, via `git_sync/config_override.py` (overrides stored as
  JSON in `automation_service_metadata`). Setting a repo URL there enables
  sync without a restart; only a deployment that can't sync at all (not local
  mode) is refused, on both the config endpoint (409) and the manual trigger
  (503).
- The token and encryption key in that blob are encrypted at rest by
  `git_sync/secret_store.py`, wrapped with `AUTOMATION_KV_SECRET` when the
  deployment sets one and otherwise with a key generated into a 0600 file
  under the workspace. If neither can be obtained, `PUT /config` fails with a
  503 rather than storing the secret in the clear.
- `GET /v1/git-sync/status` also reports `last_error`/`last_error_at` from
  the most recent failed cycle, cleared on the next successful one.
- **In-flight cycles**: a cycle writes its outcome only when it ends, so
  `/status` also reports `sync_in_progress`/`sync_started_at` — otherwise a
  caller can't tell a running sync from one that never started, and a cycle
  the periodic loop began is invisible to the UI. The flag is in-process
  state (`get_sync_started_at`), scoped like the `_sync_cycle_lock` it
  shadows: a crash mid-cycle can't strand it the way a persisted flag would.
  `POST /v1/git-sync/sync` returns `triggered: false` instead of scheduling
  when one is already running — that cycle covers everything the new one
  would, and the lock would only queue it behind anyway.

## Stream Sources

Some events arrive over a connection this service holds open rather than over
an inbound HTTP request — today that means Slack Socket Mode. Everything past
the transport is unchanged: a streamed event goes through `accept_event()` like
any webhook, so it matches **unmodified automation definitions** and is
deduplicated by `IntegrationEvent` on the provider's own delivery id.

- **Off by default.** `AUTOMATION_STREAMS_ENABLED` plus at least one configured
  app is what starts the supervisor; with it off nothing changes. This is a
  self-hosted capability: Slack does not allow Socket Mode apps in the public
  Marketplace, and a held-open connection does not fit a stateless autoscaled
  tier. Webhooks remain the cloud path.
- **Configuration is environment-only** — `AUTOMATION_SLACK_APPS`, a JSON list
  of `{org_id, app_token, bot_token, team_id, bot_user_id}`. There is
  deliberately no table and no CRUD API: per-org socket configuration is a
  multi-tenant requirement, and taking it now would cost a migration and a
  credential-encryption surface before anyone needs it. See `StreamSettings` in
  `config.py`.
- **A provider owns its own loop.** `StreamProvider.run(emit, shutdown)` holds
  the connection and pushes events out; it is not polled. A callback-driven SDK
  like `slack_sdk` already reconnects correctly and forcing it into a
  `receive()` shape means reimplementing that badly.
- **The supervisor catches per source.** Each source is a child task whose
  exceptions are caught and restarted with exponential backoff, so one bad
  provider cannot take down the others, the scheduler, the dispatcher, or HTTP
  webhook handling — the isolation a separate systemd unit used to provide. A
  `StreamConfigError` is terminal: nothing a restart can fix.
- **Slack semantics**, ported from the OSS VM's `slack-socket-bot` bridge:
  ack the envelope before any other work (Slack redelivers anything unacked),
  drop the bot's own messages (`bot_id`/`subtype`), assert `team_id` on every
  envelope, and assert both `team_id` and `bot_user_id` against `auth.test`
  *before* connecting, so a mis-pasted token fails loudly instead of bridging
  the wrong workspace. The payload handed to `accept_event()` is the whole
  Slack envelope, which is what the bridge POSTs today and what existing
  triggers filter on (`team_id == '...'`).
- **Health** is process-local (`stream_health()`): `last_connected_at`,
  `last_event_at`, `consecutive_failures` per connection. Nothing renders it
  yet.
- Slack permits up to 10 concurrent Socket Mode connections per app and
  distributes payloads across them, so this is safe active-active and a rolling
  deploy can drop nothing. That is a Slack guarantee, not a general one — see
  `Capabilities.tolerates_multiple_connections` in `providers.py`.

Cutover on the OSS VM: deploy with the flag on, verify runs are created from
in-service events, then `systemctl disable --now slack-socket-bot`. No
automation definitions change; roll back by re-enabling the unit.

## Release Procedure

Releases are driven by [release-please](https://github.com/googleapis/release-please)
via the centralized reusable workflows in
[`OpenHands/release-actions`](https://github.com/OpenHands/release-actions). You do
**not** bump versions or push tags by hand — release-please does both from the
Conventional Commit history. (The old manual `Prepare Release` workflow has been
removed.)

### Day-to-day flow

1. Open a PR against `main` with a [Conventional Commit](https://www.conventionalcommits.org)
   title (`feat: …`, `fix: …`, `docs: …`, …). The `pr` workflow lints the title and
   applies a `type: <type>` label.
2. Squash-merge it. The **PR title becomes the commit message** release-please reads,
   so the repo is configured to squash-merge with `PR_TITLE` as the commit title.
3. On merge, release-please opens/updates a `chore(main): release X.Y.Z` PR that:
   - bumps the version everywhere it is embedded (see table below), derived from the
     merged commit types — `fix` → patch, `feat` → minor, breaking → major;
   - aggregates the GitHub release notes (grouped by `type:` label via
     `.github/release.yml`).
4. **Merging that release PR publishes the release**: it tags the commit (e.g. `1.1.0`,
   no `v` prefix) and publishes the GitHub release. The tag then triggers the publish
   workflows below, and shipped PRs are back-labeled `released: X.Y.Z`.

### Versions release-please keeps in sync

A single repo version is applied to every embedded location (configured in
`release-please-config.json`):

| File | How it is updated |
|------|-------------------|
| `pyproject.toml` (`[project].version`) | native `python` release-type |
| `uv.lock` (root package) | `toml` updater (`$.package[?(@.name.value=='openhands-automation')].version`) |
| `openhands/automation/__init__.py` (`__version__`) | `generic` updater (`# x-release-please-version`) |
| `openhands/automation/app.py` (FastAPI `version=`) | `generic` updater (`# x-release-please-version`) |

The lock file is bumped so the release PR's own CI (`uv sync --frozen`)
stays green. Keep the `# x-release-please-version` annotations on the `__init__.py`
and `app.py` version lines.

### What the release tag triggers

| Workflow | File | Action |
|----------|------|--------|
| Publish PyPI Package | `pypi-release.yml` | Builds and publishes `openhands-automation` to PyPI via OIDC trusted publishing |
| Tag Docker images | `tag-image.yml` | Aliases the existing `sha-<commit>` GHCR image to the release tag plus `X.Y` / `X` / `latest` (the `ghcr-build.yml` build for that commit must have run first) |

> Prerequisites (one-time, org/repo settings — not in this repo): the org secrets
> `RELEASE_APP_ID` / `RELEASE_APP_PRIVATE_KEY` (the GitHub App release-please uses —
> required so the release tag triggers the publish workflows above), and squash-merge
> configured with `PR_TITLE` as the commit title.

### SDK dependency bumps

When bumping `openhands-sdk` / `openhands-workspace` pins:
1. Update both versions in `pyproject.toml` dependencies.
2. Run `uv lock` to regenerate `uv.lock`.
3. Open a Conventional Commit PR (e.g. `fix: bump SDK to <ver>`) and squash-merge it —
   release-please handles the version bump and release.
4. After the release publishes, update `AUTOMATION_SHA` in the deploy repo:
   - `.github/workflows/deploy.yaml`
   - `.github/workflows/deploy-automation.yaml`
