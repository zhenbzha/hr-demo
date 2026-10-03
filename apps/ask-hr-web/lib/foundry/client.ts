import { AzureCliCredential, ManagedIdentityCredential } from "@azure/identity";

import { config } from "@/lib/config";
import type { ChatTurn } from "@/lib/validation/chat-request";

export type FoundryCitation = {
  label: string;
  location?: string;
  url?: string;
};

export type HostedAgentResponse = {
  answer: string;
  citations: FoundryCitation[];
};

export type AgentStreamEvent =
  | { type: "status"; message: string }
  | { type: "answer"; answer: string }
  | { type: "citations"; citations: FoundryCitation[] }
  | { type: "error"; message: string }
  | { type: "done" };

const foundryCredential = config.isProduction
  ? new ManagedIdentityCredential({
      clientId: config.managedIdentityClientId,
    })
  : new AzureCliCredential({ tenantId: config.entraTenantId });

async function getFoundryAccessToken(): Promise<string> {
  const token = await foundryCredential.getToken(
    "https://ai.azure.com/.default",
  );
  if (!token?.token) {
    throw new Error("Unable to acquire a Microsoft Foundry access token");
  }
  return token.token;
}

export async function streamAgent(
  messages: ChatTurn[],
): Promise<ReadableStream<Uint8Array>> {
  const token = await getFoundryAccessToken();
  const response = await fetch(config.foundryAgentEndpoint, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: config.foundryAgentName,
      input: messages.map(({ role, text }) => ({
        role,
        content: text,
      })),
      store: false,
      stream: true,
    }),
    cache: "no-store",
  });

  if (!response.ok) {
    const body = (await response
      .json()
      .catch(() => null)) as ResponsesBody | null;
    throw new Error(
      `Hosted agent invocation failed (${response.status}): ${
        body?.error?.message || "Unknown agent error"
      }`,
    );
  }
  if (!response.body) {
    throw new Error("Hosted agent response did not include a stream");
  }

  const encoder = new TextEncoder();
  const decoder = new TextDecoder();

  return new ReadableStream<Uint8Array>({
    async start(controller) {
      const emit = (event: AgentStreamEvent) => {
        controller.enqueue(encoder.encode(`${JSON.stringify(event)}\n`));
      };

      let eventBuffer = "";
      let rawAnswer = "";
      let visibleAnswer = "";
      let announcedToolCall = false;

      try {
        const reader = response.body?.getReader();
        if (!reader) {
          throw new Error("Hosted agent stream reader was unavailable");
        }

        while (true) {
          const { done, value } = await reader.read();
          if (done) {
            break;
          }

          eventBuffer += decoder.decode(value, { stream: true });
          const blocks = eventBuffer.split(/\r?\n\r?\n/);
          eventBuffer = blocks.pop() ?? "";

          for (const block of blocks) {
            const data = block
              .split(/\r?\n/)
              .filter((line) => line.startsWith("data:"))
              .map((line) => line.slice(5).trimStart())
              .join("\n");
            if (!data || data === "[DONE]") {
              continue;
            }

            const event = JSON.parse(data) as ResponsesStreamEvent;
            if (
              event.type === "response.function_call_arguments.delta" &&
              !announcedToolCall
            ) {
              announcedToolCall = true;
              emit({
                type: "status",
                message: "Reviewing regional and Global policies",
              });
            }
            if (
              event.type === "response.output_text.delta" &&
              typeof event.delta === "string"
            ) {
              rawAnswer += event.delta;
              const nextVisibleAnswer = stripCitationLines(rawAnswer);
              if (nextVisibleAnswer !== visibleAnswer) {
                visibleAnswer = nextVisibleAnswer;
                emit({ type: "answer", answer: visibleAnswer });
              }
            }
            if (event.type === "response.failed") {
              throw new Error(
                event.response?.error?.message ||
                  "Hosted agent streaming response failed",
              );
            }
          }
        }

        if (!rawAnswer.trim()) {
          throw new Error("Hosted agent response did not include output text");
        }

        emit({ type: "citations", citations: extractCitations(rawAnswer) });
        emit({ type: "done" });
      } catch (error) {
        emit({
          type: "error",
          message:
            error instanceof Error
              ? error.message
              : "The HR assistant is temporarily unavailable.",
        });
      } finally {
        controller.close();
      }
    },
  });
}

type ResponsesBody = {
  error?: { message?: string };
};

type ResponsesStreamEvent = {
  type?: string;
  delta?: string;
  response?: { error?: { message?: string } };
};

function extractCitations(answer: string): FoundryCitation[] {
  const citations = new Map<string, FoundryCitation>();
  const english =
    /Source:\s*(?:[“"]([^”"]+)[”"]|([^,\n]+?))\s*,?\s*p(?:p)?\.\s*(\d+)(?:\s*[-–]\s*(\d+))?/gi;
  const chinese =
    /來源[:：]?\s*(?:[《“"]([^》”"]+)[》”"]|([^，,\n]+?))[^第]*第\s*(\d+)\s*頁/gi;

  for (const match of answer.matchAll(english)) {
    const label = (match[1] || match[2]).trim();
    const location = match[4]
      ? `Pages ${match[3]}–${match[4]}`
      : `Page ${match[3]}`;
    citations.set(`${label}:${location}`, {
      label,
      location,
    });
  }
  for (const match of answer.matchAll(chinese)) {
    const label = (match[1] || match[2]).trim();
    const location = `第 ${match[3]} 頁`;
    citations.set(`${label}:${location}`, {
      label,
      location,
    });
  }

  return [...citations.values()];
}

function stripCitationLines(answer: string): string {
  const citationLine = /^\s*[*_]*(?:Source:|來源[:：])/i;
  const sourceHeading = /^\s*(?:sources?|來源)[:：]?\s*$/i;

  return answer
    .split(/\r?\n/)
    .filter((line) => !citationLine.test(line) && !sourceHeading.test(line))
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
