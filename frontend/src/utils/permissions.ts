export type PermissionKey = "manage_automations";

// Permissions actively enforced by the UI.
// Empty = all authenticated users have full access (baseline).
// Add "manage_automations" here when backend begins sending it.
export const ENFORCED_PERMISSIONS: ReadonlySet<PermissionKey> = new Set([
  "manage_automations",
]);
