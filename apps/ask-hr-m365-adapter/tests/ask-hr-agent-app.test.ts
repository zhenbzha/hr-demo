import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  Activity,
  ActivityTypes,
} from "@microsoft/agents-activity";
import type {
  CloudAdapter,
  TurnContext,
} from "@microsoft/agents-hosting";

import {
  AskHrAgentApplication,
  shouldSendWelcome,
} from "../src/ask-hr-agent-app.js";
import type { AskHrAgentClient } from "../src/foundry-client.js";

const client: AskHrAgentClient = {
  ask: async () => ({
    answer: "Answer",
    citations: [],
    responseId: "resp-1",
  }),
};

describe("AskHrAgentApplication", () => {
  it("enables long-running messages", () => {
    const adapter = {
      setAgentName: (): void => undefined,
    } as unknown as CloudAdapter;
    const agent = new AskHrAgentApplication(client, "app-id", adapter);
    assert.equal(agent.options.longRunningMessages, true);
  });

  it("replaces the SDK default turn error response", async () => {
    const sentMessages: string[] = [];
    const adapter = {
      setAgentName: (): void => undefined,
      onTurnError: async (): Promise<void> => undefined,
    } as unknown as CloudAdapter;
    new AskHrAgentApplication(client, "app-id", adapter);
    const context = {
      activity: { channelData: {} },
      sendActivity: async (activity: { text?: string }): Promise<void> => {
        sentMessages.push(activity.text ?? "");
      },
    } as unknown as TurnContext;

    await adapter.onTurnError(context, new Error("Unexpected failure"));
    assert.deepEqual(sentMessages, [
      "I couldn't verify your access to the requested HR policy information.",
    ]);
  });
});

describe("shouldSendWelcome", () => {
  it("ignores an update that only adds the bot", () => {
    const activity = Activity.fromObject({
      type: ActivityTypes.ConversationUpdate,
      recipient: { id: "bot-id" },
      membersAdded: [{ id: "bot-id" }],
    });
    assert.equal(shouldSendWelcome(activity), false);
  });

  it("welcomes a newly added user once", () => {
    const activity = Activity.fromObject({
      type: ActivityTypes.ConversationUpdate,
      recipient: { id: "bot-id" },
      membersAdded: [{ id: "user-id" }, { id: "bot-id" }],
    });
    assert.equal(shouldSendWelcome(activity), true);
  });

  it("ignores updates without added members", () => {
    const activity = Activity.fromObject({
      type: ActivityTypes.ConversationUpdate,
      recipient: { id: "bot-id" },
    });
    assert.equal(shouldSendWelcome(activity), false);
  });
});
