# Running the Development Stack Without Docker

This document explains how to run the full OpenHands local development stack using npm and uv instead of Docker.

## Quick Start

```bash
# Single command to run everything
npm run dev

# Or with a custom port
npm run dev -- --port 12000

# Skip setup (faster restart after first run)
npm run dev:skip-setup
```

Then open:
- **Main UI**: http://localhost:8000/
- **Automations**: http://localhost:8000/automations/
- **API Docs**: http://localhost:8000/api/automation/docs

## How It Works

The development stack leverages the same patterns as [agent-server-gui](https://github.com/OpenHands/agent-server-gui)'s `npm run dev` command:

1. **Auto-installs uv** if not present (via official installer)
2. **Runs agent-server** using `uvx` (ephemeral, no global install)
3. **Runs Vite dev servers** for both frontends with hot reload
4. **Routes requests** via a unified reverse proxy

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                      http://localhost:PORT                               │
│                 (Unified Reverse Proxy)                                  │
└──────────────────────────────────────────────────────────────────────────┘
         │                      │                        │
         ▼                      ▼                        ▼
  ┌────────────┐         ┌────────────┐           ┌────────────────┐
  │    /*      │         │   /api/*   │           │ /automations/* │
  │            │         │  /sockets  │           │/api/automation │
  └─────┬──────┘         └─────┬──────┘           └───────┬────────┘
        │                      │                          │
        ▼                      ▼                          ▼
┌───────────────┐    ┌───────────────┐          ┌──────────────────┐
│ Agent Server  │    │ Agent Server  │          │ Automation FE    │
│ GUI (Vite)    │    │ (uvx)         │          │ (Vite, proxies   │
│ :PORT+30      │    │ :PORT+2       │          │  to backend)     │
└───────────────┘    └───────────────┘          │ :PORT+3          │
                                                └────────┬─────────┘
                                                         │
                                                         ▼
                                                ┌──────────────────┐
                                                │ Automation       │
                                                │ Backend (uvicorn)│
                                                │ :PORT+1          │
                                                └──────────────────┘
```

**Port allocation** (relative to `--port` or default 8000):
| Service | Port |
|---------|------|
| Reverse Proxy (main entry) | PORT |
| Automation Backend | PORT+1 |
| Agent Server | PORT+2 |
| Automation Frontend | PORT+3 |
| Agent Server GUI | PORT+30 |

## Prerequisites

The script auto-installs `uv` if missing. You only need:

| Requirement | Check Command | Install |
|-------------|---------------|---------|
| Node.js 22+ | `node --version` | [nodejs.org](https://nodejs.org/) |
| git | `git --version` | `apt install git` / `brew install git` |
| tmux | `tmux -V` | `apt install tmux` / `brew install tmux` |

## Command Line Options

```bash
npm run dev -- [options]

Options:
  -p, --port <port>      Main entry port (default: 8000)
  --gui-path <path>      Path to agent-server-gui repo (default: .dev/agent-server-gui)
  --skip-setup           Skip cloning/installing dependencies
  -v, --verbose          Show detailed output
  -h, --help             Show help
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OH_AGENT_SERVER_GIT_REF` | Git ref for agent-server SDK (default: `main`) |
| `OH_AGENT_SERVER_VERSION` | PyPI version for agent-server (overrides git ref) |
| `OH_SECRET_KEY` | Secret key for sessions |

## What the Script Does

### First Run
1. Checks prerequisites (node, git, tmux)
2. Auto-installs `uv` if missing
3. Clones `agent-server-gui` to `.dev/agent-server-gui`
4. Runs `npm ci` in both frontends
5. Runs `uv sync` for the automation backend
6. Creates state directories in `~/.openhands/dev-stack-PORT/`
7. Starts all services

### Subsequent Runs (with `--skip-setup`)
```bash
npm run dev:skip-setup
```
Skips cloning and dependency installation for faster startup.

## Data Storage

All runtime data is stored in `~/.openhands/dev-stack-PORT/`:
- `conversations/` - Agent conversation history
- `storage/` - File uploads
- `workspaces/` - Agent working directories
- `automations.db` - SQLite database

Using port-specific directories allows running multiple instances.

## Comparison: Docker vs npm/uv

| Aspect | Docker | npm/uv |
|--------|--------|--------|
| **Setup time** | ~5 min (build image) | ~3 min (first run) |
| **Hot reload** | ❌ Requires rebuild | ✅ Automatic |
| **Debugging** | Harder | ✅ Easy |
| **Resource usage** | Higher | Lower |
| **Isolation** | ✅ Full | Shared system |
| **Production parity** | ✅ Same as deploy | Different |

## When to Use Each Approach

**Use npm/uv (this script) when:**
- Active development with hot reload
- Debugging backend/frontend code
- Quick iteration on features
- Don't want to rebuild containers

**Use Docker when:**
- Running in production or staging
- Need reproducible builds
- Testing the full containerized environment
- CI/CD pipelines

## Manual Setup (Advanced)

If you prefer running services individually, see the sections below.

### Terminal 1: Agent Server

```bash
# Using uvx (ephemeral install)
uvx --from git+https://github.com/OpenHands/software-agent-sdk@main#subdirectory=openhands-agent-server \
    --with git+https://github.com/OpenHands/software-agent-sdk@main#subdirectory=openhands-tools \
    --with git+https://github.com/OpenHands/software-agent-sdk@main#subdirectory=openhands-workspace \
    agent-server --host 127.0.0.1 --port 3002
```

### Terminal 2: Agent Server GUI

```bash
cd .dev/agent-server-gui
VITE_BACKEND_HOST=127.0.0.1:3002 npm run dev:frontend
```

### Terminal 3: Automation Backend

```bash
AUTOMATION_AGENT_SERVER_URL=http://localhost:3002 \
AUTOMATION_DB_URL=sqlite+aiosqlite:///~/.openhands/dev-stack-8000/automations.db \
AUTOMATION_AUTH_DISABLED=true \
FILE_STORE=local \
LOCAL_STORAGE_PATH=~/.openhands/dev-stack-8000/storage \
uv run uvicorn automation.app:app --host 127.0.0.1 --port 8001 --reload
```

### Terminal 4: Automation Frontend

```bash
cd frontend
VITE_AUTOMATION_HOST=127.0.0.1:8001 \
VITE_OPENHANDS_HOST=127.0.0.1:3002 \
VITE_FRONTEND_PORT=3003 \
npm run dev
```

### Terminal 5: Reverse Proxy

The unified script handles this, but you could also use nginx or another proxy.

## Troubleshooting

### "uv not found"
The script auto-installs uv. If it fails, install manually:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### "tmux not found"
Install tmux - it's required by agent-server for its local runtime mode:
```bash
# Debian/Ubuntu
sudo apt install tmux

# macOS
brew install tmux
```

### Services don't start
Check if ports are already in use:
```bash
lsof -i :8000 -i :8001 -i :8002 -i :8003 -i :8030
```

### WebSocket issues
Ensure you're accessing via the proxy port (8000), not individual service ports.
