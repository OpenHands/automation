import { defineConfig, loadEnv } from "vite";
import viteTsconfigPaths from "vite-tsconfig-paths";
import { reactRouter } from "@react-router/dev/vite";
import { configDefaults } from "vitest/config";
import tailwindcss from "@tailwindcss/vite";
import svgr from "vite-plugin-svgr";

export default defineConfig(({ mode }) => {
  const {
    VITE_AUTOMATION_HOST = "127.0.0.1:8000",
    VITE_OPENHANDS_HOST = "127.0.0.1:3030",
    VITE_FRONTEND_PORT = "3002",
  } = loadEnv(mode, process.cwd());

  const FE_PORT = Number.parseInt(VITE_FRONTEND_PORT, 10);

  return {
    base: "/automations/",
    plugins: [
      !process.env.VITEST && reactRouter(),
      viteTsconfigPaths(),
      tailwindcss(),
      svgr(),
      // Allow the MSW service worker (served under /automations/) to use scope "/"
      // so it can intercept fetch requests from pages at /automations (no trailing slash).
      {
        name: "msw-service-worker-scope",
        configureServer(server) {
          server.middlewares.use((_req, res, next) => {
            if (_req.url?.includes("mockServiceWorker.js")) {
              res.setHeader("Service-Worker-Allowed", "/");
            }
            next();
          });
        },
      },
    ],
    optimizeDeps: {
      include: [
        "@tanstack/react-query",
        "react-hot-toast",
        "i18next",
        "i18next-http-backend",
        "i18next-browser-languagedetector",
        "react-i18next",
        "axios",
        "clsx",
        "tailwind-merge",
        "zustand",
      ],
    },
    server: {
      port: FE_PORT,
      host: true,
      allowedHosts: true,
      proxy: {
        // Order matters: /api/automation must come before /api
        "/api/automation": {
          target: `http://${VITE_AUTOMATION_HOST}/`,
          changeOrigin: true,
        },
        "/api": {
          target: `http://${VITE_OPENHANDS_HOST}/`,
          changeOrigin: true,
        },
      },
      watch: {
        ignored: ["**/node_modules/**", "**/.git/**"],
      },
    },
    clearScreen: false,
    test: {
      environment: "jsdom",
      setupFiles: ["vitest.setup.ts"],
      exclude: [...configDefaults.exclude, "tests"],
      coverage: {
        reporter: ["text", "json", "html", "lcov", "text-summary"],
        reportsDirectory: "coverage",
        include: ["src/**/*.{ts,tsx}"],
      },
    },
  };
});
