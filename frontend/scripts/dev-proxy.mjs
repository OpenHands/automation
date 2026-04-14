/**
 * Local reverse proxy that serves both the OpenHands frontend and the
 * Automation frontend under a single origin, enabling cross-tab
 * localStorage sharing and storage events — mirroring production routing.
 *
 * Routing (evaluated in order):
 *   /automations/*     →  localhost:3002  (Automation frontend)
 *   /api/automation/*  →  localhost:3002  (→ Vite proxy → Automation backend)
 *   /api/*             →  localhost:3030  (OpenHands backend)
 *   /*                 →  localhost:3030  (OpenHands frontend)
 *
 * Usage:
 *   node scripts/dev-proxy.mjs                    # defaults: proxy=3000, OH=3030, auto=3002
 *   node scripts/dev-proxy.mjs 3000 3030 3002     # explicit ports
 *
 * Then access both apps via http://localhost:3000
 */

import { createServer, request as httpRequest } from "node:http";

const [proxyPort, ohPort, autoPort] = [
  parseInt(process.argv[2] || "3000", 10),
  parseInt(process.argv[3] || "3030", 10),
  parseInt(process.argv[4] || "3002", 10),
];

function routeToPort(url) {
  if (url.startsWith("/automations") || url.startsWith("/api/automation")) {
    return autoPort;
  }
  return ohPort;
}

function proxy(req, res, targetPort) {
  const options = {
    hostname: "localhost",
    port: targetPort,
    path: req.url,
    method: req.method,
    headers: req.headers,
  };

  const proxyReq = httpRequest(options, (proxyRes) => {
    res.writeHead(proxyRes.statusCode, proxyRes.headers);
    proxyRes.pipe(res, { end: true });
  });

  proxyReq.on("error", (err) => {
    res.writeHead(502);
    res.end(`Proxy error: cannot reach localhost:${targetPort} — ${err.message}`);
  });

  req.pipe(proxyReq, { end: true });
}

const server = createServer((req, res) => {
  proxy(req, res, routeToPort(req.url));
});

// Handle WebSocket upgrades (HMR for both Vite dev servers)
server.on("upgrade", (req, socket, head) => {
  const targetPort = routeToPort(req.url);

  const options = {
    hostname: "localhost",
    port: targetPort,
    path: req.url,
    method: req.method,
    headers: req.headers,
  };

  const proxyReq = httpRequest(options);

  proxyReq.on("upgrade", (proxyRes, proxySocket, proxyHead) => {
    socket.write(
      `HTTP/${proxyRes.httpVersion} ${proxyRes.statusCode} ${proxyRes.statusMessage}\r\n`,
    );
    for (let i = 0; i < proxyRes.rawHeaders.length; i += 2) {
      socket.write(`${proxyRes.rawHeaders[i]}: ${proxyRes.rawHeaders[i + 1]}\r\n`);
    }
    socket.write("\r\n");

    if (proxyHead.length > 0) socket.write(proxyHead);

    proxySocket.pipe(socket, { end: true });
    socket.pipe(proxySocket, { end: true });
  });

  proxyReq.on("error", () => {
    socket.destroy();
  });

  proxyReq.end();
});

server.listen(proxyPort, () => {
  console.log(`\nDev proxy running on http://localhost:${proxyPort}`);
  console.log(`  /automations/*     →  http://localhost:${autoPort}`);
  console.log(`  /api/automation/*  →  http://localhost:${autoPort}`);
  console.log(`  /*                 →  http://localhost:${ohPort}\n`);
});
