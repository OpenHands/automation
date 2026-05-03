# @openhands/local

Run OpenHands locally with a single command - no Docker required.

## Quick Start

```bash
# Set your LLM credentials
export LLM_MODEL=anthropic/claude-sonnet-4-20250514
export LLM_API_KEY=sk-ant-xxxxx

# Run OpenHands
npx @openhands/local
```

Then open http://localhost:8000 in your browser.

## Installation

```bash
# Run directly with npx (recommended)
npx @openhands/local

# Or install globally
npm install -g @openhands/local
openhands-local
```

## Usage

```bash
# Basic usage with environment variables
export LLM_MODEL=anthropic/claude-sonnet-4-20250514
export LLM_API_KEY=sk-ant-xxxxx
npx @openhands/local

# Or pass credentials as arguments
npx @openhands/local --model anthropic/claude-sonnet-4-20250514 --api-key sk-ant-xxxxx

# Custom port
npx @openhands/local --port 3000

# Custom workspace directory
npx @openhands/local --workspace ./my-project

# Skip setup (if already installed)
npx @openhands/local --skip-setup
```

## Options

| Option | Description | Default |
|--------|-------------|---------|
| `--model <model>` | LLM model to use | `$LLM_MODEL` |
| `--api-key <key>` | API key for the LLM | `$LLM_API_KEY` |
| `--base-url <url>` | Custom LLM base URL | `$LLM_BASE_URL` |
| `-p, --port <port>` | Port for the main UI | `8000` |
| `-d, --data-dir <path>` | Data directory | `~/.openhands-local` |
| `-w, --workspace <path>` | Workspace directory | Current directory |
| `--skip-setup` | Skip dependency installation | `false` |
| `--verbose` | Show detailed output | `false` |

## Requirements

- **Node.js** >= 22
- **Python** >= 3.12
- **uv** - Python package manager ([install](https://docs.astral.sh/uv/))
- **tmux** - Terminal multiplexer
- **git**

### Installing requirements

```bash
# macOS
brew install node python uv tmux git

# Ubuntu/Debian
sudo apt install nodejs python3 tmux git
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (WSL2 recommended)
# Install WSL2, then follow Ubuntu instructions
```

## What's included

This package starts the full OpenHands local stack:

- **Agent Server GUI** - Main conversation interface at `/`
- **Automations UI** - Automation management at `/automations/`
- **Agent Server** - Backend API for conversations
- **Automation Service** - Backend API for automations

All services are managed automatically. Press Ctrl+C to stop everything.

## Data Storage

By default, data is stored in `~/.openhands-local/`:

```
~/.openhands-local/
├── automations.db      # SQLite database for automations
├── conversations/      # Conversation history
├── storage/           # Uploaded files
└── repos/             # Cloned repositories (agent-server-gui, automation)
```

## Supported LLM Providers

Any provider supported by LiteLLM works:

- **Anthropic**: `anthropic/claude-sonnet-4-20250514`, `anthropic/claude-3-5-haiku-20241022`
- **OpenAI**: `openai/gpt-4o`, `openai/gpt-4-turbo`
- **Google**: `google/gemini-pro`
- **Local**: Use `--base-url` to point to a local model server

## Troubleshooting

### Port already in use

```bash
# Use a different port
npx @openhands/local --port 3000
```

### Permission denied for agent-server

```bash
# Make sure uv installed packages globally
uv pip install --system openhands-agent-server
```

### tmux not found

```bash
# macOS
brew install tmux

# Ubuntu/Debian
sudo apt install tmux
```

## License

MIT
