import "server-only";

type RequiredEnvironment =
  | "ENTRA_TENANT_ID"
  | "ENTRA_CLIENT_ID"
  | "ENTRA_CLIENT_SECRET"
  | "ENTRA_REDIRECT_URI"
  | "APP_BASE_URL"
  | "FOUNDRY_AGENT_ENDPOINT"
  | "AZURE_STORAGE_DFS_ENDPOINT"
  | "AZURE_STORAGE_FILE_SYSTEM";

function requireEnvironment(name: RequiredEnvironment): string {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

export const config = {
  entraTenantId: requireEnvironment("ENTRA_TENANT_ID"),
  entraClientId: requireEnvironment("ENTRA_CLIENT_ID"),
  entraClientSecret: requireEnvironment("ENTRA_CLIENT_SECRET"),
  entraRedirectUri: requireEnvironment("ENTRA_REDIRECT_URI"),
  appBaseUrl: requireEnvironment("APP_BASE_URL"),
  foundryAgentEndpoint: requireEnvironment("FOUNDRY_AGENT_ENDPOINT"),
  storageDfsEndpoint: requireEnvironment("AZURE_STORAGE_DFS_ENDPOINT"),
  storageFileSystem: requireEnvironment("AZURE_STORAGE_FILE_SYSTEM"),
  foundryAgentName: process.env.FOUNDRY_AGENT_NAME?.trim() || "ask-hr",
  managedIdentityClientId: process.env.AZURE_CLIENT_ID?.trim(),
  sessionCookieName:
    process.env.SESSION_COOKIE_NAME?.trim() || "ask_hr_session",
  isProduction: process.env.NODE_ENV === "production",
};

export const signInScopes = [
  "openid",
  "profile",
  "offline_access",
  "https://search.azure.com/.default",
];

export const searchScopes = ["https://search.azure.com/.default"];
