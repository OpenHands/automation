import { generateAuthUrl } from "#/utils/generate-auth-url";

interface UseAuthUrlConfig {
  identityProvider: string;
}

export const useAuthUrl = ({ identityProvider }: UseAuthUrlConfig): string =>
  generateAuthUrl(identityProvider, new URL(window.location.href));
