import type { RequestHandler } from "express";
import {
  authorizeJWT,
  type CloudAdapter,
  type TurnState,
} from "@microsoft/agents-hosting";
import type { AgentApplication } from "@microsoft/agents-hosting";

import { runWithImmediateStatus } from "./run-turn.js";

type AuthConfig = Parameters<typeof authorizeJWT>[0];
type AuthorizeFactory = (authConfig: AuthConfig) => RequestHandler;

interface MessageRouteOptions {
  adapter: CloudAdapter;
  agent: AgentApplication<TurnState>;
  authConfig: AuthConfig;
  authorize?: AuthorizeFactory;
}

export const createMessageRouteHandlers = (
  options: MessageRouteOptions,
): [RequestHandler, RequestHandler, RequestHandler] => {
  const requestReceivedAt = new WeakMap<object, number>();
  const recordRequest: RequestHandler = (request, _response, next) => {
    requestReceivedAt.set(request, performance.now());
    next();
  };
  const authenticate = (options.authorize ?? authorizeJWT)(
    options.authConfig,
  );
  const processRequest: RequestHandler = async (request, response) => {
    const channelAuthenticatedAt = performance.now();
    const receivedAt = requestReceivedAt.get(request);
    console.info("Channel request authenticated", {
      activityId: getActivityId(request.body),
      channelAuthMs:
        receivedAt === undefined
          ? undefined
          : Math.round(channelAuthenticatedAt - receivedAt),
    });
    await options.adapter.process(
      request,
      response,
      async (context): Promise<void> => {
        await runWithImmediateStatus(context, options.agent, {
          channelAuthenticatedAt,
        });
      },
    );
  };
  return [recordRequest, authenticate, processRequest];
};

const getActivityId = (body: unknown): string | undefined => {
  if (!body || typeof body !== "object" || !("id" in body)) {
    return undefined;
  }
  return typeof body.id === "string" ? body.id : undefined;
};
