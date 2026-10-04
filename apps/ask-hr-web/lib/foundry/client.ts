import "server-only";

import { AzureCliCredential, ManagedIdentityCredential } from "@azure/identity";

import { config } from "@/lib/config";
import type { RichCitation } from "@/lib/references/types";
import type { ChatTurn } from "@/lib/validation/chat-request";

export type FoundryCitation = RichCitation;

export type HostedAgentResponse = {
  answer: string;
  citations: FoundryCitation[];
};

export type AgentStreamEvent =
  | { type: "status"; message: string }
  | { type: "answer"; answer: string }
  | { type: "citations"; citations: FoundryCitation[] }
  | { type: "error"; code: "assistant_unavailable"; message: string }
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
  agentSessionId: string,
): Promise<ReadableStream<Uint8Array>> {
  const requestId = crypto.randomUUID();
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
      agent_session_id: agentSessionId,
      store: false,
      stream: true,
    }),
    cache: "no-store",
    signal: AbortSignal.timeout(120_000),
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
      const retrievedReferences = new Map<string, RetrievedReference>();

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
              event.type === "response.output_item.done" &&
              event.item?.type === "function_call_output" &&
              typeof event.item.output === "string"
            ) {
              for (const reference of parseToolReferences(event.item.output)) {
                const existing = retrievedReferences.get(reference.chunkId);
                if (
                  !existing ||
                  reference.rerankerScore > existing.rerankerScore
                ) {
                  retrievedReferences.set(reference.chunkId, reference);
                }
              }
            }
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

        emit({
          type: "citations",
          citations: buildCitations(rawAnswer, retrievedReferences),
        });
        emit({ type: "done" });
      } catch (error) {
        console.error("Hosted agent stream failed", { requestId, error });
        emit({
          type: "error",
          code: "assistant_unavailable",
          message:
            "The HR assistant is temporarily unavailable. Please try again.",
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
  item?: {
    type?: string;
    output?: string;
  };
  response?: { error?: { message?: string } };
};

type RetrievedReference = {
  chunkId: string;
  documentId: string;
  documentTitle: string;
  region: string;
  language: string;
  version: string;
  chunkText: string;
  pageStart: number;
  pageEnd: number;
  rerankerScore: number;
};

type ToolOutput = {
  evidence?: Array<{
    result?: {
      references?: Array<{
        rerankerScore?: number;
        sourceData?: {
          chunk_id?: string;
          document_id?: string;
          document_title?: string;
          region?: string;
          language?: string;
          version?: string;
          chunk_text?: string;
          page_start?: number;
          page_end?: number;
        };
      }>;
    };
  }>;
};

function parseToolReferences(output: string): RetrievedReference[] {
  let parsed: ToolOutput;
  try {
    parsed = JSON.parse(output) as ToolOutput;
  } catch {
    return [];
  }

  return (parsed.evidence ?? []).flatMap((evidence) =>
    (evidence.result?.references ?? []).flatMap((reference) => {
      const source = reference.sourceData;
      const pageStart = source?.page_start;
      const pageEnd = source?.page_end;
      if (
        !source?.chunk_id ||
        !source.document_id ||
        !source.document_title ||
        !source.region ||
        !source.language ||
        !source.version ||
        !source.chunk_text ||
        typeof pageStart !== "number" ||
        typeof pageEnd !== "number" ||
        !Number.isInteger(pageStart) ||
        !Number.isInteger(pageEnd)
      ) {
        return [];
      }
      return [
        {
          chunkId: source.chunk_id,
          documentId: source.document_id,
          documentTitle: source.document_title,
          region: source.region,
          language: source.language,
          version: source.version,
          chunkText: source.chunk_text,
          pageStart,
          pageEnd,
          rerankerScore: reference.rerankerScore ?? 0,
        },
      ];
    }),
  );
}

function buildCitations(
  answer: string,
  references: Map<string, RetrievedReference>,
): FoundryCitation[] {
  const referenceIds = [
    ...answer.matchAll(/^\s*Reference:\s*([a-f0-9]{20,})\s*[*_.]*\s*$/gim),
  ].map((match) => match[1]);

  const grouped = new Map<
    string,
    { primary: RetrievedReference; chunkIds: string[]; excerpts: string[] }
  >();
  for (const referenceId of referenceIds) {
    const reference = references.get(referenceId);
    if (!reference) {
      continue;
    }
    const key = `${reference.documentId}:${reference.pageStart}:${reference.pageEnd}`;
    const existing = grouped.get(key);
    if (existing) {
      if (!existing.chunkIds.includes(reference.chunkId)) {
        existing.chunkIds.push(reference.chunkId);
        existing.excerpts.push(reference.chunkText);
      }
      continue;
    }
    grouped.set(key, {
      primary: reference,
      chunkIds: [reference.chunkId],
      excerpts: [reference.chunkText],
    });
  }

  if (!grouped.size) {
    if (referenceIds.length) {
      console.error(
        "Agent references did not match any retrieved Search source data",
        { referenceIds },
      );
    }
    return [];
  }

  return [...grouped.values()].map(({ primary, chunkIds, excerpts }) => {
    const location =
      primary.pageStart === primary.pageEnd
        ? `Page ${primary.pageStart}`
        : `Pages ${primary.pageStart}–${primary.pageEnd}`;
    const excerpt = excerpts.join("\n\n");
    return {
      label: primary.documentTitle,
      location,
      chunkIds,
      documentId: primary.documentId,
      version: primary.version,
      pageStart: primary.pageStart,
      pageEnd: primary.pageEnd,
      excerpt,
      region: primary.region,
      language: primary.language,
      pdfUrl: `/api/references/${encodeURIComponent(
        primary.documentId,
      )}/${encodeURIComponent(primary.version)}/pdf`,
      contextUrl: `/api/references/${encodeURIComponent(
        primary.documentId,
      )}/${encodeURIComponent(primary.version)}/context?pageStart=${
        primary.pageStart
      }&pageEnd=${primary.pageEnd}`,
    };
  });
}

function stripCitationLines(answer: string): string {
  const citationLine = /^\s*[*_]*Reference:\s*[a-f0-9]{20,}\s*[*_.]*\s*$/i;

  return answer
    .split(/\r?\n/)
    .filter((line) => !citationLine.test(line))
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
