import { randomUUID } from "node:crypto";

import { type NextRequest, NextResponse } from "next/server";

import { cryptoProvider, msalClient } from "@/lib/auth/msal-client";
import { safeReturnTo } from "@/lib/auth/safe-return-to";
import {
  createSession,
  getSession,
  saveSession,
  sessionCookieOptions,
} from "@/lib/auth/session";
import { config, signInScopes } from "@/lib/config";

export const runtime = "nodejs";

export async function GET(request: NextRequest) {
  const url = new URL(request.url);
  const session =
    getSession(request.cookies.get(config.sessionCookieName)?.value) ??
    createSession();
  const { verifier, challenge } = await cryptoProvider.generatePkceCodes();
  const state = randomUUID();
  session.authTransaction = {
    state,
    codeVerifier: verifier,
    returnTo: safeReturnTo(url.searchParams.get("returnTo"), config.appBaseUrl),
  };
  saveSession(session);

  const authUrl = await msalClient.getAuthCodeUrl({
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
    session.id,
    sessionCookieOptions,
  );
  return response;
}
