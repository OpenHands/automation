import { useEffect } from "react";
import { useIsAuthed } from "./use-is-authed";
import { getLoginMethod, LoginMethod } from "#/utils/local-storage";
import { useAuthUrl } from "./use-auth-url";

/**
 * Hook to automatically log in the user if they have a login method
 * stored in localStorage. When unauthenticated with a stored login method,
 * redirects to the OAuth provider for seamless re-authentication.
 */
export const useAutoLogin = () => {
  const { data: isAuthed, isLoading: isAuthLoading } = useIsAuthed();

  const loginMethod = getLoginMethod();

  const githubAuthUrl = useAuthUrl({ identityProvider: "github" });
  const gitlabAuthUrl = useAuthUrl({ identityProvider: "gitlab" });
  const bitbucketAuthUrl = useAuthUrl({ identityProvider: "bitbucket" });
  const bitbucketDataCenterUrl = useAuthUrl({
    identityProvider: "bitbucket_data_center",
  });
  const enterpriseSsoUrl = useAuthUrl({
    identityProvider: "enterprise_sso",
  });

  useEffect(() => {
    if (isAuthLoading) return;
    if (isAuthed) return;
    if (!loginMethod) return;

    let authUrl: string | null = null;
    if (loginMethod === LoginMethod.GITHUB) {
      authUrl = githubAuthUrl;
    } else if (loginMethod === LoginMethod.GITLAB) {
      authUrl = gitlabAuthUrl;
    } else if (loginMethod === LoginMethod.BITBUCKET) {
      authUrl = bitbucketAuthUrl;
    } else if (loginMethod === LoginMethod.BITBUCKET_DATA_CENTER) {
      authUrl = bitbucketDataCenterUrl;
    } else if (loginMethod === LoginMethod.ENTERPRISE_SSO) {
      authUrl = enterpriseSsoUrl;
    }

    if (authUrl) {
      const url = new URL(authUrl);
      url.searchParams.append("login_method", loginMethod);
      window.location.href = url.toString();
    }
  }, [
    isAuthed,
    isAuthLoading,
    loginMethod,
    githubAuthUrl,
    gitlabAuthUrl,
    bitbucketAuthUrl,
    bitbucketDataCenterUrl,
    enterpriseSsoUrl,
  ]);
};
