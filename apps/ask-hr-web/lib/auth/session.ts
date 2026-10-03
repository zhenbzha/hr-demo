import { createHash, createHmac, timingSafeEqual } from "node:crypto";

import { cookies } from "next/headers";

import type { SerializedAccount } from "@/lib/auth/msal-client";
import { config } from "@/lib/config";

export type AuthTransaction = {
  state: string;
  codeVerifier: string;
  returnTo: string;
};

export type UserSession = {
  updatedAt: number;
  account?: SerializedAccount;
  authTransaction?: AuthTransaction;
};

const sessionMaxAgeSeconds = 8 * 60 * 60;
const signingKey = createHash("sha256")
  .update(config.entraClientSecret)
  .digest();

export function createSession(): UserSession {
  return { updatedAt: Date.now() };
}

export function encodeSession(session: UserSession): string {
  const payload = Buffer.from(
    JSON.stringify({ ...session, updatedAt: Date.now() }),
  ).toString("base64url");
  const signature = createHmac("sha256", signingKey)
    .update(payload)
    .digest("base64url");
  return `${payload}.${signature}`;
}

export function getSession(value: string | undefined): UserSession | undefined {
  if (!value) {
    return undefined;
  }
  const [payload, signature] = value.split(".");
  if (!payload || !signature) {
    return undefined;
  }

  const expected = createHmac("sha256", signingKey).update(payload).digest();
  const actual = Buffer.from(signature, "base64url");
  if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) {
    return undefined;
  }

  try {
    const session = JSON.parse(
      Buffer.from(payload, "base64url").toString("utf8"),
    ) as UserSession;
    if (
      !session.updatedAt ||
      Date.now() - session.updatedAt > sessionMaxAgeSeconds * 1000
    ) {
      return undefined;
    }
    return session;
  } catch {
    return undefined;
  }
}

export async function getCurrentSession(): Promise<UserSession | undefined> {
  const cookieStore = await cookies();
  return getSession(cookieStore.get(config.sessionCookieName)?.value);
}

export const sessionCookieOptions = {
  httpOnly: true,
  secure: config.isProduction,
  sameSite: "lax" as const,
  path: "/",
  maxAge: sessionMaxAgeSeconds,
  priority: "high" as const,
};
