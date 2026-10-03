type RequiredEnvironment =
  | "ENTRA_TENANT_ID"
  | "ENTRA_CLIENT_ID"
  | "ENTRA_CLIENT_SECRET"
  | "ENTRA_REDIRECT_URI"
  | "FOUNDRY_AGENT_ENDPOINT"
  | "APP_BASE_URL";

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
  foundryAgentEndpoint: requireEnvironment("FOUNDRY_AGENT_ENDPOINT"),
  appBaseUrl: requireEnvironment("APP_BASE_URL"),
  foundryAgentName: process.env.FOUNDRY_AGENT_NAME?.trim() || "ask-hr",
  managedIdentityClientId: process.env.AZURE_CLIENT_ID?.trim(),
  sessionCookieName:
    process.env.SESSION_COOKIE_NAME?.trim() || "ask_hr_session",
  isProduction: process.env.NODE_ENV === "production",
};

export const signInScopes = ["openid", "profile", "offline_access"];
