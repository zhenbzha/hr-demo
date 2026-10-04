import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { formatAskHrResponse } from "../src/format-response.js";

describe("formatAskHrResponse", () => {
  it("returns only the answer when citations are absent", () => {
    assert.equal(
      formatAskHrResponse({
        answer: "Answer",
        citations: [],
        responseId: "resp-1",
      }),
      "Answer",
    );
  });

  it("formats Teams source links", () => {
    assert.equal(
      formatAskHrResponse({
        answer: "Answer",
        responseId: "resp-1",
        citations: [
          {
            label: "India HR Leave Policy",
            location: "Page 2",
            url: "https://example.test/reference.pdf",
          },
        ],
      }),
      "Answer\n\nSources:\n1. India HR Leave Policy — Page 2 (https://example.test/reference.pdf)",
    );
  });
});
