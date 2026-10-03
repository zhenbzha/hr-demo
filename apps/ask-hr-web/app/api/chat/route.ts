import { NextResponse } from "next/server";

import { getCurrentSession } from "@/lib/auth/session";
import { streamAgent } from "@/lib/foundry/client";
import { parseChatMessages } from "@/lib/validation/chat-request";

export const runtime = "nodejs";
export const maxDuration = 180;

export async function POST(request: Request) {
  const session = await getCurrentSession();
  if (!session?.account) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  try {
    const messages = parseChatMessages(await request.json());
    const stream = await streamAgent(messages);
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
        error:
          error instanceof Error
            ? error.message
            : "The HR assistant is temporarily unavailable.",
      },
      { status: 502 },
    );
  }
}
