import axios from "axios";

/**
 * Automation Service API client.
 * Proxied to the automation service in development via Vite config.
 */
export const automationApi = axios.create({
  baseURL: "/api/automation",
});

/**
 * OpenHands Backend API client.
 * Used for authentication and user context.
 * Proxied to the OpenHands backend in development via Vite config.
 *
 * Note: 401 handling is not done via Axios interceptors. Instead, it is
 * handled by the React Query global error handler (query-client-config.ts),
 * which invalidates the auth query key. RootLayout then handles redirect
 * or re-auth modal display based on the auth state.
 */
export const openhandsApi = axios.create();
