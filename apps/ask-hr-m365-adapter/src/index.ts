import { useAzureMonitor } from "@azure/monitor-opentelemetry";
import {
  authorizeJWT,
  CloudAdapter,
  loadAuthConfigFromEnv,
} from "@microsoft/agents-hosting";
import express from "express";

import { AskHrAgentApplication } from "./ask-hr-agent-app.js";
import { loadAdapterConfig } from "./config.js";
import { FoundryResponsesClient } from "./foundry-client.js";
import { createMessageRouteHandlers } from "./message-route.js";

if (process.env.APPLICATIONINSIGHTS_CONNECTION_STRING) {
  useAzureMonitor();
}

const config = loadAdapterConfig();
const askHrAgentClient = new FoundryResponsesClient({
  endpoint: config.foundryAgentResponsesEndpoint,
  managedIdentityClientId: config.managedIdentityClientId,
});
const authConfig = loadAuthConfigFromEnv();
const adapter = new CloudAdapter(authConfig);
const agent = new AskHrAgentApplication(
  askHrAgentClient,
  config.botAppId,
  adapter,
);
const server = express();

server.use(express.json());
server.post(
  "/api/messages",
  ...createMessageRouteHandlers({
    adapter,
    agent,
    authConfig,
    authorize: authorizeJWT,
  }),
);

server.get("/healthz", (_request: unknown, response: HealthResponse) => {
  response.status(200).json({ status: "healthy" });
});

const port = Number(process.env.PORT ?? 3978);
server.listen(port, () => {
  console.log(`Ask HR Microsoft 365 adapter listening on port ${port}`);
});

interface HealthResponse {
  status(code: number): HealthResponse;
  json(body: { status: string }): void;
}
