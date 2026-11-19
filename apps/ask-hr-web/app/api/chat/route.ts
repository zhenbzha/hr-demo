import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { getCurrentSession } from "@/lib/auth/session";
import {
  acquireSearchToken,
  ReauthenticationRequiredError,
} from "@/lib/auth/tokens";
import { config } from "@/lib/config";
import { invokeAgentStream } from "@/lib/foundry/client";
import { parseChatRequest } from "@/lib/validation/chat-request";

export const runtime = "nodejs";
export const maxDuration = 180;

export async function POST(request: NextRequest) {
  const session = config.publicDemoEnabled
    ? undefined
    : await getCurrentSession();
  if (!config.publicDemoEnabled && !session?.account) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  let chatRequest: ReturnType<typeof parseChatRequest>;
  try {
    chatRequest = parseChatRequest(await request.json());
  } catch (error) {
    console.warn("Invalid chat request", error);
    return NextResponse.json(
      {
        code: "invalid_request",
        error: "The chat request is invalid.",
      },
      { status: 400 },
    );
  }

  try {
    const searchToken = session ? await acquireSearchToken(session) : undefined;
    const streamAbort = new AbortController();
    const events = await invokeAgentStream({
      message: chatRequest.message,
      searchToken,
      userObjectId:
        session?.account?.localAccountId ?? config.foundryCallerObjectId,
      previousResponseId: chatRequest.previousResponseId,
      signal: AbortSignal.any([request.signal, streamAbort.signal]),
    });
    const iterator = events[Symbol.asyncIterator]();
    const encoder = new TextEncoder();
    let closed = false;
    const stream = new ReadableStream<Uint8Array>({
      async pull(controller) {
        try {
          const { value, done } = await iterator.next();
          if (done) {
            closed = true;
            controller.close();
            return;
          }
          controller.enqueue(encoder.encode(`${JSON.stringify(value)}\n`));
        } catch (error) {
          if (!streamAbort.signal.aborted && !request.signal.aborted) {
            console.error("Chat response stream failed", error);
            controller.enqueue(
              encoder.encode(
                `${JSON.stringify({
                  type: "error",
                  error:
                    "The HR assistant is temporarily unavailable. Please try again.",
                })}\n`,
              ),
            );
          }
          closed = true;
          controller.close();
        }
      },
      async cancel(reason) {
        if (!closed) {
          streamAbort.abort(reason);
          await iterator.return?.(undefined);
          closed = true;
        }
      },
    });
    return new Response(stream, {
      headers: {
        "Cache-Control": "no-store",
        "Content-Type": "application/x-ndjson; charset=utf-8",
        "X-Content-Type-Options": "nosniff",
      },
    });
  } catch (error) {
    if (error instanceof ReauthenticationRequiredError) {
      return NextResponse.json(
        {
          code: "reauthentication_required",
          error: "Please sign in again to refresh your HR policy access.",
        },
        { status: 401 },
      );
    }
    console.error("Chat request failed", error);
    return NextResponse.json(
      {
        code: "assistant_unavailable",
        error: "The HR assistant is temporarily unavailable. Please try again.",
      },
      { status: 502 },
    );
  }
}
