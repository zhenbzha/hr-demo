const required = (name: string): string => {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
};

export interface AdapterConfig {
  botAppId: string;
  foundryAgentResponsesEndpoint: string;
  managedIdentityClientId: string;
}

export const loadAdapterConfig = (): AdapterConfig => ({
  botAppId: required("BOT_APP_ID"),
  foundryAgentResponsesEndpoint: required(
    "FOUNDRY_AGENT_RESPONSES_ENDPOINT",
  ),
  managedIdentityClientId: required("AZURE_CLIENT_ID"),
});
