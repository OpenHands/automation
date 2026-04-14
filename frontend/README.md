# Getting Started with the Automations Frontend

## Overview

This is the frontend of the OpenHands Automations project. It is a standalone React single-page application (SPA) that provides a web interface for managing OpenHands automations — viewing, enabling/disabling, and deleting user-configured automations.

The application is deployed at `app.all-hands.dev/automations` and shares the same domain as OpenHands to enable automatic session cookie sharing for authentication.

## Tech Stack

- React 19 + React Router v7 (SPA Mode)
- TypeScript (strict mode)
- Vite
- TanStack React Query
- Zustand
- Axios
- Tailwind CSS
- i18next
- react-hot-toast
- React Testing Library
- Vitest
- Playwright

## Getting Started

### Prerequisites

- Node.js 22.12.x or later
- `npm` (v10.5.0 or later)

### Installation

```sh
# Navigate to the frontend directory
cd frontend

# Install dependencies
npm install
```

### Running the Application in Development Mode

```sh
npm run dev
```

This will start the application in development mode on [http://localhost:3002/automations](http://localhost:3002/automations).

The dev server proxies API requests to:

- `/api/automation/*` → Automation Service (`127.0.0.1:8000`)
- `/api/*` → OpenHands Backend (`127.0.0.1:3030`)

### Running with OpenHands (Cross-Tab Sync)

To test features that require cross-tab communication with the OpenHands frontend (e.g., language synchronization via `localStorage`), both apps must be served from the same origin. A local reverse proxy script is included for this purpose.

**1. Start both dev servers:**

```sh
# Terminal 1 — OpenHands frontend (port 3030)
cd /path/to/OpenHands/frontend
npm run dev

# Terminal 2 — Automation frontend (port 3002)
cd /path/to/automation/frontend
npm run dev
```

**2. Start the reverse proxy:**

```sh
# Terminal 3
npm run dev:proxy
```

**3. Access both apps via the proxy:**

- OpenHands: [http://localhost:3000](http://localhost:3000)
- Automations: [http://localhost:3000/automations](http://localhost:3000/automations)

Both apps now share the same origin (`localhost:3000`), so `localStorage` and `storage` events work across tabs.

The proxy routes requests as follows:

| Path | Target |
| --- | --- |
| `/automations/*` | `localhost:3002` (Automation frontend) |
| `/api/automation/*` | `localhost:3002` (→ Vite proxy → Automation backend) |
| `/api/*` | `localhost:3030` (OpenHands backend) |
| `/*` | `localhost:3030` (OpenHands frontend) |

Port defaults can be overridden via CLI arguments:

```sh
node scripts/dev-proxy.mjs [proxyPort] [ohPort] [autoPort]
# e.g., node scripts/dev-proxy.mjs 3000 3030 3002
```

### Running with Mocked APIs (No Backend Required)

For frontend development without running any backend services, use the mock development mode:

```sh
npm run dev:mock
```

This starts the application with [MSW (Mock Service Worker)](https://mswjs.io/) intercepting all API requests and returning realistic mock responses. No Automation Service or OpenHands Backend is needed.

**What gets mocked:**

- `GET /api/automation/v1` — Returns a list of 5 sample automations (3 active, 2 inactive)
- `GET /api/automation/v1/:id` — Returns automation detail
- `PATCH /api/automation/v1/:id` — Simulates enable/disable toggle
- `DELETE /api/automation/v1/:id` — Simulates deletion
- `POST /api/authenticate` — Always returns 200 OK
- `GET /api/me` — Returns mock user context with owner permissions

Mock data is stateful within a session — toggling or deleting an automation persists until the page is refreshed.

**How it works:**

The `VITE_MOCK_API` environment variable controls mock mode. When set to `true`, the MSW browser service worker is registered in `entry.client.tsx` before the app renders. Mock handlers are defined in `src/mocks/` and follow the same API contracts as the real backends.

| Variable        | `npm run dev` | `npm run dev:mock` |
| --------------- | ------------- | ------------------ |
| `VITE_MOCK_API` | `false`       | `true`             |

### Building for Production

```sh
npm run build
```

The build output is generated in the `build/` directory and can be served as static files.

```sh
npm start
```

### Environment Variables

The frontend application uses the following environment variables:

| Variable               | Description                           | Default Value    |
| ---------------------- | ------------------------------------- | ---------------- |
| `VITE_AUTOMATION_HOST` | The automation service host with port | `127.0.0.1:8000` |
| `VITE_OPENHANDS_HOST`  | The OpenHands backend host with port  | `127.0.0.1:3030` |
| `VITE_FRONTEND_PORT`   | Port to run the frontend application  | `3002`           |

You can create a `.env` file in the frontend directory based on the `.env.example` file.

### Project Structure

```sh
frontend
├── public
│   └── locales          # i18n translation files
├── src
│   ├── api              # API clients (Axios instances)
│   ├── components       # React components
│   ├── constants        # Application constants
│   ├── hooks            # Custom React hooks
│   ├── i18n             # Internationalization (declaration, config)
│   ├── mocks            # MSW mock handlers and fixtures (dev:mock)
│   ├── icons            # SVG icons
│   ├── routes           # React Router route components
│   ├── stores           # Zustand state stores
│   ├── types            # TypeScript type definitions
│   ├── utils            # Utility/helper functions
│   ├── entry.client.tsx # Client entry point
│   ├── root.tsx         # Root layout component
│   └── routes.ts        # Route definitions
├── .eslintrc            # ESLint configuration
├── .prettierrc.json     # Prettier configuration
├── commitlint.config.cjs
├── package.json
├── playwright.config.ts
├── react-router.config.ts
├── tailwind.config.js
├── tsconfig.json
├── vite.config.ts
└── vitest.setup.ts
```

### Architecture

The Automations frontend communicates with two backends:

1. **Automation Service** — CRUD operations for automations (`/api/automation/v1/*`)
2. **OpenHands Backend** — Authentication and user context (`/api/authenticate`, `/api/me`)

Authentication uses same-domain cookie sharing with OpenHands. The `keycloak_auth` session cookie is automatically included in requests to both services.

For more details, see [ADR-0003: Automations Frontend Architecture](../docs/0003-automations-frontend/) in the architecture repository.

## Testing

### Running Tests

```sh
# Unit tests
npm run test

# E2E tests
npm run test:e2e
```

### Code Quality

```sh
# Run linting and type checking
npm run lint

# Auto-fix linting issues
npm run lint:fix

# Type checking only
npm run typecheck
```

## Contributing

### Commit Convention

This project uses [Conventional Commits](https://www.conventionalcommits.org/). Commit messages are validated by commitlint.

### Pre-commit Hooks

Husky runs lint-staged on pre-commit, which:

- Runs ESLint with auto-fix
- Formats code with Prettier
- Checks TypeScript types
