import "server-only";

import { randomUUID } from "node:crypto";

import { cookies } from "next/headers";

import type { SerializedAccount } from "@/lib/auth/msal-client";
import { config } from "@/lib/config";

export type AuthTransaction = {
  state: string;
  codeVerifier: string;
  returnTo: string;
};

export type UserSession = {
  id: string;
  updatedAt: number;
  account?: SerializedAccount;
  authTransaction?: AuthTransaction;
};

type SessionStore = Map<string, UserSession>;

declare global {
  var askHrSessionStore: SessionStore | undefined;
}

const sessions = globalThis.askHrSessionStore ?? new Map<string, UserSession>();

if (!globalThis.askHrSessionStore) {
  globalThis.askHrSessionStore = sessions;
}

const sessionMaxAgeSeconds = 8 * 60 * 60;

export function createSession(): UserSession {
  const session = {
    id: randomUUID(),
    updatedAt: Date.now(),
  };
  sessions.set(session.id, session);
  return session;
}

export function getSession(id: string | undefined): UserSession | undefined {
  if (!id) {
    return undefined;
  }
  const session = sessions.get(id);
  if (!session) {
    return undefined;
  }
  if (Date.now() - session.updatedAt > sessionMaxAgeSeconds * 1000) {
    sessions.delete(id);
    return undefined;
  }
  session.updatedAt = Date.now();
  return session;
}

export function saveSession(session: UserSession): void {
  session.updatedAt = Date.now();
  sessions.set(session.id, session);
}

export function deleteSession(id: string | undefined): void {
  if (id) {
    sessions.delete(id);
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
