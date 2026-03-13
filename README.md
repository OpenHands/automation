# OpenHands Automation

OpenHands automation service.

## Development Setup

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Set up the development environment
make build

# Run tests
make test

# Format code
make format

# Lint code
make lint
```

## Project Structure

```
automation/             # Main package
tests/
├── conftest.py         # Shared test fixtures
└── unit/               # Unit tests
```
