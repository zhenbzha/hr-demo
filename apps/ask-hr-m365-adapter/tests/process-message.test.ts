import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type {
  AskHrAgentClient,
  FoundryRequest,
} from "../src/foundry-client.js";
import { processAskHrMessage } from "../src/process-message.js";

class RecordingClient implements AskHrAgentClient {
  request: FoundryRequest | undefined;

  async ask(request: FoundryRequest): Promise<{
    answer: string;
    citations: [];
    responseId: string;
  }> {
    this.request = request;
    return { answer: "Answer", citations: [], responseId: "resp-1" };
  }
}

describe("processAskHrMessage", () => {
  it("fails closed when the trusted user object ID is missing", async () => {
    const client = new RecordingClient();

    await assert.rejects(
      processAskHrMessage({
        question: "Question",
        userObjectId: undefined,
        acquireSearchToken: async (): Promise<string> => "token",
        askHrAgentClient: client,
      }),
      /trusted Entra user object ID/,
    );
    assert.equal(client.request, undefined);
  });

  it("fails closed when OBO does not return a Search token", async () => {
    const client = new RecordingClient();

    await assert.rejects(
      processAskHrMessage({
        question: "Question",
        userObjectId: "user-id",
        acquireSearchToken: async (): Promise<undefined> => undefined,
        askHrAgentClient: client,
      }),
      /Unable to acquire an Azure AI Search token/,
    );
    assert.equal(client.request, undefined);
  });

  it("passes only the verified identity and OBO token downstream", async () => {
    const client = new RecordingClient();

    await processAskHrMessage({
      question: " Question ",
      userObjectId: "user-id",
      previousResponseId: "resp-0",
      acquireSearchToken: async (): Promise<string> => "search-token",
      askHrAgentClient: client,
    });

    assert.deepEqual(client.request, {
      question: "Question",
      searchToken: "search-token",
      userObjectId: "user-id",
      previousResponseId: "resp-0",
    });
  });
});
