import { useUserStore } from "#/stores/user-store";
import { type PermissionKey, ENFORCED_PERMISSIONS } from "#/utils/permissions";

export function useHasPermission(permission: PermissionKey): boolean {
  const user = useUserStore((state) => state.user);
  if (!user) return false;
  if (!ENFORCED_PERMISSIONS.has(permission)) return true;
  return user.permissions.includes(permission);
}
