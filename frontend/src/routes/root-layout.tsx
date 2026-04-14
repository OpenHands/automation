import React from "react";
import { Outlet } from "react-router";
import { useIsAuthed } from "#/hooks/use-is-authed";
import { useMe } from "#/hooks/use-me";
import { useAutoLogin } from "#/hooks/use-auto-login";
import { useCrossTabState } from "#/hooks/use-cross-tab-state";
import { useLanguageSync } from "#/hooks/use-language-sync";
import { useOrgSync } from "#/hooks/use-org-sync";
import { useThemeSync } from "#/hooks/use-theme-sync";
import { ReauthModal } from "#/components/reauth-modal";
import { LOCAL_STORAGE_KEYS } from "#/utils/local-storage";

export { ErrorBoundary } from "#/components/error-boundary";

export default function RootLayout() {
  const {
    data: isAuthed,
    isLoading: isAuthLoading,
    isError: isAuthError,
    isFetching: isFetchingAuth,
  } = useIsAuthed();

  const { data: user } = useMe(isAuthed === true);
  useAutoLogin();
  useLanguageSync(user);
  useOrgSync(user?.org_id);
  useThemeSync();

  const checkLoginMethodExists = React.useCallback(
    () =>
      typeof window !== "undefined" &&
      localStorage.getItem(LOCAL_STORAGE_KEYS.LOGIN_METHOD) !== null,
    [],
  );

  const [loginMethodExists, setLoginMethodExists] = React.useState(
    checkLoginMethodExists(),
  );

  const handleStorageChange = React.useCallback(
    (event: StorageEvent) => {
      if (event.key === LOCAL_STORAGE_KEYS.LOGIN_METHOD) {
        if (event.newValue === null) {
          const redirectUrl = encodeURIComponent(window.location.pathname);
          window.location.href = `/login?redirect=${redirectUrl}`;
          return;
        }
        setLoginMethodExists(checkLoginMethodExists());
      }
    },
    [checkLoginMethodExists],
  );

  const handleWindowFocus = React.useCallback(() => {
    setLoginMethodExists(checkLoginMethodExists());
  }, [checkLoginMethodExists]);

  useCrossTabState(handleStorageChange, handleWindowFocus);

  React.useEffect(() => {
    setLoginMethodExists(checkLoginMethodExists());
  }, [isAuthed, checkLoginMethodExists]);

  const shouldRedirectToLogin =
    !isAuthLoading && !isAuthed && !isAuthError && !loginMethodExists;

  React.useEffect(() => {
    if (shouldRedirectToLogin) {
      const redirectUrl = encodeURIComponent(window.location.pathname);
      window.location.href = `/login?redirect=${redirectUrl}`;
    }
  }, [shouldRedirectToLogin]);

  if (isAuthLoading || shouldRedirectToLogin) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-surface">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-content-muted border-t-white" />
      </div>
    );
  }

  const renderReAuthModal =
    !isAuthed && !isAuthError && !isFetchingAuth && loginMethodExists;

  return (
    <div className="min-h-screen bg-surface text-white">
      {renderReAuthModal && <ReauthModal />}
      <main className="mx-auto max-w-5xl px-8 py-8">
        <Outlet />
      </main>
    </div>
  );
}
