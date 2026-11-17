import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { parseChatRequest } from "@/lib/validation/chat-request";

describe("parseChatRequest", () => {
  it("accepts standard and Foundry-hosted response IDs unchanged", () => {
    for (const previousResponseId of [
      "resp_abc-123",
      "caresp_0123456789abcdef00ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef",
    ]) {
      assert.deepEqual(
        parseChatRequest({ message: " Question ", previousResponseId }),
        { message: "Question", previousResponseId },
      );
    }
  });

  it("rejects malformed previous response IDs", () => {
    for (const previousResponseId of [
      null,
      42,
      "",
      "resp_",
      "caresp_",
      "not-a-response",
      "caresp_test/invalid",
      "caresp_test invalid",
    ]) {
      assert.throws(
        () => parseChatRequest({ message: "Question", previousResponseId }),
        /previousResponseId is invalid/,
      );
    }
  });
});
