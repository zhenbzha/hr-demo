"use client";

import {
  BookOpen,
  FileText,
  Globe2,
  LogOut,
  MessageSquarePlus,
  Send,
  Sparkles,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Textarea } from "@/components/ui/textarea";

type Citation = {
  label: string;
  location?: string;
  url?: string;
};

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
  citations?: Citation[];
  status?: string;
};

type ChatUser = {
  name?: string;
  username: string;
};

const suggestions = [
  {
    icon: Globe2,
    label: "Regional policy",
    prompt: "What are the Hong Kong MPF contribution rules?",
  },
  {
    icon: BookOpen,
    label: "Compare regions",
    prompt: "Compare annual leave in Finland and Hong Kong.",
  },
  {
    icon: FileText,
    label: "Workplace conduct",
    prompt: "What is the India POSH complaint process?",
  },
  {
    icon: Sparkles,
    label: "Global guidance",
    prompt: "What responsible AI rules apply to employees in India?",
  },
  {
    icon: Globe2,
    label: "繁體中文",
    prompt: "香港僱員有多少天年假？",
  },
  {
    icon: BookOpen,
    label: "Suomeksi",
    prompt: "Mitkä ovat työntekijän irtisanomisajat Suomessa?",
  },
];

export function ChatShell({ user }: { user: ChatUser }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState<string>();
  const abortController = useRef<AbortController | null>(null);
  const messageEnd = useRef<HTMLDivElement | null>(null);
  const router = useRouter();

  const initials = useMemo(() => {
    const source = user.name || user.username;
    return source
      .split(/[\s.@]+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((part) => part[0]?.toUpperCase())
      .join("");
  }, [user.name, user.username]);

  useEffect(() => {
    if (messages.length > 0) {
      messageEnd.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages.length]);

  async function submit(message: string) {
    const normalized = message.trim();
    if (!normalized || isSending) {
      return;
    }

    const userMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      text: normalized,
    };
    const assistantId = crypto.randomUUID();
    const history = [...messages, userMessage];

    setMessages([
      ...history,
      {
        id: assistantId,
        role: "assistant",
        text: "",
        status: "Understanding your question",
      },
    ]);
    setInput("");
    setError(undefined);
    setIsSending(true);

    const controller = new AbortController();
    abortController.current = controller;

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
        router.push("/api/auth/login");
        return;
      }
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as {
          error?: string;
        } | null;
        throw new Error(body?.error || "The chat request failed");
      }

      if (!response.body) {
        throw new Error("The chat response did not include a stream");
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
          const event = JSON.parse(line) as
            | { type: "status"; message: string }
            | { type: "answer"; answer: string }
            | { type: "citations"; citations: Citation[] }
            | { type: "error"; message: string }
            | { type: "done" };

          if (event.type === "error") {
            throw new Error(event.message);
          }
          if (event.type === "status") {
            setMessages((current) =>
              current.map((item) =>
                item.id === assistantId
                  ? { ...item, status: event.message }
                  : item,
              ),
            );
          }
          if (event.type === "answer") {
            setMessages((current) =>
              current.map((item) =>
                item.id === assistantId
                  ? { ...item, text: event.answer, status: undefined }
                  : item,
              ),
            );
          }
          if (event.type === "citations") {
            setMessages((current) =>
              current.map((item) =>
                item.id === assistantId
                  ? { ...item, citations: event.citations }
                  : item,
              ),
            );
          }
        }
      }
    } catch (caught) {
      if ((caught as Error).name !== "AbortError") {
        const message =
          caught instanceof Error ? caught.message : "Unexpected chat error";
        setError(message);
        setMessages((current) =>
          current.map((item) =>
            item.id === assistantId && !item.text
              ? {
                  ...item,
                  text: "I could not complete that request. Please try again.",
                }
              : item,
          ),
        );
      }
    } finally {
      setIsSending(false);
      abortController.current = null;
    }
  }

  function clearConversation() {
    abortController.current?.abort();
    setMessages([]);
    setError(undefined);
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void submit(input);
  }

  return (
    <div className="flex h-screen overflow-hidden bg-slate-50 text-slate-950">
      <aside className="hidden w-72 shrink-0 flex-col border-r bg-white px-4 py-5 lg:flex">
        <Brand />
        <Button
          variant="ghost"
          onClick={clearConversation}
          className="mt-7 justify-start"
        >
          <MessageSquarePlus />
          New conversation
        </Button>

        <div className="mt-8 space-y-3">
          <p className="px-2 text-xs font-medium uppercase tracking-wider text-slate-400">
            Coverage
          </p>
          <div className="flex flex-wrap gap-2 px-2">
            {["Global", "India", "Finland", "Hong Kong"].map((region) => (
              <Badge key={region} variant="secondary">
                {region}
              </Badge>
            ))}
          </div>
        </div>

        <div className="mt-auto">
          <Separator className="mb-4" />
          <div className="flex items-center gap-3 px-2">
            <Avatar>
              <AvatarFallback className="bg-emerald-700 text-white">
                {initials}
              </AvatarFallback>
            </Avatar>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">
                {user.name || "Signed-in user"}
              </p>
              <p className="truncate text-xs text-slate-500">{user.username}</p>
            </div>
          </div>
          <Button variant="outline" asChild className="mt-4 w-full">
            <a href="/api/auth/logout">
              <LogOut />
              Sign out
            </a>
          </Button>
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-16 shrink-0 items-center justify-between border-b bg-white px-4 lg:hidden">
          <Brand compact />
          <Button variant="outline" size="sm" asChild>
            <a href="/api/auth/logout">Sign out</a>
          </Button>
        </header>

        <section className="flex min-h-0 flex-1 flex-col bg-slate-50/60">
          <div className="flex-1 overflow-y-auto">
            <div className="mx-auto flex min-h-full w-full max-w-4xl flex-col px-5 py-8 md:px-8">
              {messages.length === 0 ? (
                <EmptyState onSelect={(prompt) => void submit(prompt)} />
              ) : (
                <div className="space-y-8">
                  {messages.map((message, index) => (
                    <Message
                      key={message.id}
                      message={message}
                      isLoading={
                        isSending &&
                        message.role === "assistant" &&
                        index === messages.length - 1 &&
                        !message.text
                      }
                      status={message.status}
                    />
                  ))}
                  <div ref={messageEnd} />
                </div>
              )}
            </div>
          </div>

          <div className="shrink-0 px-4 pb-4 pt-2 md:px-7">
            <form
              onSubmit={onSubmit}
              className="mx-auto flex max-w-4xl items-end gap-2 rounded-2xl border bg-white p-2 shadow-lg shadow-slate-900/5 focus-within:border-emerald-500"
            >
              <Textarea
                value={input}
                onChange={(event) => setInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    event.currentTarget.form?.requestSubmit();
                  }
                }}
                rows={1}
                maxLength={4000}
                placeholder="Ask an HR-related question"
                className="max-h-40 min-h-11 resize-none border-0 bg-transparent shadow-none focus-visible:ring-0"
              />
              <Button
                type="submit"
                size="icon"
                disabled={!input.trim() || isSending}
                className="shrink-0 rounded-full"
                aria-label="Send message"
              >
                <Send />
              </Button>
            </form>
            {error && (
              <p className="mx-auto mt-2 max-w-4xl text-xs text-rose-600">
                {error}
              </p>
            )}
            <p className="mx-auto mt-2 max-w-4xl text-center text-[11px] text-slate-400">
              Verify employment decisions with HR. Answers use fictional demo
              policies.
            </p>
          </div>
        </section>
      </main>
    </div>
  );
}

function Message({
  message,
  isLoading,
  status,
}: {
  message: ChatMessage;
  isLoading: boolean;
  status?: string;
}) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[82%] rounded-3xl rounded-br-lg bg-emerald-950 px-5 py-3 text-sm leading-6 text-white shadow-sm">
          {message.text}
        </div>
      </div>
    );
  }

  return (
    <article className="flex min-w-0 gap-3">
      <div className="grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-emerald-700 text-[11px] font-bold text-white shadow-sm">
        HR
      </div>
      <div className="min-w-0 flex-1 pt-0.5">
        <p className="mb-3 text-xs font-semibold uppercase tracking-wider text-slate-400">
          Ask HR
        </p>
        {isLoading ? (
          <RetrievalIndicator
            label={status || "Reviewing policy information"}
          />
        ) : (
          <div className="prose-hr text-[15px] leading-7 text-slate-800">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {message.text}
            </ReactMarkdown>
          </div>
        )}
        {!!message.citations?.length && (
          <div className="mt-5">
            <p className="mb-2 text-xs font-medium uppercase tracking-wider text-slate-400">
              Sources
            </p>
            <div className="grid gap-2 sm:grid-cols-2">
              {message.citations.map((citation, index) => (
                <SourceCard
                  key={`${citation.label}-${citation.location ?? ""}`}
                  citation={citation}
                  index={index + 1}
                />
              ))}
            </div>
          </div>
        )}
      </div>
    </article>
  );
}

function SourceCard({
  citation,
  index,
}: {
  citation: Citation;
  index: number;
}) {
  const content = (
    <div className="flex items-start gap-3">
      <div className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-emerald-50 text-xs font-semibold text-emerald-800">
        {index}
      </div>
      <div className="min-w-0">
        <p className="line-clamp-2 text-xs font-semibold text-slate-700">
          {citation.label}
        </p>
        {citation.location && (
          <p className="mt-1 text-[11px] text-slate-500">{citation.location}</p>
        )}
      </div>
    </div>
  );

  return citation.url ? (
    <Card className="p-3 transition hover:border-emerald-300">
      <a href={citation.url} target="_blank" rel="noreferrer">
        {content}
      </a>
    </Card>
  ) : (
    <Card className="p-3">{content}</Card>
  );
}

function RetrievalIndicator({ label }: { label: string }) {
  return (
    <div
      className="flex h-8 items-center gap-2"
      role="status"
      aria-label={label}
    >
      <span className="h-2 w-2 animate-bounce rounded-full bg-emerald-600 [animation-delay:-0.3s]" />
      <span className="h-2 w-2 animate-bounce rounded-full bg-emerald-600 [animation-delay:-0.15s]" />
      <span className="h-2 w-2 animate-bounce rounded-full bg-emerald-600" />
    </div>
  );
}

function EmptyState({ onSelect }: { onSelect: (prompt: string) => void }) {
  return (
    <div className="my-auto py-10">
      <div className="max-w-2xl">
        <Badge variant="secondary">Policy-grounded answers</Badge>
        <h1 className="mt-5 text-4xl font-semibold tracking-tight text-slate-950">
          How can HR help?
        </h1>
        <p className="mt-4 max-w-xl text-base leading-7 text-slate-600">
          Ask about employment terms, payroll, leave, workplace conduct, or
          compare policies across supported regions.
        </p>
      </div>
      <div className="mt-8 grid gap-3 md:grid-cols-2">
        {suggestions.map((suggestion) => {
          const Icon = suggestion.icon;
          return (
            <Card
              key={suggestion.prompt}
              className="cursor-pointer p-5 transition hover:-translate-y-0.5 hover:border-emerald-300 hover:shadow-md"
              onClick={() => onSelect(suggestion.prompt)}
            >
              <Icon className="h-5 w-5 text-emerald-700" />
              <p className="mt-4 text-xs font-semibold uppercase tracking-wider text-emerald-700">
                {suggestion.label}
              </p>
              <p className="mt-2 text-sm leading-6 text-slate-700">
                {suggestion.prompt}
              </p>
            </Card>
          );
        })}
      </div>
    </div>
  );
}

function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <div className="flex items-center gap-3">
      <div
        className={`grid place-items-center rounded-xl bg-emerald-700 font-bold text-white ${
          compact ? "h-9 w-9 text-xs" : "h-10 w-10 text-sm"
        }`}
      >
        HR
      </div>
      <div>
        <p className="font-semibold">Ask HR</p>
        {!compact && (
          <p className="text-xs text-slate-500">Regional policy assistant</p>
        )}
      </div>
    </div>
  );
}
