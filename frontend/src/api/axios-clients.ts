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

const handle401 = (error: unknown) => {
  if (axios.isAxiosError(error) && error.response?.status === 401) {
    const redirectUrl = encodeURIComponent(window.location.pathname);
    window.location.href = `/login?redirect=${redirectUrl}`;
  }
  return Promise.reject(error);
};

automationApi.interceptors.response.use((response) => response, handle401);
openhandsApi.interceptors.response.use((response) => response, handle401);
