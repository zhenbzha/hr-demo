import { randomUUID } from "node:crypto";

import { NextResponse } from "next/server";

import { createMsalClient, cryptoProvider } from "@/lib/auth/msal-client";
import {
  createSession,
  encodeSession,
  sessionCookieOptions,
} from "@/lib/auth/session";
import { config, signInScopes } from "@/lib/config";

export const runtime = "nodejs";

export async function GET(request: Request) {
  const session = createSession();
  const { verifier, challenge } = await cryptoProvider.generatePkceCodes();
  const returnTo = safeReturnTo(
    new URL(request.url).searchParams.get("returnTo"),
  );
  const state = randomUUID();

  session.authTransaction = {
    state,
    codeVerifier: verifier,
    returnTo,
  };

  const authUrl = await createMsalClient().getAuthCodeUrl({
    scopes: signInScopes,
    redirectUri: config.entraRedirectUri,
    responseMode: "query",
    state,
    codeChallenge: challenge,
    codeChallengeMethod: "S256",
    prompt: "select_account",
  });

  const response = NextResponse.redirect(authUrl);
  response.cookies.set(
    config.sessionCookieName,
    encodeSession(session),
    sessionCookieOptions,
  );
  return response;
}

function safeReturnTo(value: string | null): string {
  return value?.startsWith("/") && !value.startsWith("//") ? value : "/";
}
