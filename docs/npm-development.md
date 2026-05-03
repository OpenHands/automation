# Running the Development Stack Without Docker

This document explains how to run the full OpenHands local development stack (Agent Server GUI + Automations Frontend + Automations Backend + Agent Server) using npm and uv instead of Docker.

## Quick Summary

**Yes, it's possible!** The current Docker setup can be replaced with npm/uv commands. Here's what each component needs:

| Component | Docker Approach | npm/uv Approach |
|-----------|-----------------|-----------------|
| Agent Server GUI | Static files served by nginx | `npm run dev` in agent-server-gui repo |
| Agent Server | Python package in container | `uv pip install openhands-agent-server && agent-server` |
| Automations Frontend | Static files served by nginx | `npm run dev` in frontend/ |
| Automations Backend | Python in virtualenv | `uv run uvicorn automation.app:app` |
| Reverse Proxy | nginx | Node.js proxy (already exists in `scripts/dev-proxy.mjs`) |

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                     http://localhost:8000                            │
│                        (Reverse Proxy)                               │
└─────────────────────────────────────────────────────────────────────┘
         │                    │                    │
         ▼                    ▼                    ▼
    ┌─────────┐          ┌─────────┐         ┌─────────────┐
    │  /      │          │/api/*   │         │/automations/│
    │         │          │/sockets │         │/api/auto..  │
    └────┬────┘          └────┬────┘         └──────┬──────┘
         │                    │                     │
         ▼                    ▼                     ▼
┌─────────────────┐   ┌─────────────────┐   ┌──────────────────┐
│ Agent Server GUI│   │  Agent Server   │   │  Automations     │
│ (npm run dev)   │   │ (agent-server)  │   │  Frontend        │
│ :3030           │   │ :3002           │   │  (npm run dev)   │
└─────────────────┘   └─────────────────┘   │  :3003           │
                                            └────────┬─────────┘
                                                     │
                                                     ▼
                                            ┌──────────────────┐
                                            │  Automations     │
                                            │  Backend         │
                                            │  (uvicorn)       │
                                            │  :8001           │
                                            └──────────────────┘
```

## Prerequisites

```bash
# Node.js 22+
node --version  # v22.12.0 or higher

# Python 3.12+
python3 --version  # 3.12 or higher

# uv (Python package manager)
uv --version

# tmux (required by agent-server for local runtime)
tmux -V

# git
git --version
```

## Step-by-Step Setup

### 1. Clone agent-server-gui (one-time)

```bash
# From the automation repo root
mkdir -p .dev
git clone --depth 1 https://github.com/OpenHands/agent-server-gui.git .dev/agent-server-gui
cd .dev/agent-server-gui && npm ci && cd ../..
```

### 2. Install agent-server and SDK

```bash
# Install the latest versions
uv pip install openhands-agent-server openhands-sdk openhands-tools openhands-workspace libtmux

# Or install from a specific branch
uv pip install \
  "openhands-agent-server @ git+https://github.com/OpenHands/software-agent-sdk.git@main#subdirectory=openhands-agent-server" \
  "openhands-sdk @ git+https://github.com/OpenHands/software-agent-sdk.git@main#subdirectory=openhands-sdk" \
  libtmux
```

### 3. Install frontend dependencies

```bash
cd frontend && npm ci && cd ..
```

### 4. Sync automation backend dependencies

```bash
uv sync
```

### 5. Create data directories

```bash
mkdir -p .dev/data/{storage,conversations} .dev/workspace
```

## Running the Stack

You'll need **5 terminal windows** (or use a process manager):

### Terminal 1: Agent Server

```bash
export OH_CONVERSATIONS_PATH=$PWD/.dev/data/conversations
cd .dev/workspace
agent-server --host 127.0.0.1 --port 3002
```

### Terminal 2: Agent Server GUI

```bash
cd .dev/agent-server-gui
export VITE_BACKEND_HOST=127.0.0.1:3002
npm run dev:frontend
# Runs on port 3030
```

### Terminal 3: Automations Backend

```bash
export AUTOMATION_AGENT_SERVER_URL=http://localhost:3002
export AUTOMATION_DB_URL=sqlite+aiosqlite:///.dev/data/automations.db
export AUTOMATION_BASE_URL=http://localhost:8000
export AUTOMATION_WORKSPACE_BASE=$PWD/.dev/workspace
export AUTOMATION_AUTH_DISABLED=true
export FILE_STORE=local
export LOCAL_STORAGE_PATH=$PWD/.dev/data/storage

uv run uvicorn automation.app:app --host 127.0.0.1 --port 8001 --reload
```

### Terminal 4: Automations Frontend

```bash
cd frontend
export VITE_AUTOMATION_HOST=127.0.0.1:8001
export VITE_OPENHANDS_HOST=127.0.0.1:3002
export VITE_FRONTEND_PORT=3003
npm run dev
```

### Terminal 5: Reverse Proxy

```bash
node scripts/dev-proxy.mjs 8000 3030 3003
# Routes:
#   /automations/*, /api/automation/*  → :3003 (automations frontend, which proxies to backend)
#   /*                                 → :3030 (agent-server-gui)
```

## Single-Command Approach

You can use `concurrently` to run everything in one terminal:

```bash
npm install -g concurrently

concurrently --names "agent-srv,agent-gui,auto-be,auto-fe,proxy" \
  "cd .dev/workspace && OH_CONVERSATIONS_PATH=$PWD/../data/conversations agent-server --port 3002" \
  "cd .dev/agent-server-gui && VITE_BACKEND_HOST=127.0.0.1:3002 npm run dev:frontend" \
  "AUTOMATION_AGENT_SERVER_URL=http://localhost:3002 AUTOMATION_DB_URL=sqlite+aiosqlite:///.dev/data/automations.db AUTOMATION_AUTH_DISABLED=true FILE_STORE=local LOCAL_STORAGE_PATH=$PWD/.dev/data/storage uv run uvicorn automation.app:app --port 8001" \
  "cd frontend && VITE_AUTOMATION_HOST=127.0.0.1:8001 npm run dev" \
  "sleep 5 && node scripts/dev-proxy.mjs 8000 3030 3003"
```

Or use the included script:

```bash
node scripts/npm-dev-stack.mjs
```

## Updating the Enhanced Proxy

The existing `frontend/scripts/dev-proxy.mjs` needs to be updated to handle all routes. A complete reverse proxy is provided in `scripts/npm-dev-stack.mjs`.

## Comparison: Docker vs npm/uv

| Aspect | Docker | npm/uv |
|--------|--------|--------|
| **Setup time** | ~5 min (build image) | ~3 min (install deps) |
| **Hot reload** | ❌ Requires rebuild | ✅ Automatic |
| **Debugging** | Harder (attach to container) | ✅ Easy |
| **Resource usage** | Higher (container overhead) | Lower |
| **Isolation** | ✅ Full isolation | Shared system |
| **Production parity** | ✅ Same as deploy | Different |
| **CI reproducibility** | ✅ Deterministic | Depends on system |

## Limitations

1. **No isolation**: All services share the same system Python and Node.js
2. **tmux dependency**: The agent-server requires tmux for its local runtime mode
3. **Manual process management**: Need to manage 5 separate processes (or use concurrently/script)
4. **No nginx features**: WebSocket upgrade handling is simpler; no gzip, caching headers, etc.

## When to Use Each Approach

**Use Docker when:**
- Running in production or staging
- Need reproducible builds
- Testing the full containerized environment
- CI/CD pipelines

**Use npm/uv when:**
- Active development with hot reload
- Debugging backend/frontend code
- Quick iteration on features
- Don't want to rebuild containers

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_MODEL` | - | LLM model (e.g., `anthropic/claude-sonnet-4-20250514`) |
| `LLM_API_KEY` | - | API key for the LLM |
| `AUTOMATION_AUTH_DISABLED` | `true` | Bypass auth for local dev |
| `AUTOMATION_DB_URL` | sqlite path | Database connection string |
| `OH_CONVERSATIONS_PATH` | - | Where agent-server stores conversations |
