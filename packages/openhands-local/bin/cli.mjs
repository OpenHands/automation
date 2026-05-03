#!/usr/bin/env node
/**
 * @openhands/local - Run OpenHands locally with a single command
 * 
 * Usage:
 *   npx @openhands/local
 *   npx @openhands/local --model anthropic/claude-sonnet-4-20250514 --api-key sk-ant-...
 *   npx @openhands/local --help
 */

import { spawn, execSync } from 'node:child_process';
import { createServer, request as httpRequest } from 'node:http';
import { existsSync, mkdirSync, writeFileSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { homedir } from 'node:os';

const __dirname = dirname(fileURLToPath(import.meta.url));
const VERSION = '0.1.0';

// Parse command line arguments
function parseArgs() {
  const args = process.argv.slice(2);
  const config = {
    help: false,
    version: false,
    model: process.env.LLM_MODEL || null,
    apiKey: process.env.LLM_API_KEY || null,
    baseUrl: process.env.LLM_BASE_URL || null,
    port: 8000,
    dataDir: join(homedir(), '.openhands-local'),
    workspaceDir: process.cwd(),
    skipSetup: false,
    verbose: false,
    // Development mode options
    dev: false,
    localAutomation: null,
    localGui: null,
    sdkRef: null,
  };
  
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    switch (arg) {
      case '-h':
      case '--help':
        config.help = true;
        break;
      case '-v':
      case '--version':
        config.version = true;
        break;
      case '--model':
        config.model = args[++i];
        break;
      case '--api-key':
        config.apiKey = args[++i];
        break;
      case '--base-url':
        config.baseUrl = args[++i];
        break;
      case '-p':
      case '--port':
        config.port = parseInt(args[++i], 10);
        break;
      case '-d':
      case '--data-dir':
        config.dataDir = resolve(args[++i]);
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
      // Development mode
      case '--dev':
        config.dev = true;
        break;
      case '--local-automation':
        config.localAutomation = resolve(args[++i]);
        break;
      case '--local-gui':
        config.localGui = resolve(args[++i]);
        break;
      case '--sdk-ref':
        config.sdkRef = args[++i];
        break;
    }
  }
  
  // In dev mode, auto-detect local repos relative to this package
  if (config.dev) {
    const packageRoot = resolve(__dirname, '..');
    const autoRoot = resolve(packageRoot, '../..');
    
    if (!config.localAutomation && existsSync(join(autoRoot, 'automation', 'app.py'))) {
      config.localAutomation = autoRoot;
    }
  }
  
  return config;
}

function showHelp() {
  console.log(`
@openhands/local v${VERSION}

Run OpenHands locally with a single command - no Docker required.

USAGE:
  npx @openhands/local [options]
  openhands-local [options]

OPTIONS:
  -h, --help              Show this help message
  -v, --version           Show version number
  --model <model>         LLM model to use (e.g., anthropic/claude-sonnet-4-20250514)
  --api-key <key>         API key for the LLM provider
  --base-url <url>        Custom LLM base URL (for local models)
  -p, --port <port>       Port for the main UI (default: 8000)
  -d, --data-dir <path>   Data directory (default: ~/.openhands-local)
  -w, --workspace <path>  Workspace directory (default: current directory)
  --skip-setup            Skip dependency installation
  --verbose               Show detailed output

DEVELOPMENT OPTIONS:
  --dev                   Use local automation repo (auto-detects from package location)
  --local-automation <p>  Path to local automation repo
  --local-gui <path>      Path to local agent-server-gui repo  
  --sdk-ref <ref>         Install SDK from git ref (branch/tag/commit)

ENVIRONMENT VARIABLES:
  LLM_MODEL               Same as --model
  LLM_API_KEY             Same as --api-key
  LLM_BASE_URL            Same as --base-url

EXAMPLES:
  # Production: uses released packages from PyPI/npm
  npx @openhands/local --model anthropic/claude-sonnet-4-20250514 --api-key sk-ant-...

  # Development: use local automation repo (run from automation/packages/openhands-local)
  node bin/cli.mjs --dev --port 12000

  # Development with explicit paths
  npx @openhands/local --local-automation /path/to/automation --port 12000

REQUIREMENTS:
  - Node.js >= 22
  - Python >= 3.12
  - uv (https://docs.astral.sh/uv/)
  - tmux
  - git

ACCESS:
  After startup, open http://localhost:8000 in your browser.
  - Main UI:        http://localhost:8000/
  - Automations:    http://localhost:8000/automations/
  - API Docs:       http://localhost:8000/api/automation/docs
`);
}

// Colors for terminal output
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

function logStep(step, message) {
  console.log(`${c.cyan}[${step}]${c.reset} ${message}`);
}

function logSuccess(message) {
  console.log(`${c.green}✓${c.reset} ${message}`);
}

function logError(message) {
  console.error(`${c.red}✗${c.reset} ${message}`);
}

function logService(name, message, color = c.reset) {
  const ts = new Date().toISOString().split('T')[1].split('.')[0];
  console.log(`${c.dim}${ts}${c.reset} ${color}[${name}]${c.reset} ${message}`);
}

// Check if a command exists
function commandExists(cmd) {
  try {
    execSync(`which ${cmd}`, { stdio: 'pipe' });
    return true;
  } catch {
    return false;
  }
}

// Check prerequisites
function checkPrerequisites() {
  logStep('1/5', 'Checking prerequisites...');
  
  const checks = [
    { cmd: 'node', name: 'Node.js', minVersion: '22.0.0' },
    { cmd: 'python3', name: 'Python', minVersion: '3.12' },
    { cmd: 'uv', name: 'uv', install: 'https://docs.astral.sh/uv/' },
    { cmd: 'tmux', name: 'tmux', install: 'apt install tmux / brew install tmux' },
    { cmd: 'git', name: 'git' },
  ];
  
  const missing = [];
  
  for (const check of checks) {
    if (!commandExists(check.cmd)) {
      missing.push(check);
    }
  }
  
  if (missing.length > 0) {
    logError('Missing prerequisites:');
    for (const item of missing) {
      console.log(`  - ${item.name}${item.install ? ` (install: ${item.install})` : ''}`);
    }
    process.exit(1);
  }
  
  // Check Node.js version
  const nodeVersion = process.version.slice(1);
  const [major] = nodeVersion.split('.').map(Number);
  if (major < 22) {
    logError(`Node.js 22+ required, found ${process.version}`);
    process.exit(1);
  }
  
  logSuccess('All prerequisites met');
}

// Ensure directories exist
function ensureDirectories(config) {
  const dirs = [
    config.dataDir,
    join(config.dataDir, 'storage'),
    join(config.dataDir, 'conversations'),
    join(config.dataDir, 'repos'),
    config.workspaceDir,
  ];
  
  for (const dir of dirs) {
    if (!existsSync(dir)) {
      mkdirSync(dir, { recursive: true });
    }
  }
}

// Clone or update a git repository
function cloneOrUpdate(repoUrl, targetDir, branch = 'main') {
  if (existsSync(targetDir)) {
    // Already exists, optionally update
    return;
  }
  
  mkdirSync(dirname(targetDir), { recursive: true });
  execSync(`git clone --depth 1 --branch ${branch} ${repoUrl} ${targetDir}`, {
    stdio: 'inherit',
  });
}

// Setup agent-server-gui
function setupAgentServerGui(config) {
  logStep('2/5', 'Setting up Agent Server GUI...');
  
  let guiDir;
  
  if (config.localGui) {
    guiDir = config.localGui;
    log(`  Using local GUI: ${guiDir}`, c.cyan);
  } else {
    guiDir = join(config.dataDir, 'repos', 'agent-server-gui');
    
    if (!existsSync(guiDir)) {
      log('  Cloning agent-server-gui...');
      cloneOrUpdate('https://github.com/OpenHands/agent-server-gui.git', guiDir);
    }
  }
  
  if (!existsSync(join(guiDir, 'node_modules'))) {
    log('  Installing npm dependencies...');
    execSync('npm ci', { cwd: guiDir, stdio: 'inherit' });
  }
  
  logSuccess('Agent Server GUI ready');
  return guiDir;
}

// Setup automation service
function setupAutomation(config) {
  logStep('3/5', 'Setting up Automation service...');
  
  let autoDir;
  
  if (config.localAutomation) {
    autoDir = config.localAutomation;
    log(`  Using local automation: ${autoDir}`, c.cyan);
  } else {
    autoDir = join(config.dataDir, 'repos', 'automation');
    
    if (!existsSync(autoDir)) {
      log('  Cloning automation repository...');
      cloneOrUpdate('https://github.com/OpenHands/automation.git', autoDir, 'feat/agent-server-gui');
    }
  }
  
  if (!existsSync(join(autoDir, 'frontend', 'node_modules'))) {
    log('  Installing frontend npm dependencies...');
    execSync('npm ci', { cwd: join(autoDir, 'frontend'), stdio: 'inherit' });
  }
  
  log('  Syncing Python dependencies...');
  execSync('uv sync', { cwd: autoDir, stdio: 'inherit' });
  
  logSuccess('Automation service ready');
  return autoDir;
}

// Find the pip-installed bin directory
function findPipBinDir() {
  try {
    const userBase = execSync('python3 -m site --user-base', { stdio: 'pipe', encoding: 'utf-8' }).trim();
    return join(userBase, 'bin');
  } catch {
    return join(homedir(), '.local', 'bin');
  }
}

// Install agent-server
function installAgentServer(config) {
  logStep('4/5', 'Installing Agent Server...');
  
  const pipBinDir = findPipBinDir();
  
  // Add pip bin to PATH for this process and child processes
  process.env.PATH = `${pipBinDir}:${process.env.PATH}`;
  
  // Check if already installed
  const agentServerPath = join(pipBinDir, 'agent-server');
  let needsInstall = !existsSync(agentServerPath);
  
  if (!needsInstall) {
    // Verify it works
    try {
      execSync(`${agentServerPath} --help`, { stdio: 'pipe' });
      log(`  Found agent-server at ${agentServerPath}`, c.dim);
    } catch {
      needsInstall = true;
    }
  }
  
  if (config.sdkRef) {
    // Install from git with specific ref (forces reinstall)
    log(`  Installing SDK from git ref: ${config.sdkRef}...`);
    const gitBase = `git+https://github.com/OpenHands/software-agent-sdk.git@${config.sdkRef}`;
    execSync(`pip install --user --upgrade --force-reinstall "${gitBase}#subdirectory=openhands-agent-server" "${gitBase}#subdirectory=openhands-sdk" "${gitBase}#subdirectory=openhands-tools" "${gitBase}#subdirectory=openhands-workspace" libtmux`, {
      stdio: 'inherit',
    });
    needsInstall = false;
  } else if (needsInstall) {
    // Install from PyPI with matching versions
    log('  Installing openhands-agent-server from PyPI...');
    execSync('pip install --user "openhands-agent-server" "openhands-sdk" "openhands-tools" "openhands-workspace" libtmux', {
      stdio: 'inherit',
    });
  }
  
  // Final verification
  if (!existsSync(agentServerPath)) {
    logError(`agent-server not found at ${agentServerPath}`);
    logError(`Make sure ${pipBinDir} is in your PATH`);
    process.exit(1);
  }
  
  logSuccess('Agent Server ready');
  return pipBinDir;
}

// Process manager
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

// Start all services
function startServices(config, guiDir, autoDir, pipBinDir) {
  logStep('5/5', 'Starting services...');
  
  const ports = {
    agentServer: 3002,
    agentGui: 3030,
    autoBackend: 8001,
    autoFrontend: 3003,
    proxy: config.port,
  };
  
  // Use full path to agent-server
  const agentServerBin = join(pipBinDir, 'agent-server');
  
  // Start Agent Server
  log('  Starting Agent Server...');
  spawnService('agent-server', agentServerBin, [
    '--host', '127.0.0.1',
    '--port', ports.agentServer.toString(),
  ], {
    cwd: config.workspaceDir,
    env: {
      OH_CONVERSATIONS_PATH: join(config.dataDir, 'conversations'),
      OPENHANDS_SUPPRESS_BANNER: '1',
    },
    color: c.blue,
  });
  
  // Wait for agent server to start
  setTimeout(() => {
    // Start Agent Server GUI
    log('  Starting Agent Server GUI...');
    spawnService('agent-gui', 'npm', ['run', 'dev:frontend'], {
      cwd: guiDir,
      env: {
        VITE_BACKEND_HOST: `127.0.0.1:${ports.agentServer}`,
        VITE_FRONTEND_PORT: ports.agentGui.toString(),
      },
      color: c.magenta,
    });
    
    // Start Automation Backend
    log('  Starting Automation Backend...');
    spawnService('auto-backend', 'uv', [
      'run', 'uvicorn', 'automation.app:app',
      '--host', '127.0.0.1',
      '--port', ports.autoBackend.toString(),
    ], {
      cwd: autoDir,
      env: {
        AUTOMATION_AGENT_SERVER_URL: `http://localhost:${ports.agentServer}`,
        AUTOMATION_DB_URL: `sqlite+aiosqlite:///${join(config.dataDir, 'automations.db')}`,
        AUTOMATION_BASE_URL: `http://localhost:${ports.proxy}`,
        AUTOMATION_WORKSPACE_BASE: config.workspaceDir,
        AUTOMATION_AUTH_DISABLED: 'true',
        FILE_STORE: 'local',
        LOCAL_STORAGE_PATH: join(config.dataDir, 'storage'),
        OPENHANDS_SUPPRESS_BANNER: '1',
        ...(config.model && { AUTOMATION_LLM_MODEL: config.model }),
        ...(config.apiKey && { AUTOMATION_LLM_API_KEY: config.apiKey }),
        ...(config.baseUrl && { AUTOMATION_LLM_BASE_URL: config.baseUrl }),
      },
      color: c.green,
    });
    
    // Start Automation Frontend
    log('  Starting Automation Frontend...');
    spawnService('auto-frontend', 'npm', ['run', 'dev'], {
      cwd: join(autoDir, 'frontend'),
      env: {
        VITE_AUTOMATION_HOST: `127.0.0.1:${ports.autoBackend}`,
        VITE_OPENHANDS_HOST: `127.0.0.1:${ports.agentServer}`,
        VITE_FRONTEND_PORT: ports.autoFrontend.toString(),
      },
      color: c.cyan,
    });
    
    // Start reverse proxy after a delay
    setTimeout(() => {
      startProxy(ports);
    }, 3000);
    
  }, 2000);
}

// Start reverse proxy
function startProxy(ports) {
  function routeToPort(url) {
    // /api/automation/* -> automation backend directly
    if (url.startsWith('/api/automation')) {
      return ports.autoBackend;
    }
    // /automations/* -> automation frontend
    if (url.startsWith('/automations')) {
      return ports.autoFrontend;
    }
    // /api/*, /sockets, etc -> agent server
    if (url.startsWith('/api/') || url.startsWith('/sockets') || 
        url === '/server_info' || url === '/health' || 
        url === '/ready' || url === '/alive') {
      return ports.agentServer;
    }
    // Everything else -> agent server GUI
    return ports.agentGui;
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
  
  server.listen(ports.proxy, () => {
    console.log('');
    console.log(`${c.green}${c.bold}╔══════════════════════════════════════════════════════════════╗${c.reset}`);
    console.log(`${c.green}${c.bold}║${c.reset}  ${c.bold}OpenHands Local${c.reset} is running!                               ${c.green}${c.bold}║${c.reset}`);
    console.log(`${c.green}${c.bold}╠══════════════════════════════════════════════════════════════╣${c.reset}`);
    console.log(`${c.green}${c.bold}║${c.reset}                                                              ${c.green}${c.bold}║${c.reset}`);
    console.log(`${c.green}${c.bold}║${c.reset}  Main UI:      ${c.cyan}http://localhost:${ports.proxy}/${c.reset}                       ${c.green}${c.bold}║${c.reset}`);
    console.log(`${c.green}${c.bold}║${c.reset}  Automations:  ${c.cyan}http://localhost:${ports.proxy}/automations/${c.reset}           ${c.green}${c.bold}║${c.reset}`);
    console.log(`${c.green}${c.bold}║${c.reset}  API Docs:     ${c.cyan}http://localhost:${ports.proxy}/api/automation/docs${c.reset}    ${c.green}${c.bold}║${c.reset}`);
    console.log(`${c.green}${c.bold}║${c.reset}                                                              ${c.green}${c.bold}║${c.reset}`);
    console.log(`${c.green}${c.bold}╚══════════════════════════════════════════════════════════════╝${c.reset}`);
    console.log('');
    console.log(`${c.dim}Press Ctrl+C to stop${c.reset}`);
    console.log('');
  });
}

// Graceful shutdown
function shutdown() {
  console.log('');
  log('Shutting down...', c.yellow);
  
  for (const [name, proc] of processes) {
    log(`  Stopping ${name}...`, c.dim);
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

// Main
async function main() {
  const config = parseArgs();
  
  if (config.version) {
    console.log(`@openhands/local v${VERSION}`);
    process.exit(0);
  }
  
  if (config.help) {
    showHelp();
    process.exit(0);
  }
  
  console.log('');
  console.log(`${c.cyan}${c.bold}@openhands/local${c.reset} v${VERSION}`);
  console.log('');
  
  // Setup
  checkPrerequisites();
  ensureDirectories(config);
  
  let guiDir, autoDir, pipBinDir;
  
  if (!config.skipSetup) {
    guiDir = setupAgentServerGui(config);
    autoDir = setupAutomation(config);
    pipBinDir = installAgentServer(config);
  } else {
    guiDir = config.localGui || join(config.dataDir, 'repos', 'agent-server-gui');
    autoDir = config.localAutomation || join(config.dataDir, 'repos', 'automation');
    pipBinDir = findPipBinDir();
    // Still need to add pip bin to PATH
    process.env.PATH = `${pipBinDir}:${process.env.PATH}`;
  }
  
  // Start
  startServices(config, guiDir, autoDir, pipBinDir);
}

main().catch(err => {
  logError(`Fatal error: ${err.message}`);
  if (err.stack) {
    console.error(c.dim + err.stack + c.reset);
  }
  process.exit(1);
});
