export const LOCAL_STORAGE_KEYS = {
  LOGIN_METHOD: "openhands_login_method",
};

export enum LoginMethod {
  GITHUB = "github",
  GITLAB = "gitlab",
  BITBUCKET = "bitbucket",
  BITBUCKET_DATA_CENTER = "bitbucket_data_center",
  AZURE_DEVOPS = "azure_devops",
  ENTERPRISE_SSO = "enterprise_sso",
}

export const setLoginMethod = (method: LoginMethod): void => {
  localStorage.setItem(LOCAL_STORAGE_KEYS.LOGIN_METHOD, method);
};

export const getLoginMethod = (): LoginMethod | null => {
  const method = localStorage.getItem(LOCAL_STORAGE_KEYS.LOGIN_METHOD);
  return method as LoginMethod | null;
};

export const clearLoginData = (): void => {
  localStorage.removeItem(LOCAL_STORAGE_KEYS.LOGIN_METHOD);
};
