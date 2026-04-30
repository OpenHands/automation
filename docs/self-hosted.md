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

---

## Quick Start with Docker (Recommended)

The easiest way to run self-hosted is using the combined Docker image that packages everything:
- Automation service (scheduler, dispatcher, API)
- Agent server (from OpenHands SDK)
- SQLite database

### 1. Create Environment File

```bash
# Copy the example file
cp .env.local.example .env.local

# Edit with your LLM configuration
cat > .env.local << 'EOF'
OPENHANDS_LLM_MODEL=anthropic/claude-sonnet-4-20250514
OPENHANDS_LLM_API_KEY=sk-ant-your-key-here
EOF
```

### 2. Run the Combined Container

```bash
docker run -d \
  --name automation \
  -p 8000:8000 \
  -v ./workspace:/workspace \
  -v ./data:/data \
  --env-file .env.local \
  ghcr.io/openhands/automation-local:latest
```

### 3. Verify the Service

```bash
# Wait for startup (about 10 seconds)
sleep 10

# Health check
curl http://localhost:8000/api/automation/health

# Open the UI
open http://localhost:8000/automations
```

### What's in the Container?

```
┌─────────────────────────────────────────────────────────────┐
│              Combined Local Container                        │
│                                                              │
│  ┌──────────────────┐    ┌──────────────────────────┐       │
│  │  Automation      │    │  Agent Server            │       │
│  │  Service :8000   │───▶│  (from SDK) :3000        │       │
│  │                  │    │                          │       │
│  │  - Scheduler     │    │  - Bash execution        │       │
│  │  - Dispatcher    │    │  - File operations       │       │
│  │  - API routes    │    │  - SDK workspace         │       │
│  └──────────────────┘    └──────────────────────────┘       │
│                                                              │
│  /workspace ──── user's project files (volume)              │
│  /data/automations.db ──── SQLite database (volume)         │
└─────────────────────────────────────────────────────────────┘
```

---

## Building the Docker Image Locally

If you want to build the combined image yourself:

### Prerequisites

- Docker 20.10+
- Git

### Build Steps

```bash
# Clone the repository
git clone https://github.com/OpenHands/automation.git
cd automation

# Build the combined local image
docker build -f containers/Dockerfile.local -t automation-local:dev .

# Run your local build
docker run -d \
  --name automation \
  -p 8000:8000 \
  -v ./workspace:/workspace \
  -v ./data:/data \
  -e OPENHANDS_LLM_MODEL=anthropic/claude-sonnet-4-20250514 \
  -e OPENHANDS_LLM_API_KEY=sk-ant-your-key-here \
  automation-local:dev
```

### Build Options

The `Dockerfile.local` uses multi-stage builds:

1. **Stage 1** (`frontend-build`): Builds the frontend SPA
2. **Stage 2** (`agent-server`): Pulls the agent server from `ghcr.io/openhands/agent-server`
3. **Stage 3**: Combines everything into the final image

### Troubleshooting Build Issues

**"Cannot pull agent-server image"**
```bash
# Try pulling the base image manually first
docker pull ghcr.io/openhands/agent-server:latest-python

# If that fails, check your Docker authentication
docker login ghcr.io
```

**"Frontend build fails"**
```bash
# Ensure the frontend directory exists
ls frontend/package.json

# If no frontend, build without it (API only mode)
# Edit Dockerfile.local to remove the frontend stage
```

---

## Manual Installation (Without Docker)

For development or when Docker isn't available.

### Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager
- A running OpenHands agent server

### 1. Clone and Install

```bash
git clone https://github.com/OpenHands/automation.git
cd automation
uv sync
```

### 2. Configure Environment

```bash
# Required: Agent server connection
export AUTOMATION_AGENT_SERVER_URL="http://localhost:3000"
export AUTOMATION_AGENT_SERVER_API_KEY="your-agent-server-key"

# Required: SQLite database
export AUTOMATION_DB_URL="sqlite+aiosqlite:///./automation.db"

# Required: Base URL for callbacks
export AUTOMATION_BASE_URL="http://localhost:8000/api/automation"

# Required: LLM configuration (for preset automations)
export OPENHANDS_LLM_MODEL="anthropic/claude-sonnet-4-20250514"
export OPENHANDS_LLM_API_KEY="sk-ant-..."
```

### 3. Start Agent Server

The agent server is part of the [OpenHands SDK](https://github.com/OpenHands/software-agent-sdk):

```bash
# Option A: Run from SDK repository
git clone https://github.com/OpenHands/software-agent-sdk.git
cd software-agent-sdk/openhands-agent-server
uv sync
SESSION_API_KEY=your-key uv run python -m openhands.agent_server --port 3000
```

```bash
# Option B: Use Docker
docker run -p 3000:3000 \
  -e SESSION_API_KEY=your-key \
  ghcr.io/openhands/agent-server:latest-python
```

### 4. Start Automation Service

```bash
uv run uvicorn automation.app:app --host 0.0.0.0 --port 8000
```

### 5. Verify

```bash
# Health check
curl http://localhost:8000/api/automation/health

# List automations
curl http://localhost:8000/api/automation/v1
```

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
