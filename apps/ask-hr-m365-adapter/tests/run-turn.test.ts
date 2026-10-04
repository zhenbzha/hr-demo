import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  type Activity,
  ActivityTypes,
} from "@microsoft/agents-activity";
import type {
  AgentApplication,
  TurnContext,
  TurnState,
} from "@microsoft/agents-hosting";

import {
  replaceStatusOrSend,
  runWithImmediateStatus,
  statusActivityIdKey,
} from "../src/run-turn.js";

describe("runWithImmediateStatus", () => {
  it("sends status before running a message turn and preserves its ID", async () => {
    const events: string[] = [];
    const context = {
      activity: { type: ActivityTypes.Message, channelData: {} },
      sendActivity: async (): Promise<{ id: string }> => {
        events.push("status");
        return { id: "status-id" };
      },
    } as unknown as TurnContext;
    const agent = {
      run: async (): Promise<void> => {
        events.push("run");
      },
    } as unknown as AgentApplication<TurnState>;

    await runWithImmediateStatus(context, agent);

    assert.deepEqual(events, ["status", "run"]);
    assert.equal(
      context.activity.channelData?.[statusActivityIdKey],
      "status-id",
    );
  });

  it("runs the turn when status delivery fails", async () => {
    let runCalled = false;
    const context = {
      activity: { type: ActivityTypes.Message, channelData: {} },
      sendActivity: async (): Promise<never> => {
        throw new Error("Connector unavailable");
      },
    } as unknown as TurnContext;
    const agent = {
      run: async (): Promise<void> => {
        runCalled = true;
      },
    } as unknown as AgentApplication<TurnState>;

    await runWithImmediateStatus(context, agent);
    assert.equal(runCalled, true);
  });
});

describe("replaceStatusOrSend", () => {
  it("replaces the existing status activity", async () => {
    const events: string[] = [];
    const context = {
      activity: { channelData: { [statusActivityIdKey]: "status-1" } },
      updateActivity: async (activity: Activity): Promise<void> => {
        assert.equal(activity.id, "status-1");
        events.push("update");
      },
      sendActivity: async (): Promise<void> => {
        events.push("send");
      },
    } as unknown as TurnContext;

    await replaceStatusOrSend(
      context,
      { type: ActivityTypes.Message, text: "Answer" } as Activity,
    );
    assert.deepEqual(events, ["update"]);
  });

  it("sends a new message when replacement fails", async () => {
    const events: string[] = [];
    const context = {
      activity: { channelData: { [statusActivityIdKey]: "status-1" } },
      updateActivity: async (): Promise<never> => {
        events.push("update");
        throw new Error("Activity unavailable");
      },
      sendActivity: async (activity: Activity): Promise<void> => {
        assert.equal(activity.id, undefined);
        events.push("send");
      },
    } as unknown as TurnContext;

    await replaceStatusOrSend(
      context,
      { type: ActivityTypes.Message, text: "Answer" } as Activity,
    );
    assert.deepEqual(events, ["update", "send"]);
  });
});
