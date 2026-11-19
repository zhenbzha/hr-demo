import "server-only";

import { AzureCliCredential, ManagedIdentityCredential } from "@azure/identity";

import { config } from "@/lib/config";
import { parseServerSentEvents } from "@/lib/foundry/responses-stream";
import type { RichCitation } from "@/lib/references/types";

export type HostedAgentResponse = {
  answer: string;
  citations: RichCitation[];
  responseId: string;
};

export type HostedAgentStreamEvent =
  | { type: "text_delta"; delta: string }
  | ({ type: "completed" } & HostedAgentResponse);

type InvokeAgentInput = {
  message: string;
  searchToken?: string;
  userObjectId: string;
  previousResponseId?: string;
  signal?: AbortSignal;
};

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

export async function invokeAgentStream(
  input: InvokeAgentInput,
): Promise<AsyncGenerator<HostedAgentStreamEvent>> {
  const foundryToken = await getFoundryAccessToken();
  const response = await fetch(config.foundryAgentEndpoint, {
    method: "POST",
    headers: {
      Authorization: ["Bearer", foundryToken].join(" "),
      "Content-Type": "application/json",
      ...(input.searchToken
        ? { "x-client-search-authorization": input.searchToken }
        : {}),
      "x-ms-user-identity": input.userObjectId,
    },
    body: JSON.stringify({
      input: input.message,
      stream: true,
      background: false,
      store: true,
      previous_response_id: input.previousResponseId,
    }),
    cache: "no-store",
    redirect: "error",
    signal: input.signal
      ? AbortSignal.any([input.signal, AbortSignal.timeout(120_000)])
      : AbortSignal.timeout(120_000),
  });

  if (!response.ok) {
    const payload = (await response
      .json()
      .catch(() => null)) as ResponsesPayload | null;
    console.error("Hosted agent invocation failed", {
      status: response.status,
      responseId: payload?.id,
    });
    throw new Error("The HR assistant is temporarily unavailable.");
  }
  if (!response.body) {
    throw new Error("The HR assistant did not return a response stream.");
  }

  return parseHostedAgentStream(response.body);
}

async function* parseHostedAgentStream(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<HostedAgentStreamEvent> {
  for await (const event of parseServerSentEvents(body)) {
    if (event?.type === "response.output_text.delta") {
      if (typeof event.delta === "string" && event.delta) {
        yield { type: "text_delta", delta: event.delta };
      }
      continue;
    }
    if (event?.type === "response.completed") {
      yield {
        type: "completed",
        ...parseCompletedResponse(event.response),
      };
      return;
    }
    if (event?.type === "response.failed" || event?.type === "error") {
      const error =
        event.error && typeof event.error === "object"
          ? (event.error as { code?: unknown })
          : undefined;
      console.error("Hosted agent stream failed", {
        type: event.type,
        code: error?.code,
      });
      throw new Error("The HR assistant is temporarily unavailable.");
    }
  }
  throw new Error("The HR assistant response stream ended unexpectedly.");
}

function parseCompletedResponse(payload: unknown): HostedAgentResponse {
  if (!payload || typeof payload !== "object") {
    throw new Error("The HR assistant returned an incomplete response.");
  }
  const response = payload as ResponsesPayload;
  if (
    response.status !== "completed" ||
    typeof response.id !== "string" ||
    !Array.isArray(response.output)
  ) {
    console.error("Hosted agent returned an incomplete response", {
      status: response.status,
      responseId: response.id,
    });
    throw new Error("The HR assistant returned an incomplete response.");
  }

  const outputText = response.output
    .flatMap((item) => item.content ?? [])
    .find((content) => content.type === "output_text");
  if (typeof outputText?.text !== "string" || !outputText.text.trim()) {
    throw new Error("The HR assistant response did not include text.");
  }

  const retrievedReferences = new Map<string, RetrievedReference>();
  for (const item of response.output) {
    if (
      item.type !== "function_call_output" ||
      typeof item.output !== "string"
    ) {
      continue;
    }
    for (const reference of parseToolReferences(item.output)) {
      const existing = retrievedReferences.get(reference.chunkId);
      if (!existing || reference.rerankerScore > existing.rerankerScore) {
        retrievedReferences.set(reference.chunkId, reference);
      }
    }
  }

  return {
    answer: outputText.text.trim(),
    citations: buildCitations(retrievedReferences),
    responseId: response.id,
  };
}

type ResponsesPayload = {
  id?: unknown;
  status?: unknown;
  output?: Array<{
    type?: unknown;
    output?: unknown;
    content?: Array<{
      type?: unknown;
      text?: unknown;
      annotations?: unknown;
    }>;
  }>;
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
  references: Map<string, RetrievedReference>,
): RichCitation[] {
  const grouped = new Map<
    string,
    { primary: RetrievedReference; chunkIds: string[]; excerpts: string[] }
  >();
  for (const reference of references.values()) {
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

  return [...grouped.values()].map(({ primary, chunkIds, excerpts }) => {
    const location =
      primary.pageStart === primary.pageEnd
        ? `Page ${primary.pageStart}`
        : `Pages ${primary.pageStart}–${primary.pageEnd}`;
    return {
      label: primary.documentTitle,
      location,
      chunkIds,
      documentId: primary.documentId,
      version: primary.version,
      pageStart: primary.pageStart,
      pageEnd: primary.pageEnd,
      excerpt: excerpts.join("\n\n"),
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
