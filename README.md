# OpenHands Automation Service

Scheduled and event-driven automation execution for OpenHands Cloud. This service allows users to create automations that run on a schedule (cron) or in response to events (webhooks).

## Features

- **Scheduled Automations**: Run OpenHands conversations on a cron schedule
- **Event-Driven**: Trigger automations via webhooks (e.g., GitHub events)
- **API Key Management**: Per-user API keys for secure automation access
- **Run History**: Track automation runs with status and results

## Development

### Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) for dependency management
- PostgreSQL (or use testcontainers for testing)

### Setup

```bash
# Install dependencies
uv sync --group dev

# Run the service locally (requires PostgreSQL)
uv run uvicorn automation.app:app --host 0.0.0.0 --port 8000 --reload
```

### Testing

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=automation --cov-report=term-missing
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
automation/
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
