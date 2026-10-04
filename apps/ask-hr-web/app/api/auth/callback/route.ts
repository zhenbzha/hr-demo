import { type NextRequest, NextResponse } from "next/server";
import type { AuthErrorCode } from "@/lib/auth/auth-errors";
import { msalClient } from "@/lib/auth/msal-client";
import {
  createSession,
  deleteSession,
  getSession,
  saveSession,
  sessionCookieOptions,
} from "@/lib/auth/session";
import { config, signInScopes } from "@/lib/config";

export const runtime = "nodejs";

export async function GET(request: NextRequest) {
  const session = getSession(
    request.cookies.get(config.sessionCookieName)?.value,
  );
  const transaction = session?.authTransaction;
  if (!session || !transaction) {
    return authError("session_expired");
  }

  const url = new URL(request.url);
  const providerError = url.searchParams.get("error");
  if (providerError) {
    console.error("Entra authorization failed", {
      error: providerError,
      description: url.searchParams.get("error_description"),
    });
    return authError("provider_error");
  }
  const state = url.searchParams.get("state");
  const code = url.searchParams.get("code");
  if (!code || state !== transaction.state) {
    return authError("validation_failed");
  }

  try {
    const result = await msalClient.acquireTokenByCode({
      code,
      scopes: signInScopes,
      redirectUri: config.entraRedirectUri,
      codeVerifier: transaction.codeVerifier,
    });
    if (!result.account) {
      throw new Error("Microsoft Entra did not return an account");
    }
    const authenticatedSession = createSession();
    authenticatedSession.account = {
      homeAccountId: result.account.homeAccountId,
      environment: result.account.environment,
      tenantId: result.account.tenantId,
      username: result.account.username,
      localAccountId: result.account.localAccountId,
      name: result.account.name,
    };
    saveSession(authenticatedSession);
    deleteSession(session.id);

    const response = NextResponse.redirect(
      new URL(transaction.returnTo, config.appBaseUrl),
    );
    response.cookies.set(
      config.sessionCookieName,
      authenticatedSession.id,
      sessionCookieOptions,
    );
    return response;
  } catch (error) {
    console.error(
      "Entra callback failed",
      error instanceof Error ? error.message : "Unknown error",
    );
    return authError("callback_failed");
  }
}

function authError(code: AuthErrorCode) {
  const target = new URL("/", config.appBaseUrl);
  target.searchParams.set("authError", code);
  return NextResponse.redirect(target);
}
