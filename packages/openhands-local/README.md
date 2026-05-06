# @openhands/local

Run OpenHands locally with a single command - no Docker required.

## Quick Start

```bash
npx @openhands/local
```

Then open http://localhost:8000 in your browser:
- **Main UI**: http://localhost:8000/
- **Automations**: http://localhost:8000/automations/
- **API Docs**: http://localhost:8000/api/automation/docs

## How It Works

This CLI leverages the same patterns as [agent-server-gui](https://github.com/OpenHands/agent-server-gui):

1. **Auto-installs uv** if not present
2. **Uses uvx** for ephemeral agent-server installation (no global install)
3. **Runs Vite dev servers** for both frontends with hot reload
4. **Routes all requests** through a unified reverse proxy

## Usage

```bash
# Basic usage
npx @openhands/local

# Custom port
npx @openhands/local --port 12000

# Custom workspace directory  
npx @openhands/local --workspace ./my-project

# Skip setup (faster restart after first run)
npx @openhands/local --skip-setup

# Development mode (uses local automation repo)
npx @openhands/local --dev
```

## Options

| Option | Description | Default |
|--------|-------------|---------|
| `-p, --port <port>` | Main entry port | `8000` |
| `-w, --workspace <path>` | Working directory | Current directory |
| `--skip-setup` | Skip cloning/installing dependencies | `false` |
| `--verbose` | Show detailed output | `false` |
| `--dev` | Use local automation repo (auto-detect) | `false` |
| `--local-automation <path>` | Path to local automation repo | - |
| `--local-gui <path>` | Path to local agent-server-gui repo | - |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OH_AGENT_SERVER_GIT_REF` | Git ref for agent-server SDK (default: `main`) |
| `OH_AGENT_SERVER_VERSION` | PyPI version for agent-server (overrides git ref) |

## Requirements

The CLI auto-installs `uv` if missing. You only need:

| Requirement | Install |
|-------------|---------|
| **Node.js** >= 22 | [nodejs.org](https://nodejs.org/) |
| **git** | `apt install git` / `brew install git` |
| **tmux** | `apt install tmux` / `brew install tmux` |

## Architecture

```
http://localhost:PORT (Reverse Proxy)
         |
         +-- /*              -> Agent Server GUI (:PORT+30)
         +-- /api/*          -> Agent Server (:PORT+2)
         +-- /sockets        -> Agent Server (:PORT+2)
         +-- /automations/*  -> Automation Frontend (:PORT+3)
         +-- /api/automation -> Automation Backend (:PORT+1)
```

## Data Storage

All data is stored in `~/.openhands/local-PORT/`:

```
~/.openhands/local-8000/
+-- automations.db      # SQLite database
+-- conversations/      # Conversation history
+-- storage/            # Uploaded files
+-- workspaces/         # Agent working directories
+-- repos/              # Cloned repositories
```

Using port-specific directories allows running multiple instances.

## Troubleshooting

### Port already in use

```bash
npx @openhands/local --port 12000
```

### tmux not found

```bash
# macOS
brew install tmux

# Ubuntu/Debian
sudo apt install tmux
```

### uv installation fails

```bash
# Manual install
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## License

MIT
