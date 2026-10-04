import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { TokenCredential } from "@azure/identity";

import { FoundryResponsesClient } from "../src/foundry-client.js";

class TestCredential implements TokenCredential {
  async getToken(): Promise<{
    token: string;
    expiresOnTimestamp: number;
  }> {
    return {
      token: "foundry-token",
      expiresOnTimestamp: Date.now() + 60_000,
    };
  }
}

describe("FoundryResponsesClient", () => {
  it("separates workload and delegated user tokens", async () => {
    let capturedRequest: Request | undefined;
    const client = new FoundryResponsesClient({
      endpoint: "https://example.test/responses",
      managedIdentityClientId: "managed-identity-client-id",
      credential: new TestCredential(),
      fetchImpl: async (input, init): Promise<Response> => {
        capturedRequest = new Request(input, init);
        return completedResponse();
      },
    });

    await client.ask({
      question: "Question",
      searchToken: "search-token",
      userObjectId: "82dd50b7-bf39-499e-9ca4-cb211ebd0481",
      previousResponseId: "resp-0",
    });

    const expectedAuthorization = ["Bearer", "foundry-token"].join(" ");
    assert.equal(
      capturedRequest?.headers.get("authorization"),
      expectedAuthorization,
    );
    assert.equal(
      capturedRequest?.headers.get("x-client-search-authorization"),
      "search-token",
    );
    assert.equal(
      capturedRequest?.headers.get("x-ms-user-identity"),
      "82dd50b7-bf39-499e-9ca4-cb211ebd0481",
    );
    assert.deepEqual(await capturedRequest?.json(), {
      input: "Question",
      stream: false,
      background: false,
      store: true,
      previous_response_id: "resp-0",
    });
  });

  it("fails closed when Foundry returns an error", async () => {
    const client = new FoundryResponsesClient({
      endpoint: "https://example.test/responses",
      managedIdentityClientId: "managed-identity-client-id",
      credential: new TestCredential(),
      fetchImpl: async (): Promise<Response> =>
        new Response("forbidden", { status: 403 }),
    });

    await assert.rejects(
      client.ask({
        question: "Question",
        searchToken: "search-token",
        userObjectId: "user-id",
      }),
      /non-JSON response \(403\)/,
    );
  });

  it("does not propagate an upstream token-like error message", async () => {
    const client = new FoundryResponsesClient({
      endpoint: "https://example.test/responses",
      managedIdentityClientId: "managed-identity-client-id",
      credential: new TestCredential(),
      fetchImpl: async (): Promise<Response> =>
        Response.json(
          {
            status: "failed",
            error: { message: "search-token" },
          },
          { status: 403 },
        ),
    });

    await assert.rejects(
      client.ask({
        question: "Question",
        searchToken: "search-token",
        userObjectId: "user-id",
      }),
      (error: unknown) =>
        error instanceof Error &&
        error.message === "Ask HR invocation failed (403)" &&
        !error.message.includes("search-token"),
    );
  });

  it("reports an explicit timeout error", async () => {
    const client = new FoundryResponsesClient({
      endpoint: "https://example.test/responses",
      managedIdentityClientId: "managed-identity-client-id",
      credential: new TestCredential(),
      requestTimeoutMs: 25,
      fetchImpl: async (_input, init): Promise<Response> => {
        assert.ok(init?.signal);
        const error = new Error("Request aborted");
        error.name = "TimeoutError";
        throw error;
      },
    });

    await assert.rejects(
      client.ask({
        question: "Question",
        searchToken: "search-token",
        userObjectId: "user-id",
      }),
      /timed out after 25 ms/,
    );
  });

  it("parses output text, response ID, and URL citations", async () => {
    const client = new FoundryResponsesClient({
      endpoint: "https://example.test/responses",
      managedIdentityClientId: "managed-identity-client-id",
      credential: new TestCredential(),
      fetchImpl: async (): Promise<Response> => completedResponse(),
    });

    const result = await client.ask({
      question: "Question",
      searchToken: "search-token",
      userObjectId: "user-id",
    });

    assert.deepEqual(result, {
      answer: "Answer",
      responseId: "resp-1",
      citations: [
        {
          label: "India HR Leave Policy",
          location: "Page 2",
          url: "https://example.test/reference.pdf",
        },
      ],
    });
  });
});

const completedResponse = (): Response =>
  Response.json({
    id: "resp-1",
    status: "completed",
    output: [
      {
        type: "message",
        content: [
          {
            type: "output_text",
            text: "Answer",
            annotations: [
              {
                type: "url_citation",
                title: "India HR Leave Policy - Page 2",
                url: "https://example.test/reference.pdf",
              },
            ],
          },
        ],
      },
    ],
  });
