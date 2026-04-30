# Self-Hosted Deployment Guide

This guide explains how to run the Automation Service in self-hosted mode, using a local agent server and SQLite database instead of OpenHands Cloud infrastructure.

## Overview

The automation service supports two execution modes:

| Feature | Cloud Mode (default) | Local Mode (self-hosted) |
|---------|---------------------|--------------------------|
| Execution environment | Fresh Cloud sandbox per run | Persistent local agent server |
| Database | PostgreSQL (Cloud SQL) | SQLite or PostgreSQL |
| LLM/Secrets/MCP config | OpenHands Cloud API | Optional Cloud API or manual config |
| Auth | Per-user API keys via service key | Config-level API key |
| Sandbox cleanup | Automatic after run | Not needed (persistent server) |

## Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager
- A running OpenHands agent server (see [Running Agent Server](#running-agent-server))

## Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/OpenHands/automation.git
cd automation
```

### 2. Install Dependencies

```bash
uv sync
```

### 3. Configure Environment

Create a `.env` file or export environment variables:

```bash
# Required: Agent server connection
export AUTOMATION_AGENT_SERVER_URL="http://localhost:3000"
export AUTOMATION_AGENT_SERVER_API_KEY="your-agent-server-key"

# Required: SQLite database (or use PostgreSQL)
export AUTOMATION_DB_URL="sqlite+aiosqlite:///./automation.db"

# Required: Base URL for callbacks (use your external URL)
export AUTOMATION_BASE_URL="http://localhost:8000/api/automation"

# Optional: OpenHands Cloud API for LLM/secrets/MCP (hybrid mode)
# export AUTOMATION_OPENHANDS_API_BASE_URL="https://app.all-hands.dev"
# export OPENHANDS_API_KEY="sk-oh-..."
```

### 4. Start the Service

```bash
uv run uvicorn automation.app:app --host 0.0.0.0 --port 8000
```

The service will:
- Create SQLite tables automatically on first startup
- Start the scheduler (creates PENDING runs from cron triggers)
- Start the dispatcher (executes PENDING runs on the agent server)
- Start the watchdog (marks stale RUNNING runs as FAILED)

### 5. Verify the Service

```bash
# Health check
curl http://localhost:8000/api/automation/health

# List automations (requires auth in production)
curl http://localhost:8000/api/automation/v1
```

---

## Running Agent Server

The automation service dispatches work to an OpenHands agent server. You need a running agent server accessible via HTTP.

### Option A: Run Agent Server Locally

```bash
# Clone OpenHands
git clone https://github.com/All-Hands-AI/OpenHands.git
cd OpenHands

# Start agent server (see OpenHands docs for full setup)
poetry run python -m openhands.server
```

The agent server typically runs on `http://localhost:3000`.

### Option B: Use Docker

```bash
docker run -p 3000:3000 ghcr.io/all-hands-ai/openhands:latest
```

### Option C: Use Existing Deployment

Point `AUTOMATION_AGENT_SERVER_URL` to any accessible OpenHands agent server.

---

## Configuration Reference

### Required Settings

| Environment Variable | Description | Example |
|---------------------|-------------|---------|
| `AUTOMATION_AGENT_SERVER_URL` | Agent server URL | `http://localhost:3000` |
| `AUTOMATION_AGENT_SERVER_API_KEY` | Agent server API key | `your-secret-key` |
| `AUTOMATION_DB_URL` | Database URL | `sqlite+aiosqlite:///./automation.db` |
| `AUTOMATION_BASE_URL` | Service base URL (for callbacks) | `http://localhost:8000/api/automation` |

### Optional Settings

| Environment Variable | Description | Default |
|---------------------|-------------|---------|
| `AUTOMATION_OPENHANDS_API_BASE_URL` | Cloud API for LLM/secrets/MCP | (none) |
| `OPENHANDS_API_KEY` | Cloud API key | (none) |
| `AUTOMATION_SCHEDULER_INTERVAL` | Scheduler poll interval (seconds) | `10` |
| `AUTOMATION_DISPATCHER_INTERVAL` | Dispatcher poll interval (seconds) | `5` |
| `AUTOMATION_WATCHDOG_INTERVAL` | Watchdog poll interval (seconds) | `30` |

### Database Options

**SQLite** (recommended for local/single-instance):
```bash
AUTOMATION_DB_URL="sqlite+aiosqlite:///./automation.db"
```

**PostgreSQL** (recommended for production):
```bash
AUTOMATION_DB_URL="postgresql+asyncpg://user:pass@localhost:5432/automation"
```

---

## Creating Automations

### Using the API

```bash
# Create a cron-triggered automation
curl -X POST http://localhost:8000/api/automation/v1/preset/prompt \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Daily Report",
    "prompt": "Generate a status report and save it to /tmp/report.txt",
    "trigger": {
      "type": "cron",
      "schedule": "0 9 * * *",
      "timezone": "UTC"
    }
  }'

# Manually trigger a run
curl -X POST http://localhost:8000/api/automation/v1/{automation_id}/dispatch

# List runs
curl http://localhost:8000/api/automation/v1/{automation_id}/runs
```

### Run Lifecycle

1. **PENDING** - Run created by scheduler or manual trigger
2. **RUNNING** - Dispatcher picked up run and started execution
3. **COMPLETED** - Run finished successfully (callback received)
4. **FAILED** - Run failed (error or timeout)

---

## Hybrid Mode

Hybrid mode uses a local agent server for execution but connects to OpenHands Cloud for:
- LLM configuration (`workspace.get_llm()`)
- Secrets (`workspace.get_secrets()`)
- MCP server config (`workspace.get_mcp_config()`)

To enable hybrid mode, set both agent server AND Cloud API config:

```bash
# Local execution
export AUTOMATION_AGENT_SERVER_URL="http://localhost:3000"
export AUTOMATION_AGENT_SERVER_API_KEY="local-key"

# Cloud API for LLM/secrets/MCP
export AUTOMATION_OPENHANDS_API_BASE_URL="https://app.all-hands.dev"
export OPENHANDS_API_KEY="sk-oh-your-cloud-key"
```

---

## Development

### Running Tests

```bash
# Unit tests (no external dependencies)
uv run pytest tests/ -v --ignore=tests/integration

# Integration tests (requires OPENHANDS_API_KEY)
OPENHANDS_API_KEY=sk-oh-... uv run pytest tests/integration/ -v
```

### Pre-commit Checks

```bash
uv run pre-commit run --all-files
```

### Local Development with Hot Reload

```bash
uv run uvicorn automation.app:app --reload --host 0.0.0.0 --port 8000
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Automation Service                        │
│  ┌──────────────┐  ┌─────────────┐  ┌──────────────┐        │
│  │  Scheduler   │  │  Dispatcher │  │   Watchdog   │        │
│  │ (cron→PEND)  │  │ (PEND→RUN)  │  │ (stale→FAIL) │        │
│  └──────┬───────┘  └──────┬──────┘  └──────┬───────┘        │
│         │                 │                 │                │
│         └─────────────────┼─────────────────┘                │
│                           │                                  │
│                    ┌──────▼──────┐                           │
│                    │  SQLite DB  │                           │
│                    └─────────────┘                           │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            │ HTTP (tarball upload, bash commands)
                            ▼
                    ┌───────────────┐
                    │ Agent Server  │
                    │ (persistent)  │
                    └───────────────┘
```

### Components

- **Scheduler**: Polls automations table, creates PENDING runs for due cron schedules
- **Dispatcher**: Polls PENDING runs, uploads tarball, starts entrypoint on agent server
- **Watchdog**: Scans for RUNNING runs past timeout, verifies status, marks as FAILED if needed
- **Backend Abstraction**: `LocalAgentServerBackend` handles all local-mode specifics

---

## Troubleshooting

### Common Issues

**"Agent server not reachable"**
- Verify `AUTOMATION_AGENT_SERVER_URL` is correct
- Check agent server is running: `curl http://localhost:3000/api/health`
- Check network connectivity

**"Database locked" (SQLite)**
- SQLite doesn't support concurrent writes well
- For production, use PostgreSQL
- Ensure only one instance of the service is running

**"Run stuck in RUNNING"**
- Watchdog will mark as FAILED after timeout
- Check agent server logs for errors
- Verify callback URL is reachable from agent server

**"No LLM config" (hybrid mode)**
- Verify `AUTOMATION_OPENHANDS_API_BASE_URL` and `OPENHANDS_API_KEY` are set
- Check Cloud API key is valid: `curl -H "Authorization: Bearer $OPENHANDS_API_KEY" https://app.all-hands.dev/api/v1/users/me`

### Logs

Enable debug logging:

```bash
export AUTOMATION_LOG_LEVEL="DEBUG"
```

---

## Security Considerations

- **API Key Security**: Store `AUTOMATION_AGENT_SERVER_API_KEY` securely (not in version control)
- **Database Access**: SQLite file permissions should be restricted
- **Network**: In production, use HTTPS and proper firewall rules
- **Auth**: The service authenticates requests via OpenHands Cloud API or custom middleware

---

## Migration from Cloud Mode

If migrating an existing Cloud-mode deployment to self-hosted:

1. Export your automations via the API
2. Set up local agent server and database
3. Configure local mode environment variables
4. Import automations (update tarball paths if using internal storage)
5. Update any webhook URLs to point to new service location
