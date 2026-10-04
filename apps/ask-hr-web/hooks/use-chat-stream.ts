"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { RichCitation } from "@/lib/references/types";

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
  citations?: RichCitation[];
  status?: string;
};

type ChatResponse = {
  answer: string;
  citations: RichCitation[];
  responseId: string;
};

type ChatStreamEvent =
  | { type: "text_delta"; delta: string }
  | ({ type: "completed" } & ChatResponse)
  | { type: "error"; error: string };

export function useChatStream() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState<string>();
  const abortController = useRef<AbortController | null>(null);
  const previousResponseId = useRef<string | undefined>(undefined);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      abortController.current?.abort();
    };
  }, []);

  const submit = useCallback(async (message: string) => {
    const normalized = message.trim();
    if (!normalized || abortController.current) {
      return;
    }

    const userMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      text: normalized,
    };
    const assistantId = crypto.randomUUID();
    const controller = new AbortController();
    let animationFrame: number | undefined;
    abortController.current = controller;

    setMessages((current) => [
      ...current,
      userMessage,
      {
        id: assistantId,
        role: "assistant",
        text: "",
        status: "Reviewing regional and Global policies",
      },
    ]);
    setError(undefined);
    setIsSending(true);

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: normalized,
          previousResponseId: previousResponseId.current,
        }),
        signal: controller.signal,
      });

      if (response.status === 401) {
        window.location.assign("/api/auth/login");
        return;
      }
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as {
          error?: string;
        } | null;
        throw new Error(
          body?.error
            ? body.error
            : "The HR assistant is temporarily unavailable.",
        );
      }

      let streamedText = "";
      let completed = false;
      const flushText = () => {
        animationFrame = undefined;
        if (!mounted.current) {
          return;
        }
        setMessages((current) =>
          current.map((item) =>
            item.id === assistantId
              ? { ...item, text: streamedText, status: undefined }
              : item,
          ),
        );
      };

      for await (const event of readChatEvents(response)) {
        if (event.type === "text_delta") {
          streamedText += event.delta;
          if (animationFrame === undefined) {
            animationFrame = requestAnimationFrame(flushText);
          }
          continue;
        }
        if (event.type === "error") {
          throw new Error(event.error);
        }

        completed = true;
        previousResponseId.current = event.responseId;
        streamedText = event.answer;
        if (animationFrame !== undefined) {
          cancelAnimationFrame(animationFrame);
          animationFrame = undefined;
        }
        if (mounted.current) {
          setMessages((current) =>
            current.map((item) =>
              item.id === assistantId
                ? {
                    ...item,
                    text: event.answer,
                    citations: event.citations,
                    status: undefined,
                  }
                : item,
            ),
          );
        }
      }
      if (!completed) {
        throw new Error("The HR assistant returned an incomplete response.");
      }
    } catch (caught) {
      if (animationFrame !== undefined) {
        cancelAnimationFrame(animationFrame);
        animationFrame = undefined;
      }
      if ((caught as Error).name !== "AbortError" && mounted.current) {
        const message =
          caught instanceof Error
            ? caught.message
            : "The HR assistant is temporarily unavailable.";
        setError(message);
        setMessages((current) =>
          current.map((item) =>
            item.id === assistantId
              ? {
                  ...item,
                  text: "I could not complete that request. Please try again.",
                  status: undefined,
                }
              : item,
          ),
        );
      }
    } finally {
      if (animationFrame !== undefined) {
        cancelAnimationFrame(animationFrame);
      }
      if (abortController.current === controller) {
        abortController.current = null;
        if (mounted.current) {
          setIsSending(false);
        }
      }
    }
  }, []);

  const clearConversation = useCallback(() => {
    abortController.current?.abort();
    abortController.current = null;
    previousResponseId.current = undefined;
    setMessages([]);
    setError(undefined);
    setIsSending(false);
  }, []);

  return {
    messages,
    isSending,
    error,
    submit,
    clearConversation,
  };
}

async function* readChatEvents(
  response: Response,
): AsyncGenerator<ChatStreamEvent> {
  if (!response.body) {
    throw new Error("The HR assistant did not return a response stream.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (!line.trim()) {
          continue;
        }
        yield parseChatStreamEvent(line);
      }
      if (done) {
        break;
      }
    }
    if (buffer.trim()) {
      yield parseChatStreamEvent(buffer);
    }
  } finally {
    reader.releaseLock();
  }
}

function parseChatStreamEvent(line: string): ChatStreamEvent {
  const value = JSON.parse(line) as Record<string, unknown>;
  if (value.type === "text_delta" && typeof value.delta === "string") {
    return { type: "text_delta", delta: value.delta };
  }
  if (
    value.type === "completed" &&
    typeof value.answer === "string" &&
    typeof value.responseId === "string" &&
    Array.isArray(value.citations)
  ) {
    return {
      type: "completed",
      answer: value.answer,
      responseId: value.responseId,
      citations: value.citations as RichCitation[],
    };
  }
  if (value.type === "error" && typeof value.error === "string") {
    return { type: "error", error: value.error };
  }
  throw new Error("The HR assistant returned an invalid stream event.");
}
