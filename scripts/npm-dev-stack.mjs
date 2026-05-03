#!/usr/bin/env node
/**
 * Development stack runner for OpenHands local development
 * 
 * This script replaces Docker by running all services directly:
 * 
 *   1. Agent Server (Python) - port 3002
 *   2. Agent Server GUI (Node.js) - port 3030
 *   3. Automations Backend (Python) - port 8001
 *   4. Automations Frontend (Node.js) - port 3003
 *   5. Reverse Proxy (Node.js) - port 8000 (main entry point)
 * 
 * Prerequisites:
 *   - Node.js >= 22
 *   - Python >= 3.12
 *   - uv (Python package manager)
 *   - tmux (required by agent-server for local runtime)
 *   - git (to clone agent-server-gui if not present)
 * 
 * Usage:
 *   node scripts/npm-dev-stack.mjs
 *   # or
 *   npm run dev:all  (if added to package.json)
 * 
 * Environment variables:
 *   - LLM_MODEL: LLM model to use (e.g., anthropic/claude-sonnet-4-20250514)
 *   - LLM_API_KEY: API key for the LLM
 *   - LLM_BASE_URL: (optional) Custom LLM base URL
 *   - AGENT_SERVER_GUI_PATH: (optional) Path to agent-server-gui repo
 *   - SDK_VERSION: (optional) SDK version to install (default: latest)
 * 
 * Access points after startup:
 *   - http://localhost:8000/           - Agent Server GUI (conversations)
 *   - http://localhost:8000/automations/ - Automations UI
 *   - http://localhost:8000/api/        - Agent Server API
 *   - http://localhost:8000/api/automation/ - Automations API
 */

import { spawn, execSync } from 'node:child_process';
import { createServer, request as httpRequest } from 'node:http';
import { existsSync, mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const projectRoot = resolve(__dirname, '..');

// Configuration
const config = {
  // Ports
  proxyPort: parseInt(process.env.PROXY_PORT || '8000', 10),
  agentServerPort: parseInt(process.env.AGENT_SERVER_PORT || '3002', 10),
  agentGuiPort: parseInt(process.env.AGENT_GUI_PORT || '3030', 10),
  automationBackendPort: parseInt(process.env.AUTOMATION_BACKEND_PORT || '8001', 10),
  automationFrontendPort: parseInt(process.env.AUTOMATION_FRONTEND_PORT || '3003', 10),
  
  // Paths
  agentServerGuiPath: process.env.AGENT_SERVER_GUI_PATH || join(projectRoot, '.dev', 'agent-server-gui'),
  sdkVersion: process.env.SDK_VERSION || null,  // null = latest from PyPI
  
  // Database
  dbPath: process.env.DB_PATH || join(projectRoot, '.dev', 'data', 'automations.db'),
  storagePath: process.env.STORAGE_PATH || join(projectRoot, '.dev', 'data', 'storage'),
  conversationsPath: process.env.CONVERSATIONS_PATH || join(projectRoot, '.dev', 'data', 'conversations'),
  workspacePath: process.env.WORKSPACE_PATH || join(projectRoot, '.dev', 'workspace'),
};

// Colors for terminal output
const colors = {
  reset: '\x1b[0m',
  red: '\x1b[31m',
  green: '\x1b[32m',
  yellow: '\x1b[33m',
  blue: '\x1b[34m',
  magenta: '\x1b[35m',
  cyan: '\x1b[36m',
};

function log(service, message, color = colors.reset) {
  const timestamp = new Date().toISOString().split('T')[1].split('.')[0];
  console.log(`${colors.cyan}[${timestamp}]${color} [${service}]${colors.reset} ${message}`);
}

function logError(service, message) {
  log(service, message, colors.red);
}

function logSuccess(service, message) {
  log(service, message, colors.green);
}

// Check prerequisites
function checkPrerequisites() {
  const missing = [];
  
  try {
    execSync('node --version', { stdio: 'pipe' });
  } catch {
    missing.push('Node.js');
  }
  
  try {
    execSync('uv --version', { stdio: 'pipe' });
  } catch {
    missing.push('uv (install from https://docs.astral.sh/uv/)');
  }
  
  try {
    execSync('tmux -V', { stdio: 'pipe' });
  } catch {
    missing.push('tmux (required by agent-server)');
  }
  
  try {
    execSync('git --version', { stdio: 'pipe' });
  } catch {
    missing.push('git');
  }
  
  if (missing.length > 0) {
    console.error(`${colors.red}Missing prerequisites:${colors.reset}`);
    missing.forEach(item => console.error(`  - ${item}`));
    process.exit(1);
  }
  
  logSuccess('setup', 'All prerequisites met');
}

// Ensure directories exist
function ensureDirectories() {
  const dirs = [
    dirname(config.dbPath),
    config.storagePath,
    config.conversationsPath,
    config.workspacePath,
  ];
  
  dirs.forEach(dir => {
    if (!existsSync(dir)) {
      mkdirSync(dir, { recursive: true });
      log('setup', `Created directory: ${dir}`);
    }
  });
}

// Clone or update agent-server-gui
async function setupAgentServerGui() {
  if (!existsSync(config.agentServerGuiPath)) {
    log('agent-gui', 'Cloning agent-server-gui repository...');
    mkdirSync(dirname(config.agentServerGuiPath), { recursive: true });
    execSync(
      `git clone --depth 1 https://github.com/OpenHands/agent-server-gui.git ${config.agentServerGuiPath}`,
      { stdio: 'inherit' }
    );
  }
  
  // Install npm dependencies
  log('agent-gui', 'Installing npm dependencies...');
  execSync('npm ci', { cwd: config.agentServerGuiPath, stdio: 'inherit' });
  logSuccess('agent-gui', 'Agent Server GUI setup complete');
}

// Install agent-server and SDK using uv
function installAgentServer() {
  log('agent-server', 'Installing openhands-agent-server and SDK...');
  
  const packages = [
    'openhands-agent-server',
    'openhands-sdk',
    'openhands-tools',
    'openhands-workspace',
    'libtmux',
  ];
  
  // Use specific version if provided
  const pkgSpecs = packages.map(pkg => 
    config.sdkVersion ? `${pkg}==${config.sdkVersion}` : pkg
  );
  
  execSync(`uv pip install --system ${pkgSpecs.join(' ')}`, { stdio: 'inherit' });
  logSuccess('agent-server', 'Agent Server installed');
}

// Process manager
const processes = new Map();

function spawnService(name, command, args, options = {}) {
  const proc = spawn(command, args, {
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, ...options.env },
    cwd: options.cwd || projectRoot,
    shell: true,
  });
  
  proc.stdout.on('data', data => {
    data.toString().split('\n').filter(Boolean).forEach(line => {
      log(name, line.trim());
    });
  });
  
  proc.stderr.on('data', data => {
    data.toString().split('\n').filter(Boolean).forEach(line => {
      log(name, line.trim(), colors.yellow);
    });
  });
  
  proc.on('exit', code => {
    if (code !== 0 && code !== null) {
      logError(name, `Process exited with code ${code}`);
    }
    processes.delete(name);
  });
  
  processes.set(name, proc);
  return proc;
}

// Start Agent Server
function startAgentServer() {
  log('agent-server', `Starting on port ${config.agentServerPort}...`);
  
  spawnService('agent-server', 'agent-server', [
    '--host', '127.0.0.1',
    '--port', config.agentServerPort.toString(),
  ], {
    env: {
      OH_CONVERSATIONS_PATH: config.conversationsPath,
      OPENHANDS_SUPPRESS_BANNER: '1',
    },
    cwd: config.workspacePath,
  });
}

// Start Agent Server GUI (dev mode)
function startAgentServerGui() {
  log('agent-gui', `Starting on port ${config.agentGuiPort}...`);
  
  spawnService('agent-gui', 'npm', ['run', 'dev:frontend'], {
    env: {
      VITE_BACKEND_HOST: `127.0.0.1:${config.agentServerPort}`,
      PORT: config.agentGuiPort.toString(),
    },
    cwd: config.agentServerGuiPath,
  });
}

// Start Automation Backend
function startAutomationBackend() {
  log('auto-backend', `Starting on port ${config.automationBackendPort}...`);
  
  spawnService('auto-backend', 'uv', [
    'run', 'uvicorn', 'automation.app:app',
    '--host', '127.0.0.1',
    '--port', config.automationBackendPort.toString(),
    '--reload',
  ], {
    env: {
      AUTOMATION_AGENT_SERVER_URL: `http://localhost:${config.agentServerPort}`,
      AUTOMATION_DB_URL: `sqlite+aiosqlite:///${config.dbPath}`,
      AUTOMATION_BASE_URL: `http://localhost:${config.proxyPort}`,
      AUTOMATION_WORKSPACE_BASE: config.workspacePath,
      AUTOMATION_AUTH_DISABLED: 'true',
      FILE_STORE: 'local',
      LOCAL_STORAGE_PATH: config.storagePath,
      OPENHANDS_SUPPRESS_BANNER: '1',
    },
    cwd: projectRoot,
  });
}

// Start Automation Frontend
function startAutomationFrontend() {
  log('auto-frontend', `Starting on port ${config.automationFrontendPort}...`);
  
  spawnService('auto-frontend', 'npm', ['run', 'dev'], {
    env: {
      VITE_AUTOMATION_HOST: `127.0.0.1:${config.automationBackendPort}`,
      VITE_OPENHANDS_HOST: `127.0.0.1:${config.agentServerPort}`,
      VITE_FRONTEND_PORT: config.automationFrontendPort.toString(),
    },
    cwd: join(projectRoot, 'frontend'),
  });
}

// Start reverse proxy
function startReverseProxy() {
  log('proxy', `Starting reverse proxy on port ${config.proxyPort}...`);
  
  function routeToPort(url) {
    // Order matters: more specific routes first
    if (url.startsWith('/automations') || url.startsWith('/api/automation')) {
      return config.automationFrontendPort;
    }
    if (url.startsWith('/api/') || url.startsWith('/sockets') || 
        url === '/server_info' || url === '/health' || 
        url === '/ready' || url === '/alive') {
      return config.agentServerPort;
    }
    // Default: Agent Server GUI
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
      res.end(`Proxy error: cannot reach localhost:${targetPort} — ${err.message}`);
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
    
    proxyReq.on('error', () => {
      socket.destroy();
    });
    
    proxyReq.end();
  });
  
  server.listen(config.proxyPort, () => {
    logSuccess('proxy', `Reverse proxy ready on http://localhost:${config.proxyPort}`);
    console.log('');
    console.log(`${colors.green}╔════════════════════════════════════════════════════════════╗${colors.reset}`);
    console.log(`${colors.green}║${colors.reset}  OpenHands Local Development Stack                         ${colors.green}║${colors.reset}`);
    console.log(`${colors.green}╠════════════════════════════════════════════════════════════╣${colors.reset}`);
    console.log(`${colors.green}║${colors.reset}  Main UI:        ${colors.cyan}http://localhost:${config.proxyPort}/${colors.reset}                    ${colors.green}║${colors.reset}`);
    console.log(`${colors.green}║${colors.reset}  Automations:    ${colors.cyan}http://localhost:${config.proxyPort}/automations/${colors.reset}        ${colors.green}║${colors.reset}`);
    console.log(`${colors.green}║${colors.reset}  API Docs:       ${colors.cyan}http://localhost:${config.proxyPort}/api/automation/docs${colors.reset} ${colors.green}║${colors.reset}`);
    console.log(`${colors.green}╚════════════════════════════════════════════════════════════╝${colors.reset}`);
    console.log('');
    console.log(`${colors.yellow}Note: Configure LLM settings via environment variables:${colors.reset}`);
    console.log(`  LLM_MODEL=anthropic/claude-sonnet-4-20250514`);
    console.log(`  LLM_API_KEY=sk-ant-...`);
    console.log('');
  });
}

// Graceful shutdown
function shutdown() {
  console.log('\n');
  log('shutdown', 'Stopping all services...');
  
  processes.forEach((proc, name) => {
    log('shutdown', `Stopping ${name}...`);
    proc.kill('SIGTERM');
  });
  
  // Force kill after 5 seconds
  setTimeout(() => {
    processes.forEach((proc, name) => {
      if (!proc.killed) {
        logError('shutdown', `Force killing ${name}...`);
        proc.kill('SIGKILL');
      }
    });
    process.exit(0);
  }, 5000);
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);

// Main
async function main() {
  console.log('');
  console.log(`${colors.cyan}Starting OpenHands Local Development Stack...${colors.reset}`);
  console.log('');
  
  // Pre-flight checks
  checkPrerequisites();
  ensureDirectories();
  
  // Setup external dependencies
  await setupAgentServerGui();
  installAgentServer();
  
  // Install frontend dependencies if needed
  if (!existsSync(join(projectRoot, 'frontend', 'node_modules'))) {
    log('auto-frontend', 'Installing npm dependencies...');
    execSync('npm ci', { cwd: join(projectRoot, 'frontend'), stdio: 'inherit' });
  }
  
  // Sync automation backend dependencies
  log('auto-backend', 'Syncing uv dependencies...');
  execSync('uv sync', { cwd: projectRoot, stdio: 'inherit' });
  
  // Start all services
  startAgentServer();
  
  // Wait for agent server to start
  await new Promise(resolve => setTimeout(resolve, 2000));
  
  startAgentServerGui();
  startAutomationBackend();
  startAutomationFrontend();
  
  // Wait for services to start
  await new Promise(resolve => setTimeout(resolve, 3000));
  
  // Start reverse proxy
  startReverseProxy();
}

main().catch(err => {
  console.error(`${colors.red}Fatal error:${colors.reset}`, err);
  process.exit(1);
});
