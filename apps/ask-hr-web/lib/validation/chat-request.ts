export type ChatTurn = {
  role: "user" | "assistant";
  text: string;
};

export function parseChatMessages(value: unknown): ChatTurn[] {
  if (!value || typeof value !== "object") {
    throw new Error("Request body must be a JSON object");
  }
  const messages = (value as { messages?: unknown }).messages;
  if (!Array.isArray(messages) || messages.length === 0) {
    throw new Error("messages must be a non-empty array");
  }
  if (messages.length > 30) {
    throw new Error("messages cannot exceed 30 turns");
  }

  return messages.map((item, index) => {
    if (!item || typeof item !== "object") {
      throw new Error(`messages[${index}] must be an object`);
    }
    const role = (item as { role?: unknown }).role;
    const text = (item as { text?: unknown }).text;
    if (role !== "user" && role !== "assistant") {
      throw new Error(`messages[${index}].role is invalid`);
    }
    if (typeof text !== "string" || !text.trim()) {
      throw new Error(`messages[${index}].text must be non-empty`);
    }
    if (text.length > 4_000) {
      throw new Error(`messages[${index}].text is too long`);
    }
    return { role, text: text.trim() };
  });
}
