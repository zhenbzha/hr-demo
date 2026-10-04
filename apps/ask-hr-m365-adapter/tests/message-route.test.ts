import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { RequestHandler } from "express";
import type {
  AgentApplication,
  CloudAdapter,
  TurnState,
} from "@microsoft/agents-hosting";

import { createMessageRouteHandlers } from "../src/message-route.js";

describe("message route security order", () => {
  it("authenticates before status delivery and agent processing", async () => {
    const events: string[] = [];
    const request = { body: { id: "activity-id" } };
    const response = {};
    const context = {
      activity: { type: "message", channelData: {} },
      sendActivity: async (): Promise<{ id: string }> => {
        events.push("status");
        return { id: "status-id" };
      },
    };
    const adapter = {
      process: async (
        _request: unknown,
        _response: unknown,
        callback: (turnContext: unknown) => Promise<void>,
      ): Promise<void> => {
        events.push("process");
        await callback(context);
      },
    } as unknown as CloudAdapter;
    const agent = {
      run: async (): Promise<void> => {
        events.push("agent");
      },
    } as unknown as AgentApplication<TurnState>;
    const authorize = (): RequestHandler => (_request, _response, next) => {
      events.push("authenticate");
      next();
    };
    const handlers = createMessageRouteHandlers({
      adapter,
      agent,
      authConfig: {},
      authorize,
    });

    await invokeHandler(handlers[0], request, response);
    await invokeHandler(handlers[1], request, response);
    await invokeHandler(handlers[2], request, response);

    assert.deepEqual(events, [
      "authenticate",
      "process",
      "status",
      "agent",
    ]);
  });
});

const invokeHandler = async (
  handler: RequestHandler,
  request: object,
  response: object,
): Promise<void> => {
  await new Promise<void>((resolve, reject) => {
    const result = handler(
      request as Parameters<RequestHandler>[0],
      response as Parameters<RequestHandler>[1],
      (error?: unknown) => (error ? reject(error) : resolve()),
    );
    if (result instanceof Promise) {
      result.then(resolve, reject);
    }
  });
};
