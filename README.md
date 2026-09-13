# OpenHands Automation Service

> ⚠️ **Beta**: This project is currently in beta. APIs and features may change without notice.

Scheduled and event-driven automation execution for OpenHands Cloud. This service allows users to create automations that run on a schedule (cron) or in response to events (webhooks).

## Features

- **Scheduled Automations**: Run OpenHands conversations on a cron schedule
- **Event-Driven**: Trigger automations via webhooks (e.g., GitHub events)
- **API Key Management**: Per-user API keys for secure automation access
- **Run History**: Track automation runs with status and results

## Repository boundaries

The Automation Service owns automation definitions, cron scheduling, webhooks, run history, dispatch, and sandbox lifecycle orchestration. It manages when work runs; the Agent Server and [`OpenHands/software-agent-sdk`](https://github.com/OpenHands/software-agent-sdk) execute the agent conversations and own agent/tool behavior, workspaces, events, and API endpoints.

[`OpenHands/typescript-client`](https://github.com/OpenHands/typescript-client) provides typed browser access to the Agent Server API, while [`OpenHands/OpenHands`](https://github.com/OpenHands/OpenHands) owns Agent Canvas UI and frontend integration. If a change belongs in one of those repositories, open the PR there rather than duplicating the logic here.

## Development

### Conversation execution in local or Docker workspaces

Set `AUTOMATION_AGENT_SERVER_URL`, `AUTOMATION_AGENT_SERVER_API_KEY`, and
`AUTOMATION_AGENT_PROFILE` (a saved agent profile UUID). The backend reads the
server's authoritative `conversation_runtime` and provisions a run conversation
using the same API and profile in either mode. No bundle configuration or workflow
branch changes when switching workspace kind.

Both modes supply `AUTOMATION_CONVERSATION_ID`, `AGENT_SERVER_URL`,
`SESSION_API_KEY`, and `WORKSPACE_BASE`, and use conversation-scoped upload,
bash execution, and completion verification. Local workspaces live in per-run
subdirectories of the configured workspace root. Docker workspaces use
`/workspace` and receive only the selected inner session key, never the outer
server key or shared callback key. Local mode retains its existing single-tenant
server credential boundary; a local workspace is not a security sandbox.

`AUTOMATION_CONVERSATION_MAX_CONCURRENT_RUNS` defaults to 2. Docker servers must
support runtime credential provisioning and release; bound container CPU, memory,
and PIDs in the server configuration. Completed Docker runtimes are released while
history remains; the persistent local server and its history are retained.

### Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) for dependency management
- PostgreSQL (or use testcontainers for testing)

### Setup

```bash
# Install dependencies
uv sync --group dev

# Run the service locally (requires PostgreSQL)
uv run uvicorn openhands.automation.app:app --host 0.0.0.0 --port 8000 --reload
```

### Testing

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=openhands/automation --cov-report=term-missing
```

### Code Quality

```bash
# Run pre-commit hooks
uv run pre-commit run --all-files

# Format code
uv run ruff format

# Lint code
uv run ruff check --fix

# Type check
uv run pyright
```

### Database Migrations

```bash
# Create a new migration
uv run alembic revision --autogenerate -m "description"

# Apply migrations
uv run alembic upgrade head
```

## Docker

```bash
# Build the image
docker build -t automation -f containers/Dockerfile .

# Run the container
docker run -p 8000:8000 automation
```

## Project Structure

```
openhands/
└── automation/      # Main application package (openhands.automation namespace)
    ├── app.py           # FastAPI application entry point
    ├── router.py        # API routes for CRUD operations
    ├── scheduler.py     # Background scheduler for cron jobs
    ├── dispatcher.py    # Dispatches pending runs to OpenHands
    ├── models.py        # SQLAlchemy models
    ├── schemas.py       # Pydantic schemas for API
    └── utils/           # Utility functions
migrations/          # Alembic database migrations
tests/               # Unit tests
containers/          # Docker configuration
```

## Deployment

This service is deployed via the [deploy repository](https://github.com/All-Hands-AI/deploy). Docker images are automatically built and pushed to `ghcr.io/openhands/automation` on every push to main and on tags.

For different role permissions, set `AUTOMATION_AGENT_PROFILE_OVERRIDES`
to a JSON object mapping automation UUIDs to saved agent profile UUIDs. Unmapped
automations use `AUTOMATION_AGENT_PROFILE`. This host-controlled mapping
lets deterministic jobs select a profile with no model credential or agent tools,
while implementation and review jobs select only their required tools/model.

### SDK Client Integration Dependency

The conversation backend and Agent Server execution helpers use public clients
from [software-agent-sdk #5010](https://github.com/OpenHands/software-agent-sdk/pull/5010).
This draft pins the SDK implementation by immutable Git commit so its tests and
source installation are reproducible. Replace that integration pin with the SDK
release before merging. The live factory additionally integrates the server
runtime stack; those server changes are separate from this client dependency. Workflow bundles
receive the same environment contract in local and Docker workspaces.
