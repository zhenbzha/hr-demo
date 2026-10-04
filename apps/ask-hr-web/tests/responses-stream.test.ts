import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { parseServerSentEvents } from "@/lib/foundry/responses-stream";

describe("parseServerSentEvents", () => {
  it("parses events split across transport chunks", async () => {
    const encoder = new TextEncoder();
    const chunks = [
      'data: {"type":"response.output_',
      'text.delta","delta":"Hello"}\n\n',
      'data: {"type":"response.completed","response":{"id":"resp_1"}}\n\n',
      "data: [DONE]\n\n",
    ];
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) {
          controller.enqueue(encoder.encode(chunk));
        }
        controller.close();
      },
    });

    const events = [];
    for await (const event of parseServerSentEvents(stream)) {
      events.push(event);
    }

    assert.deepEqual(events, [
      { type: "response.output_text.delta", delta: "Hello" },
      { type: "response.completed", response: { id: "resp_1" } },
    ]);
  });
});
