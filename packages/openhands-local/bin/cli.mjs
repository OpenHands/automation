#!/usr/bin/env node
/**
 * @openhands/local - Run OpenHands locally with a single command
 * 
 * This CLI leverages the same patterns as agent-server-gui's `npm run dev`:
 * - Auto-installs uv if not present
 * - Uses uvx for ephemeral agent-server installation  
 * - Runs Vite dev servers for frontends
 * - Provides a unified proxy for all services
 * 
 * Usage:
 *   npx @openhands/local
 *   npx @openhands/local --port 12000
 *   npx @openhands/local --help
 */

import { spawn, execSync } from 'node:child_process';
import { createServer, request as httpRequest } from 'node:http';
import { existsSync, mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { homedir } from 'node:os';
import { setTimeout as delay } from 'node:timers/promises';

const __dirname = dirname(fileURLToPath(import.meta.url));
const VERSION = '0.1.0';

// ═══════════════════════════════════════════════════════════════════════════
// Configuration
// ═══════════════════════════════════════════════════════════════════════════

function parseArgs() {
  const args = process.argv.slice(2);
  const config = {
    help: false,
    version: false,
    port: 8000,
    workspaceDir: process.cwd(),
    skipSetup: false,
    verbose: false,
    dev: false,
    localAutomation: null,
    localGui: null,
  };
  
  for (let i = 0; i < args.length; i++) {
    switch (args[i]) {
      case '-h':
      case '--help':
        config.help = true;
        break;
      case '-v':
      case '--version':
        config.version = true;
        break;
      case '-p':
      case '--port':
        config.port = parseInt(args[++i], 10);
        break;
      case '-w':
      case '--workspace':
        config.workspaceDir = resolve(args[++i]);
        break;
      case '--skip-setup':
        config.skipSetup = true;
        break;
      case '--verbose':
        config.verbose = true;
        break;
      case '--dev':
        config.dev = true;
        break;
      case '--local-automation':
        config.localAutomation = resolve(args[++i]);
        break;
      case '--local-gui':
        config.localGui = resolve(args[++i]);
        break;
    }
  }
  
  // In dev mode, auto-detect local repos
  if (config.dev) {
    const packageRoot = resolve(__dirname, '..');
    const autoRoot = resolve(packageRoot, '../..');
    
    if (!config.localAutomation && existsSync(join(autoRoot, 'automation', 'app.py'))) {
      config.localAutomation = autoRoot;
    }
  }
  
  return config;
}

function buildConfig(args) {
  const basePort = args.port;
  const stateDir = join(homedir(), '.openhands', `local-${basePort}`);
  
  return {
    proxyPort: basePort,
    agentServerPort: basePort + 2,
    agentGuiPort: basePort + 30,
    autoBackendPort: basePort + 1,
    autoFrontendPort: basePort + 3,
    
    stateDir,
    workspaceDir: args.workspaceDir,
    reposDir: join(stateDir, 'repos'),
    
    skipSetup: args.skipSetup,
    verbose: args.verbose,
    dev: args.dev,
    localAutomation: args.localAutomation,
    localGui: args.localGui,
  };
}

function showHelp() {
  console.log(`
@openhands/local v${VERSION}

Run OpenHands locally with a single command - no Docker required.

USAGE:
  npx @openhands/local [options]

OPTIONS:
  -h, --help              Show this help message
  -v, --version           Show version number
  -p, --port <port>       Main entry port (default: 8000)
  -w, --workspace <path>  Working directory (default: current directory)
  --skip-setup            Skip cloning/installing dependencies
  --verbose               Show detailed output

DEVELOPMENT OPTIONS:
  --dev                   Use local automation repo (auto-detects)
  --local-automation <p>  Path to local automation repo
  --local-gui <path>      Path to local agent-server-gui repo

ENVIRONMENT VARIABLES:
  OH_AGENT_SERVER_GIT_REF   Git ref for agent-server SDK
  OH_AGENT_SERVER_VERSION   PyPI version for agent-server

EXAMPLES:
  npx @openhands/local
  npx @openhands/local --port 12000
  npx @openhands/local --dev

ACCESS:
  Main UI:      http://localhost:PORT/
  Automations:  http://localhost:PORT/automations/
  API Docs:     http://localhost:PORT/api/automation/docs
`);
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

function ensureUv() {
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
  
  const nodeVersion = process.version.slice(1);
  const [major] = nodeVersion.split('.').map(Number);
  if (major < 22) {
    logError(`Node.js 22+ required (found ${nodeVersion})`);
    process.exit(1);
  }
  
  if (!ensureUv()) {
    logError('uv is required. Install from https://docs.astral.sh/uv/');
    process.exit(1);
  }
  
  logSuccess('All prerequisites met');
}

function ensureDirectories(config) {
  const dirs = [
    config.stateDir,
    config.reposDir,
    join(config.stateDir, 'conversations'),
    join(config.stateDir, 'storage'),
    join(config.stateDir, 'workspaces'),
    join(config.stateDir, 'tmux'),
    join(config.stateDir, 'bash_events'),
  ];
  
  for (const dir of dirs) {
    if (!existsSync(dir)) {
      mkdirSync(dir, { recursive: true });
    }
  }
}

async function setupAgentServerGui(config) {
  logStep('2/4', 'Setting up agent-server-gui...');
  
  const guiDir = config.localGui || join(config.reposDir, 'agent-server-gui');
  
  if (!existsSync(guiDir)) {
    log('  Cloning repository...');
    mkdirSync(dirname(guiDir), { recursive: true });
    execSync(
      `git clone --depth 1 https://github.com/OpenHands/agent-server-gui.git "${guiDir}"`,
      { stdio: 'inherit' }
    );
  }
  
  log('  Installing npm dependencies...');
  execSync('npm ci', { cwd: guiDir, stdio: 'inherit' });
  
  logSuccess('agent-server-gui ready');
  return guiDir;
}

async function setupAutomation(config) {
  logStep('3/4', 'Setting up automation...');
  
  const autoDir = config.localAutomation || join(config.reposDir, 'automation');
  
  if (!existsSync(autoDir)) {
    log('  Cloning repository...');
    mkdirSync(dirname(autoDir), { recursive: true });
    execSync(
      `git clone --depth 1 https://github.com/OpenHands/automation.git "${autoDir}"`,
      { stdio: 'inherit' }
    );
  }
  
  log('  Syncing Python dependencies...');
  execSync('uv sync', { cwd: autoDir, stdio: 'inherit' });
  
  const frontendDir = join(autoDir, 'frontend');
  if (!existsSync(join(frontendDir, 'node_modules'))) {
    log('  Installing frontend npm dependencies...');
    execSync('npm ci', { cwd: frontendDir, stdio: 'inherit' });
  }
  
  logSuccess('Automation ready');
  return autoDir;
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

function startAgentServer(config) {
  logService('agent-server', `Starting on port ${config.agentServerPort}...`, c.blue);
  
  const gitRef = process.env.OH_AGENT_SERVER_GIT_REF || 'main';
  const version = process.env.OH_AGENT_SERVER_VERSION;
  
  let uvxArgs;
  
  if (version) {
    uvxArgs = [
      '--with', 'openhands-tools',
      '--with', 'openhands-workspace',
      `openhands-agent-server==${version}`,
    ];
  } else {
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
    cwd: config.workspaceDir,
    env: {
      TMUX_TMPDIR: join(config.stateDir, 'tmux'),
      OH_CONVERSATIONS_PATH: join(config.stateDir, 'conversations'),
      OH_BASH_EVENTS_DIR: join(config.stateDir, 'bash_events'),
      OPENHANDS_SUPPRESS_BANNER: '1',
    },
    color: c.blue,
  });
}

function startAgentServerGui(config, guiDir) {
  logService('agent-gui', `Starting on port ${config.agentGuiPort}...`, c.magenta);
  
  spawnService('agent-gui', 'npm', ['run', 'dev:frontend'], {
    cwd: guiDir,
    env: {
      VITE_BACKEND_HOST: `127.0.0.1:${config.agentServerPort}`,
      PORT: config.agentGuiPort.toString(),
    },
    color: c.magenta,
  });
}

function startAutomationBackend(config, autoDir) {
  logService('auto-be', `Starting on port ${config.autoBackendPort}...`, c.green);
  
  spawnService('auto-be', 'uv', [
    'run', 'uvicorn', 'automation.app:app',
    '--host', '127.0.0.1',
    '--port', config.autoBackendPort.toString(),
    '--reload',
  ], {
    cwd: autoDir,
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

function startAutomationFrontend(config, autoDir) {
  logService('auto-fe', `Starting on port ${config.autoFrontendPort}...`, c.cyan);
  
  spawnService('auto-fe', 'npm', ['run', 'dev'], {
    cwd: join(autoDir, 'frontend'),
    env: {
      VITE_AUTOMATION_HOST: `127.0.0.1:${config.autoBackendPort}`,
      VITE_OPENHANDS_HOST: `127.0.0.1:${config.agentServerPort}`,
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
  
  function routeToPort(url) {
    if (url.startsWith('/automations') || url.startsWith('/api/automation')) {
      return config.autoFrontendPort;
    }
    if (url.startsWith('/api/') || url.startsWith('/sockets') ||
        url === '/server_info' || url === '/health' ||
        url === '/ready' || url === '/alive') {
      return config.agentServerPort;
    }
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
    console.log(`${c.green}${c.bold}║${c.reset}  ${c.bold}OpenHands Local${c.reset}                                            ${c.green}${c.bold}║${c.reset}`);
    console.log(`${c.green}${c.bold}╠══════════════════════════════════════════════════════════════╣${c.reset}`);
    console.log(`${c.green}${c.bold}║${c.reset}                                                              ${c.green}${c.bold}║${c.reset}`);
    console.log(`${c.green}${c.bold}║${c.reset}  Main UI:      ${c.cyan}http://localhost:${config.proxyPort}/${c.reset}                       ${c.green}${c.bold}║${c.reset}`);
    console.log(`${c.green}${c.bold}║${c.reset}  Automations:  ${c.cyan}http://localhost:${config.proxyPort}/automations/${c.reset}           ${c.green}${c.bold}║${c.reset}`);
    console.log(`${c.green}${c.bold}║${c.reset}  API Docs:     ${c.cyan}http://localhost:${config.proxyPort}/api/automation/docs${c.reset}    ${c.green}${c.bold}║${c.reset}`);
    console.log(`${c.green}${c.bold}║${c.reset}                                                              ${c.green}${c.bold}║${c.reset}`);
    console.log(`${c.green}${c.bold}╚══════════════════════════════════════════════════════════════╝${c.reset}`);
    console.log('');
    console.log(`${c.dim}State directory: ${config.stateDir}${c.reset}`);
    console.log(`${c.dim}Workspace: ${config.workspaceDir}${c.reset}`);
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
  
  if (args.version) {
    console.log(`@openhands/local v${VERSION}`);
    process.exit(0);
  }
  
  if (args.help) {
    showHelp();
    process.exit(0);
  }
  
  const config = buildConfig(args);
  
  console.log('');
  console.log(`${c.cyan}${c.bold}@openhands/local${c.reset} v${VERSION}`);
  console.log('');
  
  // Setup phase
  checkPrerequisites();
  ensureDirectories(config);
  
  let guiDir, autoDir;
  
  if (!config.skipSetup) {
    guiDir = await setupAgentServerGui(config);
    autoDir = await setupAutomation(config);
  } else {
    guiDir = config.localGui || join(config.reposDir, 'agent-server-gui');
    autoDir = config.localAutomation || join(config.reposDir, 'automation');
  }
  
  // Start services phase
  logStep('4/4', 'Starting services...');
  
  startAgentServer(config);
  await waitForService('agent-server', `http://localhost:${config.agentServerPort}/server_info`);
  
  startAgentServerGui(config, guiDir);
  startAutomationBackend(config, autoDir);
  startAutomationFrontend(config, autoDir);
  
  await delay(3000);
  
  startReverseProxy(config);
}

main().catch(err => {
  logError(`Fatal error: ${err.message}`);
  if (err.stack) {
    console.error(c.dim + err.stack + c.reset);
  }
  process.exit(1);
});
