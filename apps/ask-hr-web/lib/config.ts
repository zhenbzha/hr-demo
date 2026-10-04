import "server-only";

type RequiredEnvironment =
  | "ENTRA_TENANT_ID"
  | "ENTRA_CLIENT_ID"
  | "ENTRA_CLIENT_SECRET"
  | "NEXTAUTH_SECRET"
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
  nextAuthSecret: requireEnvironment("NEXTAUTH_SECRET"),
  foundryAgentEndpoint: requireEnvironment("FOUNDRY_AGENT_ENDPOINT"),
  storageDfsEndpoint: requireEnvironment("AZURE_STORAGE_DFS_ENDPOINT"),
  storageFileSystem: requireEnvironment("AZURE_STORAGE_FILE_SYSTEM"),
  foundryAgentName: process.env.FOUNDRY_AGENT_NAME?.trim() || "ask-hr",
  managedIdentityClientId: process.env.AZURE_CLIENT_ID?.trim(),
  isProduction: process.env.NODE_ENV === "production",
};
