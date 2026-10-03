import { type NextRequest, NextResponse } from "next/server";

import { createMsalClient } from "@/lib/auth/msal-client";
import {
  encodeSession,
  getSession,
  sessionCookieOptions,
} from "@/lib/auth/session";
import { config, signInScopes } from "@/lib/config";

export const runtime = "nodejs";

export async function GET(request: NextRequest) {
  const url = new URL(request.url);
  const sessionId = request.cookies.get(config.sessionCookieName)?.value;
  const session = getSession(sessionId);
  const transaction = session?.authTransaction;

  if (!session || !transaction) {
    return authError("The sign-in session expired. Please try again.");
  }

  const providerError = url.searchParams.get("error");
  if (providerError) {
    return authError(
      url.searchParams.get("error_description") || providerError,
    );
  }

  const state = url.searchParams.get("state");
  const code = url.searchParams.get("code");
  if (!code || state !== transaction.state) {
    return authError("The sign-in response could not be validated.");
  }

  try {
    const msal = createMsalClient();
    const result = await msal.acquireTokenByCode({
      code,
      scopes: signInScopes,
      redirectUri: config.entraRedirectUri,
      codeVerifier: transaction.codeVerifier,
    });
    if (!result.account) {
      throw new Error("Microsoft Entra did not return an account");
    }

    session.account = {
      homeAccountId: result.account.homeAccountId,
      environment: result.account.environment,
      tenantId: result.account.tenantId,
      username: result.account.username,
      localAccountId: result.account.localAccountId,
      name: result.account.name,
    };
    session.authTransaction = undefined;

    const response = NextResponse.redirect(
      new URL(transaction.returnTo, config.appBaseUrl),
    );
    response.cookies.set(
      config.sessionCookieName,
      encodeSession(session),
      sessionCookieOptions,
    );
    return response;
  } catch (error) {
    return authError(error instanceof Error ? error.message : "Sign-in failed");
  }
}

function authError(message: string) {
  const target = new URL("/", config.appBaseUrl);
  target.searchParams.set("authError", message);
  return NextResponse.redirect(target);
}
