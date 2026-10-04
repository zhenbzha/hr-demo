import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { parseChatRequest } from "@/lib/validation/chat-request";

describe("parseChatRequest", () => {
  it("accepts a current question and previous response ID", () => {
    assert.deepEqual(
      parseChatRequest({
        message: " Question ",
        previousResponseId: "resp_abc-123",
      }),
      {
        message: "Question",
        previousResponseId: "resp_abc-123",
      },
    );
  });

  it("rejects malformed previous response IDs", () => {
    assert.throws(
      () =>
        parseChatRequest({
          message: "Question",
          previousResponseId: "not-a-response",
        }),
      /previousResponseId is invalid/,
    );
  });
});
