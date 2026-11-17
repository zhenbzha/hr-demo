export type ChatTurn = {
  role: "user" | "assistant";
  text: string;
};

export type ChatRequest = {
  message: string;
  previousResponseId?: string;
};

export function parseChatRequest(value: unknown): ChatRequest {
  if (!value || typeof value !== "object") {
    throw new Error("Request body must be a JSON object");
  }
  const message = (value as { message?: unknown }).message;
  if (typeof message !== "string" || !message.trim()) {
    throw new Error("message must be a non-empty string");
  }
  if (message.length > 4_000) {
    throw new Error("message is too long");
  }
  const previousResponseId = (value as { previousResponseId?: unknown })
    .previousResponseId;
  if (
    previousResponseId !== undefined &&
    (typeof previousResponseId !== "string" ||
      !/^(?:ca)?resp_[A-Za-z0-9_-]+$/.test(previousResponseId))
  ) {
    throw new Error("previousResponseId is invalid");
  }
  return {
    message: message.trim(),
    previousResponseId,
  };
}
