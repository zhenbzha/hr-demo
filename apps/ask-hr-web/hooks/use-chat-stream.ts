"use client";

import { signIn } from "next-auth/react";
import { useCallback, useEffect, useRef, useState } from "react";

import type { RichCitation } from "@/lib/references/types";

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
  citations?: RichCitation[];
  status?: string;
};

type AssistantPatch = {
  text?: string;
  citations?: RichCitation[];
  status?: string;
};

type ChatStreamEvent =
  | { type: "status"; message: string }
  | { type: "answer"; answer: string }
  | { type: "citations"; citations: RichCitation[] }
  | { type: "error"; code?: string; message: string }
  | { type: "done" };

export function useChatStream() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState<string>();
  const abortController = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const animationFrame = useRef<number | undefined>(undefined);
  const pendingPatch = useRef<
    | {
        assistantId: string;
        patch: AssistantPatch;
      }
    | undefined
  >(undefined);
  const flushAssistantPatch = useCallback(() => {
    if (animationFrame.current !== undefined) {
      cancelAnimationFrame(animationFrame.current);
      animationFrame.current = undefined;
    }
    const pending = pendingPatch.current;
    pendingPatch.current = undefined;
    if (!pending || !mounted.current) {
      return;
    }
    setMessages((current) =>
      current.map((message) =>
        message.id === pending.assistantId
          ? { ...message, ...pending.patch }
          : message,
      ),
    );
  }, []);

  const scheduleAssistantPatch = useCallback(
    (assistantId: string, patch: AssistantPatch) => {
      if (!mounted.current) {
        return;
      }
      const current = pendingPatch.current;
      pendingPatch.current = {
        assistantId,
        patch:
          current?.assistantId === assistantId
            ? { ...current.patch, ...patch }
            : patch,
      };
      if (animationFrame.current === undefined) {
        animationFrame.current = requestAnimationFrame(flushAssistantPatch);
      }
    },
    [flushAssistantPatch],
  );

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      abortController.current?.abort();
      if (animationFrame.current !== undefined) {
        cancelAnimationFrame(animationFrame.current);
      }
      pendingPatch.current = undefined;
    };
  }, []);

  const submit = useCallback(
    async (message: string) => {
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
      const history = [...messages, userMessage];
      const controller = new AbortController();
      abortController.current = controller;

      setMessages([
        ...history,
        {
          id: assistantId,
          role: "assistant",
          text: "",
          status: "Understanding your question",
        },
      ]);
      setError(undefined);
      setIsSending(true);

      try {
        const response = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            messages: history.map(({ role, text }) => ({ role, text })),
          }),
          signal: controller.signal,
        });

        if (response.status === 401) {
          await signIn("azure-ad", { callbackUrl: "/" });
          return;
        }
        if (!response.ok) {
          const body = (await response.json().catch(() => null)) as {
            error?: string;
          } | null;
          throw new Error(
            body?.error || "The HR assistant is temporarily unavailable.",
          );
        }
        if (!response.body) {
          throw new Error("The HR assistant returned an incomplete response.");
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) {
            break;
          }

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() ?? "";

          for (const line of lines) {
            if (!line.trim()) {
              continue;
            }
            const event = JSON.parse(line) as ChatStreamEvent;
            if (event.type === "error") {
              throw new Error(event.message);
            }
            if (event.type === "status") {
              scheduleAssistantPatch(assistantId, { status: event.message });
            }
            if (event.type === "answer") {
              scheduleAssistantPatch(assistantId, {
                text: event.answer,
                status: undefined,
              });
            }
            if (event.type === "citations") {
              scheduleAssistantPatch(assistantId, {
                citations: event.citations,
              });
            }
          }
        }
        flushAssistantPatch();
      } catch (caught) {
        flushAssistantPatch();
        if ((caught as Error).name !== "AbortError") {
          const message =
            caught instanceof Error
              ? caught.message
              : "The HR assistant is temporarily unavailable.";
          setError(message);
          setMessages((current) =>
            current.map((item) =>
              item.id === assistantId && !item.text
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
        if (abortController.current === controller) {
          abortController.current = null;
          if (mounted.current) {
            setIsSending(false);
          }
        }
      }
    },
    [flushAssistantPatch, messages, scheduleAssistantPatch],
  );

  const clearConversation = useCallback(() => {
    abortController.current?.abort();
    abortController.current = null;
    if (animationFrame.current !== undefined) {
      cancelAnimationFrame(animationFrame.current);
      animationFrame.current = undefined;
    }
    pendingPatch.current = undefined;
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
