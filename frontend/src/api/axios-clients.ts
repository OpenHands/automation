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
 */
export const openhandsApi = axios.create();
