import { type NextRequest, NextResponse } from "next/server";

import { msalClient } from "@/lib/auth/msal-client";
import { deleteSession, getSession } from "@/lib/auth/session";
import { config } from "@/lib/config";

export const runtime = "nodejs";

export async function POST(request: NextRequest) {
  if (request.headers.get("origin") !== new URL(config.appBaseUrl).origin) {
    return NextResponse.json({ error: "Invalid origin" }, { status: 403 });
  }
  const sessionId = request.cookies.get(config.sessionCookieName)?.value;
  const session = getSession(sessionId);
  if (session?.account) {
    const account = await msalClient
      .getTokenCache()
      .getAccountByHomeId(session.account.homeAccountId);
    if (account) {
      await msalClient.getTokenCache().removeAccount(account);
    }
  }
  deleteSession(sessionId);
  const response = NextResponse.redirect(new URL("/", config.appBaseUrl), 303);
  response.cookies.delete(config.sessionCookieName);
  return response;
}
