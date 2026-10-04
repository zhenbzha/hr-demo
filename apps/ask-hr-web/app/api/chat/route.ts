import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

import { getCurrentToken } from "@/lib/auth/session";
import { streamAgent } from "@/lib/foundry/client";
import { parseChatMessages } from "@/lib/validation/chat-request";

export const runtime = "nodejs";
export const maxDuration = 180;

export async function POST(request: NextRequest) {
  const token = await getCurrentToken(request);
  if (!token?.sub) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const agentSessionId = token.agentSessionId || token.sub;

  let messages: ReturnType<typeof parseChatMessages>;
  try {
    messages = parseChatMessages(await request.json());
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
    const stream = await streamAgent(messages, agentSessionId);
    return new Response(stream, {
      headers: {
        "Cache-Control": "no-store",
        "Content-Type": "application/x-ndjson; charset=utf-8",
        "X-Content-Type-Options": "nosniff",
      },
    });
  } catch (error) {
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
