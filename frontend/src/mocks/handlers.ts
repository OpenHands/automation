import { automationHandlers } from "./automation-handlers";
import { authHandlers } from "./auth-handlers";

export const handlers = [...automationHandlers, ...authHandlers];
