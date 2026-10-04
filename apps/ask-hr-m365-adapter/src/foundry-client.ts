import {
  DefaultAzureCredential,
  ManagedIdentityCredential,
  type TokenCredential,
} from "@azure/identity";

export interface AskHrCitation {
  label: string;
  location: string | null;
  url: string | null;
}

export interface AskHrAgentResponse {
  answer: string;
  citations: AskHrCitation[];
  responseId: string;
}

export interface FoundryRequest {
  question: string;
  searchToken: string;
  userObjectId: string;
  previousResponseId?: string;
}

export interface AskHrAgentClient {
  ask(request: FoundryRequest): Promise<AskHrAgentResponse>;
}

interface FoundryClientOptions {
  endpoint: string;
  managedIdentityClientId: string;
  credential?: TokenCredential;
  fetchImpl?: typeof fetch;
  requestTimeoutMs?: number;
}

const foundryScope = "https://ai.azure.com/.default";
const defaultRequestTimeoutMs = 120_000;

export class FoundryResponsesClient implements AskHrAgentClient {
  private readonly endpoint: string;
  private readonly credential: TokenCredential;
  private readonly fetchImpl: typeof fetch;
  private readonly requestTimeoutMs: number;

  constructor(options: FoundryClientOptions) {
    this.endpoint = options.endpoint;
    this.credential =
      options.credential ??
      (process.env.NODE_ENV === "development"
        ? new DefaultAzureCredential()
        : new ManagedIdentityCredential(options.managedIdentityClientId));
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.requestTimeoutMs =
      options.requestTimeoutMs ?? defaultRequestTimeoutMs;
  }

  async ask(request: FoundryRequest): Promise<AskHrAgentResponse> {
    const foundryToken = await this.credential.getToken(foundryScope);
    if (!foundryToken?.token) {
      throw new Error("Unable to acquire a Microsoft Foundry access token");
    }

    let response: Response;
    try {
      response = await this.fetchImpl(this.endpoint, {
        method: "POST",
        headers: {
          Authorization: ["Bearer", foundryToken.token].join(" "),
          "Content-Type": "application/json",
          "x-client-search-authorization": request.searchToken,
          "x-ms-user-identity": request.userObjectId,
        },
        body: JSON.stringify({
          input: request.question,
          stream: false,
          background: false,
          store: true,
          previous_response_id: request.previousResponseId,
        }),
        redirect: "error",
        signal: AbortSignal.timeout(this.requestTimeoutMs),
      });
    } catch (error) {
      if (
        error instanceof Error &&
        (error.name === "TimeoutError" || error.name === "AbortError")
      ) {
        throw new Error(
          `Ask HR request timed out after ${this.requestTimeoutMs} ms`,
        );
      }
      throw error;
    }

    const responseText = await response.text();
    let body: ResponsesPayload;
    try {
      body = JSON.parse(responseText) as ResponsesPayload;
    } catch {
      throw new Error(
        `Ask HR returned a non-JSON response (${response.status})`,
      );
    }

    if (!response.ok) {
      throw new Error(
        `Ask HR invocation failed (${response.status})`,
      );
    }
    if (body.status !== "completed") {
      throw new Error(
        `Ask HR Responses request failed with status ${String(body.status)}`,
      );
    }
    if (typeof body.id !== "string" || !Array.isArray(body.output)) {
      throw new Error("Ask HR returned an incomplete Responses payload");
    }

    const outputText = body.output
      .filter(isResponsesOutputItem)
      .flatMap((item) =>
        Array.isArray(item.content)
          ? item.content.filter(isResponsesOutputText)
          : [],
      )
      .find((content) => content.type === "output_text");
    if (!outputText || typeof outputText.text !== "string") {
      throw new Error("Ask HR response did not include output text");
    }

    return {
      answer: outputText.text.trim(),
      citations: parseCitations(outputText.annotations),
      responseId: body.id,
    };
  }
}

type ResponsesPayload = {
  id?: unknown;
  status?: unknown;
  error?: string | { message?: unknown };
  output?: unknown;
};

interface ResponsesOutputText {
  type?: unknown;
  text?: unknown;
  annotations?: unknown;
}

interface ResponsesOutputItem {
  type?: unknown;
  content?: unknown;
}

const isResponsesOutputItem = (value: unknown): value is ResponsesOutputItem =>
  value !== null && typeof value === "object";

const isResponsesOutputText = (value: unknown): value is ResponsesOutputText =>
  value !== null && typeof value === "object";

const parseCitations = (value: unknown): AskHrCitation[] => {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.flatMap((annotation): AskHrCitation[] => {
    if (
      !annotation ||
      typeof annotation !== "object" ||
      !("type" in annotation) ||
      annotation.type !== "url_citation" ||
      !("url" in annotation) ||
      typeof annotation.url !== "string"
    ) {
      return [];
    }
    const title =
      "title" in annotation && typeof annotation.title === "string"
        ? annotation.title
        : "Source";
    const separator = " - ";
    const separatorIndex = title.lastIndexOf(separator);
    return [
      {
        label:
          separatorIndex > 0 ? title.slice(0, separatorIndex) : title,
        location:
          separatorIndex > 0
            ? title.slice(separatorIndex + separator.length)
            : null,
        url: annotation.url,
      },
    ];
  });
};
