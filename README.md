# OpenHands Automations Service

Scheduled and event-driven automation execution for OpenHands Cloud.

## Overview

This service enables users to define automations that execute SDK code on a schedule
or in response to events. Each automation is defined by:

- **Triggers**: When to run (cron schedule, webhooks — MVP supports cron only)
- **SDK code tarball path**: What to run (S3/GCS path to a tarball of SDK code)

The service is intentionally decoupled from plugin/skill schemas — it only knows
about SDK code tarballs and when to trigger them.

## Architecture

```
┌─────────────────────────────┐
│  Automations Service        │
│  (this repo)                │
│                             │
│  ┌───────────────────────┐  │
│  │ CRUD API (FastAPI)    │  │  POST/GET/PATCH/DELETE /api/v1/automations
│  └───────────────────────┘  │
│  ┌───────────────────────┐  │
│  │ Cron Scheduler        │  │  Background task: evaluate crons every 60s
│  └───────────┬───────────┘  │
│  ┌───────────┴───────────┐  │
│  │ Own PostgreSQL DB     │  │  automations, automation_runs tables
│  └───────────────────────┘  │
└──────────────┬──────────────┘
               │ POST /api/v1/app-conversations
               ▼
┌──────────────────────────────┐
│  OpenHands SaaS Server       │  (existing V1 API)
└──────────────────────────────┘
```

## Quick Start

```bash
# Install dependencies
make build

# Run locally (SQLite, no PostgreSQL needed)
AUTOMATION_SQLITE_PATH=automations.db \
AUTOMATION_ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
AUTOMATION_OPENHANDS_API_BASE_URL=https://app.openhands.ai \
uvicorn automation.app:app --reload

# Run tests
make test

# Lint & format
make lint
make format
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/automations` | Create automation |
| `GET` | `/api/v1/automations` | List automations |
| `GET` | `/api/v1/automations/{id}` | Get automation |
| `PATCH` | `/api/v1/automations/{id}` | Update automation |
| `DELETE` | `/api/v1/automations/{id}` | Delete automation |
| `POST` | `/api/v1/automations/{id}/trigger` | Manual trigger |
| `GET` | `/api/v1/automations/{id}/runs` | List runs |
| `GET` | `/health` | Health check |
| `GET` | `/ready` | Readiness check |

## Authentication

All API calls require an OpenHands API key in the `Authorization: Bearer <key>` header.
The service validates the key against the OpenHands V1 API (`/api/v1/user`).

## Configuration

All config via environment variables with `AUTOMATION_` prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTOMATION_DB_HOST` | `localhost` | PostgreSQL host |
| `AUTOMATION_DB_PORT` | `5432` | PostgreSQL port |
| `AUTOMATION_DB_NAME` | `automations` | Database name |
| `AUTOMATION_DB_USER` | `postgres` | Database user |
| `AUTOMATION_DB_PASS` | `postgres` | Database password |
| `AUTOMATION_SQLITE_PATH` | (none) | Set to use SQLite instead of PostgreSQL |
| `AUTOMATION_ENCRYPTION_KEY` | (required) | Fernet key for API key encryption |
| `AUTOMATION_OPENHANDS_API_BASE_URL` | `https://app.openhands.ai` | SaaS API URL |
| `AUTOMATION_SCHEDULER_INTERVAL_SECONDS` | `60` | Cron evaluation interval |

## Project Structure

```
automation/
├── app.py          # FastAPI application + lifespan
├── config.py       # Settings from env vars
├── db.py           # Database engine + session management
├── models.py       # SQLAlchemy ORM models
├── schemas.py      # Pydantic request/response schemas
├── router.py       # CRUD API + trigger + runs endpoints
├── scheduler.py    # Background cron evaluator
├── executor.py     # V1 API caller for running automations
├── auth.py         # API key validation against OH SaaS
└── encryption.py   # Fernet encryption for stored keys
chart/              # Helm chart for K8s deployment
migrations/         # Alembic DB migrations
tests/              # Test suite
```

## Deployment

Helm chart in `chart/`. Designed to be added to the OpenHands-Cloud chart repository.

```bash
helm install automations ./chart -f chart/values.yaml
```

## Links

- [ADR-0002: Automations Service Architecture](https://github.com/OpenHands/architecture/pull/11)
- [RFC: OpenHands Automations](https://github.com/OpenHands/OpenHands/issues/13275)
