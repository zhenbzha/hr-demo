import type { AskHrAgentResponse } from "./foundry-client.js";

export const formatAskHrResponse = (
  response: AskHrAgentResponse,
): string => {
  if (response.citations.length === 0) {
    return response.answer;
  }

  const sources = response.citations
    .map((citation, index) => {
      const location = citation.location ? ` — ${citation.location}` : "";
      const link = citation.url ? ` (${citation.url})` : "";
      return `${index + 1}. ${citation.label}${location}${link}`;
    })
    .join("\n");

  return `${response.answer}\n\nSources:\n${sources}`;
};
