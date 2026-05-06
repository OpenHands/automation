#!/usr/bin/env node
/**
 * Unified Development Stack Runner
 * 
 * Runs the full OpenHands local development stack by leveraging the agent-server-gui's
 * `npm run dev` command (which handles agent-server + GUI + uv installation) and adding
 * the automation backend + frontend on top.
 * 
 * Architecture:
 *   ┌──────────────────────────────────────────────────────────────────────────┐
 *   │                      http://localhost:PORT                               │
 *   │                 (Unified Vite Dev Server with Proxy)                     │
 *   └──────────────────────────────────────────────────────────────────────────┘
 *            │                      │                        │
 *            ▼                      ▼                        ▼
 *     ┌────────────┐         ┌────────────┐           ┌────────────────┐
 *     │    /*      │         │   /api/*   │           │ /automations/* │
 *     │            │         │  /sockets  │           │/api/automation │
 *     └─────┬──────┘         └─────┬──────┘           └───────┬────────┘
 *           │                      │                          │
 *           ▼                      ▼                          ▼
 *   ┌───────────────┐    ┌───────────────┐          ┌──────────────────┐
 *   │ Agent Server  │    │ Agent Server  │          │ Automation FE    │
 *   │ GUI (Vite)    │    │ (Python)      │          │ (Vite, proxies   │
 *   │ :3030         │    │ :3002         │          │  to backend)     │
 *   └───────────────┘    └───────────────┘          │ :3003            │
 *        ▲                     ▲                    └────────┬─────────┘
 *        │                     │                             │
 *        └─────────────────────┴──────────────────────┐      │
 *                                                     │      ▼
 *                       agent-server-gui              │  ┌──────────────┐
 *                       `npm run dev`                 │  │ Automation   │
 *                       (handles both)                │  │ Backend      │
 *                                                     │  │ (uvicorn)    │
 *                                                     │  │ :8001        │
 *                                                     │  └──────────────┘
 * 
 * Key Design Decisions:
 *   1. agent-server-gui's `npm run dev` (dev-safe.mjs) installs uv and starts
 *      both agent-server and the GUI - we inherit this behavior
 *   2. We add automation backend (via uv) and automation frontend (via npm)
 *   3. A unified proxy routes requests to the appropriate service
 *   4. Automation frontend is served at /automations/ subpath via Vite's base config
 * 
 * Usage:
 *   node scripts/npm-dev-stack.mjs
 *   node scripts/npm-dev-stack.mjs --port 12000
 * 
 * Environment variables:
 *   - PORT: Main entry port (default: 8000)
 *   - AGENT_SERVER_GUI_PATH: Path to agent-server-gui repo (default: .dev/agent-server-gui)
 *   - OH_AGENT_SERVER_GIT_REF: Git ref for agent-server (passed to dev-safe.mjs)
 * 
 * Access points:
 *   - http://localhost:PORT/              - Agent Server GUI
 *   - http://localhost:PORT/automations/  - Automations UI
 *   - http://localhost:PORT/api/          - Agent Server API
 *   - http://localhost:PORT/api/automation/ - Automations API
 */

import { spawn, execSync } from 'node:child_process';
import { createServer, request as httpRequest } from 'node:http';
import { existsSync, mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { homedir } from 'node:os';
import { setTimeout as delay } from 'node:timers/promises';

const __dirname = dirname(fileURLToPath(import.meta.url));
const projectRoot = resolve(__dirname, '..');

// ═══════════════════════════════════════════════════════════════════════════
// Configuration
// ═══════════════════════════════════════════════════════════════════════════

function parseArgs() {
  const args = process.argv.slice(2);
  const config = {
    port: 8000,
    guiPath: null,
    skipSetup: false,
    verbose: false,
  };
  
  for (let i = 0; i < args.length; i++) {
    switch (args[i]) {
      case '-p':
      case '--port':
        config.port = parseInt(args[++i], 10);
        break;
      case '--gui-path':
        config.guiPath = resolve(args[++i]);
        break;
      case '--skip-setup':
        config.skipSetup = true;
        break;
      case '-v':
      case '--verbose':
        config.verbose = true;
        break;
      case '-h':
      case '--help':
        showHelp();
        process.exit(0);
    }
  }
  
  return config;
}

function showHelp() {
  console.log(`
Unified Development Stack Runner

Runs the full OpenHands local stack by leveraging agent-server-gui's npm scripts.

USAGE:
  node scripts/npm-dev-stack.mjs [options]

OPTIONS:
  -p, --port <port>      Main entry port (default: 8000)
  --gui-path <path>      Path to agent-server-gui repo (default: .dev/agent-server-gui)
  --skip-setup           Skip cloning/installing dependencies
  -v, --verbose          Show detailed output
  -h, --help             Show this help

ENVIRONMENT VARIABLES:
  OH_AGENT_SERVER_GIT_REF   Git ref for agent-server SDK
  OH_AGENT_SERVER_VERSION   PyPI version for agent-server
  OH_SECRET_KEY             Secret key for sessions

ACCESS POINTS:
  Main UI:      http://localhost:PORT/
  Automations:  http://localhost:PORT/automations/
  API Docs:     http://localhost:PORT/api/automation/docs
`);
}

function buildConfig(args) {
  // Ports - staggered to avoid conflicts
  const basePort = args.port;
  
  return {
    // Main entry port
    proxyPort: basePort,
    
    // Service ports (internal, behind proxy)
    agentServerPort: basePort + 2,   // e.g., 8002 for agent-server API
    agentGuiPort: basePort + 30,     // e.g., 8030 for GUI Vite dev server
    autoBackendPort: basePort + 1,   // e.g., 8001 for automation backend
    autoFrontendPort: basePort + 3,  // e.g., 8003 for automation frontend
    
    // Paths
    guiPath: args.guiPath || join(projectRoot, '.dev', 'agent-server-gui'),
    autoPath: projectRoot,
    
    // Data directories (isolated per port to allow multiple instances)
    stateDir: join(homedir(), '.openhands', `dev-stack-${basePort}`),
    
    // Flags
    skipSetup: args.skipSetup,
    verbose: args.verbose,
  };
}

// ═══════════════════════════════════════════════════════════════════════════
// Terminal Styling
// ═══════════════════════════════════════════════════════════════════════════

const c = {
  reset: '\x1b[0m',
  bold: '\x1b[1m',
  dim: '\x1b[2m',
  red: '\x1b[31m',
  green: '\x1b[32m',
  yellow: '\x1b[33m',
  blue: '\x1b[34m',
  magenta: '\x1b[35m',
  cyan: '\x1b[36m',
};

function log(message, color = c.reset) {
  console.log(`${color}${message}${c.reset}`);
}

function logService(name, message, color = c.reset) {
  const ts = new Date().toISOString().split('T')[1].split('.')[0];
  console.log(`${c.dim}${ts}${c.reset} ${color}[${name}]${c.reset} ${message}`);
}

function logStep(step, message) {
  console.log(`${c.cyan}[${step}]${c.reset} ${message}`);
}

function logSuccess(message) {
  console.log(`${c.green}✓${c.reset} ${message}`);
}

function logError(message) {
  console.error(`${c.red}✗${c.reset} ${message}`);
}

// ═══════════════════════════════════════════════════════════════════════════
// Prerequisites & Setup
// ═══════════════════════════════════════════════════════════════════════════

function commandExists(cmd) {
  try {
    execSync(`command -v ${cmd}`, { stdio: 'pipe' });
    return true;
  } catch {
    return false;
  }
}

/**
 * Ensure uv is installed - auto-install from official source if missing.
 * This mirrors agent-server-gui's dev-safe.mjs behavior.
 */
function ensureUv() {
  // Add ~/.local/bin to PATH (where uv installs by default)
  const uvBinPath = join(homedir(), '.local', 'bin');
  if (!process.env.PATH?.includes(uvBinPath)) {
    process.env.PATH = `${uvBinPath}:${process.env.PATH}`;
  }
  
  if (commandExists('uv')) {
    const version = execSync('uv --version', { encoding: 'utf-8' }).trim();
    log(`  Found ${version}`, c.dim);
    return true;
  }
  
  log('  Installing uv via official installer...', c.yellow);
  try {
    execSync('curl -LsSf https://astral.sh/uv/install.sh | sh', {
      stdio: 'inherit',
      shell: true,
    });
    
    if (commandExists('uv')) {
      const version = execSync('uv --version', { encoding: 'utf-8' }).trim();
      log(`  Installed ${version}`, c.green);
      return true;
    }
  } catch (err) {
    logError(`Failed to install uv: ${err.message}`);
    return false;
  }
  
  return false;
}

function checkPrerequisites() {
  logStep('1/4', 'Checking prerequisites...');
  
  const checks = [
    { cmd: 'node', name: 'Node.js' },
    { cmd: 'git', name: 'git' },
    { cmd: 'tmux', name: 'tmux', hint: 'apt install tmux / brew install tmux' },
  ];
  
  const missing = checks.filter(check => !commandExists(check.cmd));
  
  if (missing.length > 0) {
    logError('Missing prerequisites:');
    for (const item of missing) {
      console.log(`  - ${item.name}${item.hint ? ` (install: ${item.hint})` : ''}`);
    }
    process.exit(1);
  }
  
  // Node.js version check
  const nodeVersion = process.version.slice(1);
  const [major] = nodeVersion.split('.').map(Number);
  if (major < 22) {
    logError(`Node.js 22+ required (found ${nodeVersion})`);
    process.exit(1);
  }
  
  // uv - auto-install if missing
  if (!ensureUv()) {
    logError('uv is required. Install from https://docs.astral.sh/uv/');
    process.exit(1);
  }
  
  logSuccess('All prerequisites met');
}

function ensureDirectories(config) {
  const dirs = [
    config.stateDir,
    join(config.stateDir, 'conversations'),
    join(config.stateDir, 'storage'),
    join(config.stateDir, 'workspaces'),
  ];
  
  for (const dir of dirs) {
    if (!existsSync(dir)) {
      mkdirSync(dir, { recursive: true });
    }
  }
}

async function setupAgentServerGui(config) {
  logStep('2/4', 'Setting up agent-server-gui...');
  
  if (!existsSync(config.guiPath)) {
    log('  Cloning repository...');
    mkdirSync(dirname(config.guiPath), { recursive: true });
    execSync(
      `git clone --depth 1 https://github.com/OpenHands/agent-server-gui.git "${config.guiPath}"`,
      { stdio: 'inherit' }
    );
  }
  
  log('  Installing npm dependencies...');
  execSync('npm ci', { cwd: config.guiPath, stdio: 'inherit' });
  
  logSuccess('agent-server-gui ready');
}

async function setupAutomation(config) {
  logStep('3/4', 'Setting up automation...');
  
  // Sync Python dependencies
  log('  Syncing Python dependencies...');
  execSync('uv sync', { cwd: config.autoPath, stdio: 'inherit' });
  
  // Install frontend dependencies
  const frontendDir = join(config.autoPath, 'frontend');
  if (!existsSync(join(frontendDir, 'node_modules'))) {
    log('  Installing frontend npm dependencies...');
    execSync('npm ci', { cwd: frontendDir, stdio: 'inherit' });
  }
  
  logSuccess('Automation ready');
}

// ═══════════════════════════════════════════════════════════════════════════
// Process Management
// ═══════════════════════════════════════════════════════════════════════════

const processes = new Map();

function spawnService(name, command, args, options = {}) {
  const proc = spawn(command, args, {
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, ...options.env },
    cwd: options.cwd,
    shell: true,
  });
  
  const color = options.color || c.reset;
  
  proc.stdout.on('data', data => {
    data.toString().split('\n').filter(Boolean).forEach(line => {
      logService(name, line.trim(), color);
    });
  });
  
  proc.stderr.on('data', data => {
    data.toString().split('\n').filter(Boolean).forEach(line => {
      logService(name, line.trim(), c.yellow);
    });
  });
  
  proc.on('exit', code => {
    if (code !== 0 && code !== null) {
      logService(name, `Exited with code ${code}`, c.red);
    }
    processes.delete(name);
  });
  
  processes.set(name, proc);
  return proc;
}

/**
 * Wait for a service to become healthy at the given URL.
 */
async function waitForService(name, url, timeoutMs = 30000) {
  const start = Date.now();
  
  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetch(url);
      if (res.ok) {
        logService(name, `Ready at ${url}`, c.green);
        return true;
      }
    } catch {
      // Keep trying
    }
    await delay(500);
  }
  
  logService(name, `Timeout waiting for ${url}`, c.red);
  return false;
}

// ═══════════════════════════════════════════════════════════════════════════
// Service Starters
// ═══════════════════════════════════════════════════════════════════════════

/**
 * Start agent-server using uvx (matches dev-safe.mjs behavior).
 * This runs the agent-server without permanent installation.
 */
function startAgentServer(config) {
  logService('agent-server', `Starting on port ${config.agentServerPort}...`, c.blue);
  
  // Build uvx command - same logic as dev-safe.mjs
  const gitRef = process.env.OH_AGENT_SERVER_GIT_REF || 'main';
  const version = process.env.OH_AGENT_SERVER_VERSION;
  
  let uvxArgs;
  
  if (version) {
    // Use specific PyPI version
    uvxArgs = [
      '--with', 'openhands-tools',
      '--with', 'openhands-workspace',
      `openhands-agent-server==${version}`,
    ];
  } else {
    // Use git ref (default: main)
    const baseGitUrl = `git+https://github.com/OpenHands/software-agent-sdk@${gitRef}`;
    uvxArgs = [
      '--from', `${baseGitUrl}#subdirectory=openhands-agent-server`,
      '--with', `${baseGitUrl}#subdirectory=openhands-tools`,
      '--with', `${baseGitUrl}#subdirectory=openhands-workspace`,
      'agent-server',
    ];
  }
  
  spawnService('agent-server', 'uvx', [
    ...uvxArgs,
    '--host', '127.0.0.1',
    '--port', config.agentServerPort.toString(),
  ], {
    cwd: join(config.stateDir, 'workspaces'),
    env: {
      TMUX_TMPDIR: join(config.stateDir, 'tmux'),
      OH_CONVERSATIONS_PATH: join(config.stateDir, 'conversations'),
      OH_BASH_EVENTS_DIR: join(config.stateDir, 'bash_events'),
      OPENHANDS_SUPPRESS_BANNER: '1',
    },
    color: c.blue,
  });
}

/**
 * Start agent-server-gui frontend (Vite dev server).
 */
function startAgentServerGui(config) {
  logService('agent-gui', `Starting on port ${config.agentGuiPort}...`, c.magenta);
  
  spawnService('agent-gui', 'npm', ['run', 'dev:frontend'], {
    cwd: config.guiPath,
    env: {
      VITE_BACKEND_HOST: `127.0.0.1:${config.agentServerPort}`,
      VITE_FRONTEND_PORT: config.agentGuiPort.toString(),
    },
    color: c.magenta,
  });
}

/**
 * Start automation backend (FastAPI via uvicorn).
 */
function startAutomationBackend(config) {
  logService('auto-be', `Starting on port ${config.autoBackendPort}...`, c.green);
  
  spawnService('auto-be', 'uv', [
    'run', 'uvicorn', 'automation.app:app',
    '--host', '127.0.0.1',
    '--port', config.autoBackendPort.toString(),
    '--reload',
  ], {
    cwd: config.autoPath,
    env: {
      AUTOMATION_AGENT_SERVER_URL: `http://localhost:${config.agentServerPort}`,
      AUTOMATION_DB_URL: `sqlite+aiosqlite:///${join(config.stateDir, 'automations.db')}`,
      AUTOMATION_BASE_URL: `http://localhost:${config.proxyPort}`,
      AUTOMATION_WORKSPACE_BASE: join(config.stateDir, 'workspaces'),
      AUTOMATION_AUTH_DISABLED: 'true',
      FILE_STORE: 'local',
      LOCAL_STORAGE_PATH: join(config.stateDir, 'storage'),
      OPENHANDS_SUPPRESS_BANNER: '1',
    },
    color: c.green,
  });
}

/**
 * Start automation frontend (Vite dev server at /automations/ subpath).
 */
function startAutomationFrontend(config) {
  logService('auto-fe', `Starting on port ${config.autoFrontendPort}...`, c.cyan);
  
  spawnService('auto-fe', 'npm', ['run', 'dev'], {
    cwd: join(config.autoPath, 'frontend'),
    env: {
      // The automation backend for /api/automation/* calls
      VITE_AUTOMATION_HOST: `127.0.0.1:${config.autoBackendPort}`,
      // The agent server for /api/* calls (settings, etc.)
      VITE_OPENHANDS_HOST: `127.0.0.1:${config.agentServerPort}`,
      // The port for this Vite dev server
      VITE_FRONTEND_PORT: config.autoFrontendPort.toString(),
    },
    color: c.cyan,
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// Reverse Proxy
// ═══════════════════════════════════════════════════════════════════════════

function startReverseProxy(config) {
  logService('proxy', `Starting on port ${config.proxyPort}...`, c.yellow);
  
  /**
   * Route requests to the appropriate backend service.
   * Order matters: more specific routes first.
   */
  function routeToPort(url) {
    // Automation routes -> automation frontend (which proxies /api/automation to backend)
    if (url.startsWith('/automations') || url.startsWith('/api/automation')) {
      return config.autoFrontendPort;
    }
    
    // Agent server API and WebSockets
    if (url.startsWith('/api/') || url.startsWith('/sockets') ||
        url === '/server_info' || url === '/health' ||
        url === '/ready' || url === '/alive') {
      return config.agentServerPort;
    }
    
    // Everything else -> agent-server-gui
    return config.agentGuiPort;
  }
  
  function proxy(req, res, targetPort) {
    const options = {
      hostname: 'localhost',
      port: targetPort,
      path: req.url,
      method: req.method,
      headers: req.headers,
    };
    
    const proxyReq = httpRequest(options, proxyRes => {
      res.writeHead(proxyRes.statusCode, proxyRes.headers);
      proxyRes.pipe(res, { end: true });
    });
    
    proxyReq.on('error', err => {
      res.writeHead(502);
      res.end(`Proxy error: ${err.message}`);
    });
    
    req.pipe(proxyReq, { end: true });
  }
  
  const server = createServer((req, res) => {
    proxy(req, res, routeToPort(req.url));
  });
  
  // Handle WebSocket upgrades
  server.on('upgrade', (req, socket, head) => {
    const targetPort = routeToPort(req.url);
    
    const options = {
      hostname: 'localhost',
      port: targetPort,
      path: req.url,
      method: req.method,
      headers: req.headers,
    };
    
    const proxyReq = httpRequest(options);
    
    proxyReq.on('upgrade', (proxyRes, proxySocket, proxyHead) => {
      socket.write(
        `HTTP/${proxyRes.httpVersion} ${proxyRes.statusCode} ${proxyRes.statusMessage}\r\n`
      );
      for (let i = 0; i < proxyRes.rawHeaders.length; i += 2) {
        socket.write(`${proxyRes.rawHeaders[i]}: ${proxyRes.rawHeaders[i + 1]}\r\n`);
      }
      socket.write('\r\n');
      
      if (proxyHead.length > 0) socket.write(proxyHead);
      
      proxySocket.pipe(socket, { end: true });
      socket.pipe(proxySocket, { end: true });
    });
    
    proxyReq.on('error', () => socket.destroy());
    proxyReq.end();
  });
  
  server.listen(config.proxyPort, () => {
    console.log('');
    console.log(`${c.green}${c.bold}╔══════════════════════════════════════════════════════════════╗${c.reset}`);
    console.log(`${c.green}${c.bold}║${c.reset}  ${c.bold}OpenHands Development Stack${c.reset}                                ${c.green}${c.bold}║${c.reset}`);
    console.log(`${c.green}${c.bold}╠══════════════════════════════════════════════════════════════╣${c.reset}`);
    console.log(`${c.green}${c.bold}║${c.reset}                                                              ${c.green}${c.bold}║${c.reset}`);
    console.log(`${c.green}${c.bold}║${c.reset}  Main UI:      ${c.cyan}http://localhost:${config.proxyPort}/${c.reset}                       ${c.green}${c.bold}║${c.reset}`);
    console.log(`${c.green}${c.bold}║${c.reset}  Automations:  ${c.cyan}http://localhost:${config.proxyPort}/automations/${c.reset}           ${c.green}${c.bold}║${c.reset}`);
    console.log(`${c.green}${c.bold}║${c.reset}  API Docs:     ${c.cyan}http://localhost:${config.proxyPort}/api/automation/docs${c.reset}    ${c.green}${c.bold}║${c.reset}`);
    console.log(`${c.green}${c.bold}║${c.reset}                                                              ${c.green}${c.bold}║${c.reset}`);
    console.log(`${c.green}${c.bold}╚══════════════════════════════════════════════════════════════╝${c.reset}`);
    console.log('');
    console.log(`${c.dim}State directory: ${config.stateDir}${c.reset}`);
    console.log(`${c.dim}Press Ctrl+C to stop${c.reset}`);
    console.log('');
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// Main
// ═══════════════════════════════════════════════════════════════════════════

function shutdown() {
  console.log('');
  log('Shutting down...', c.yellow);
  
  for (const [name, proc] of processes) {
    logService(name, 'Stopping...', c.dim);
    proc.kill('SIGTERM');
  }
  
  setTimeout(() => {
    for (const [name, proc] of processes) {
      if (!proc.killed) {
        proc.kill('SIGKILL');
      }
    }
    process.exit(0);
  }, 3000);
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);

async function main() {
  const args = parseArgs();
  const config = buildConfig(args);
  
  console.log('');
  console.log(`${c.cyan}${c.bold}OpenHands Development Stack${c.reset}`);
  console.log('');
  
  // Setup phase
  checkPrerequisites();
  ensureDirectories(config);
  
  if (!config.skipSetup) {
    await setupAgentServerGui(config);
    await setupAutomation(config);
  }
  
  // Start services phase
  logStep('4/4', 'Starting services...');
  
  // 1. Start agent-server first (other services depend on it)
  startAgentServer(config);
  await waitForService('agent-server', `http://localhost:${config.agentServerPort}/server_info`);
  
  // 2. Start the other services in parallel
  startAgentServerGui(config);
  startAutomationBackend(config);
  startAutomationFrontend(config);
  
  // 3. Wait a moment for Vite dev servers to start
  await delay(3000);
  
  // 4. Start the reverse proxy
  startReverseProxy(config);
}

main().catch(err => {
  logError(`Fatal error: ${err.message}`);
  if (err.stack) {
    console.error(c.dim + err.stack + c.reset);
  }
  process.exit(1);
});
