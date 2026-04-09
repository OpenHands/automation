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

| Variable               | Description                                          | Default Value     |
| ---------------------- | ---------------------------------------------------- | ----------------- |
| `VITE_AUTOMATION_HOST` | The automation service host with port                | `127.0.0.1:8000`  |
| `VITE_OPENHANDS_HOST`  | The OpenHands backend host with port                 | `127.0.0.1:3030`  |
| `VITE_FRONTEND_PORT`   | Port to run the frontend application                 | `3002`            |

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
